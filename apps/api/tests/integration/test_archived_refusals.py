"""Archived is not absent: every write verb, and the three answers it gives.

Stage C2. Until C1 there was nothing a caller could do about an archived
agent or workflow, so every write answered 404 and "archived" was
indistinguishable from "never existed". Restoring exists now, and these
tests pin the distinction that makes it usable:

- **archived** → 409, naming the state and the remedy;
- **unknown** → 404, unchanged, which is what keeps the 409 meaningful;
- **a test harness** → the harness 409, which takes precedence and is not
  the archived one;
- **live** → whatever it always did.

The last case in each table is the one that matters most: restore, then
the same verb succeeds.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_restore import Island

from helpers import Actor

pytestmark = pytest.mark.integration

SAMPLE = "Summarise the fintech funding news."


@pytest.fixture
def island(client: TestClient, actor: Actor) -> Island:
    return Island(client, actor)


def assert_archived(response: Any, kind: str) -> None:
    """A 409 that names the state, the remedy, and nothing else."""
    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    detail = response.json()["detail"]
    assert f"This {kind} is archived" in detail
    assert "restored" in detail
    assert f"{kind}_id" not in response.json(), "the id is for the log, not the body"


# ---------------------------------------------------------------- the agent ---


def test_editing_an_archived_agent_says_so(island: Island) -> None:
    island.archive_agent()

    refused = island.client.patch(
        f"{island.agents}/{island.agent_id}",
        json={"display_name": "Back to work"},
        headers=island.headers(),
    )

    assert_archived(refused, "agent")
    assert island.read_agent()["display_name"] == "Researcher", "nothing was written"


def test_editing_an_unknown_agent_is_still_a_404(island: Island) -> None:
    """The half that keeps the other half meaningful."""
    response = island.client.patch(
        f"{island.agents}/{uuid.uuid4()}",
        json={"display_name": "Nobody"},
        headers=island.headers(),
    )

    assert response.status_code == 404, response.text


def test_archiving_an_archived_agent_says_it_is_already_archived(island: Island) -> None:
    """A different sentence, because the remedy is different.

    The caller asked for the state the row is already in, so "restore it
    first" would be nonsense - and 404 would claim the agent is gone.
    """
    island.archive_agent()

    refused = island.archive_agent()

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert "already archived" in detail
    assert "restored" not in detail


def test_an_archived_agent_can_be_edited_once_restored(island: Island) -> None:
    island.archive_agent()
    assert island.restore_agent().status_code == 200

    response = island.client.patch(
        f"{island.agents}/{island.agent_id}",
        json={"display_name": "Back to work"},
        headers=island.headers(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Back to work"


# ------------------------------------------------------------- the workflow ---


def test_every_archived_workflow_write_says_so(island: Island) -> None:
    """One table, because the answer has to be the same for all of them.

    A client that learns the rule from `publish` must not be surprised by
    `run`. `validate` is in the table deliberately: it writes nothing, but
    it answers "could this run", and an archived workflow cannot run at all
    - a report of green ticks would be true of the graph and misleading
    about the workflow.
    """
    published = island.client.post(
        f"{island.workflows}/{island.workflow_id}/publish", headers=island.headers()
    )
    assert published.status_code == 201, published.text
    island.archive_workflow()
    base = f"{island.workflows}/{island.workflow_id}"

    attempts = {
        "patch": island.client.patch(base, json={"name": "Renamed"}, headers=island.headers()),
        "draft": island.client.put(
            f"{base}/draft",
            json={"graph": {"steps": [], "edges": []}},
            headers=island.headers(),
        ),
        "publish": island.client.post(f"{base}/publish", headers=island.headers()),
        "run": island.client.post(f"{base}/run", json={}, headers=island.headers()),
        "validate": island.client.post(f"{base}/validate", headers=island.headers()),
        "archive": island.client.delete(base, headers=island.headers()),
    }

    for verb, response in attempts.items():
        if verb == "archive":
            assert response.status_code == 409, f"{verb}: {response.text}"
            assert "already archived" in response.json()["detail"]
        else:
            assert_archived(response, "workflow")

    # And nothing moved: one published version, the same pointer, the same
    # draft, no execution.
    assert len(island.read_workflow("/versions")) == 1
    assert island.read_workflow()["current_version_id"] == published.json()["version_id"]
    assert island.read_workflow()["name"] == "Competitor brief"
    assert island.read_workflow("/executions") == []


def test_the_same_verbs_on_an_unknown_workflow_are_still_404(island: Island) -> None:
    base = f"{island.workflows}/{uuid.uuid4()}"

    for response in (
        island.client.patch(base, json={"name": "Nobody"}, headers=island.headers()),
        island.client.put(
            f"{base}/draft",
            json={"graph": {"steps": [], "edges": []}},
            headers=island.headers(),
        ),
        island.client.post(f"{base}/publish", headers=island.headers()),
        island.client.post(f"{base}/run", json={}, headers=island.headers()),
        island.client.post(f"{base}/validate", headers=island.headers()),
        island.client.delete(base, headers=island.headers()),
    ):
        assert response.status_code == 404, response.text


def test_a_harness_still_gets_the_harness_refusal(island: Island) -> None:
    """Precedence, and the reason it cannot be the other way round.

    A harness is never archived, so it can only ever meet the harness
    refusal - and that refusal says what the workflow *is*, which is the
    more useful of the two sentences.
    """
    started = island.client.post(
        f"{island.agents}/{island.agent_id}/test", json={"input": SAMPLE}, headers=island.headers()
    ).json()
    base = f"{island.workflows}/{started['workflow_id']}"

    for response in (
        island.client.patch(base, json={"name": "Mine now"}, headers=island.headers()),
        island.client.put(
            f"{base}/draft",
            json={"graph": {"steps": [], "edges": []}},
            headers=island.headers(),
        ),
        island.client.post(f"{base}/publish", headers=island.headers()),
        island.client.post(f"{base}/run", json={}, headers=island.headers()),
        island.client.delete(base, headers=island.headers()),
    ):
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert "test harness" in detail
        # Not the archived sentence. Checked on the two state phrases rather
        # than on the word: the harness refusal for `DELETE` says "cannot be
        # archived", which is the verb the caller asked for and not a claim
        # about what the row is.
        assert "is archived" not in detail
        assert "already archived" not in detail


def test_an_archived_workflow_runs_again_once_restored(island: Island) -> None:
    """The whole point of the pair of stages, end to end."""
    island.client.post(f"{island.workflows}/{island.workflow_id}/publish", headers=island.headers())
    island.archive_workflow()
    assert (
        island.client.post(
            f"{island.workflows}/{island.workflow_id}/run", json={}, headers=island.headers()
        ).status_code
        == 409
    )

    assert island.restore_workflow().status_code == 200

    ran = island.client.post(
        f"{island.workflows}/{island.workflow_id}/run", json={}, headers=island.headers()
    )

    assert ran.status_code == 202, ran.text
    assert len(island.read_workflow("/executions")) == 1


def test_a_live_workflow_is_untouched_by_any_of_this(island: Island) -> None:
    """The regression half: nothing above made an ordinary workflow worse."""
    base = f"{island.workflows}/{island.workflow_id}"

    assert (
        island.client.patch(base, json={"name": "Renamed"}, headers=island.headers()).status_code
        == 200
    )
    assert island.client.post(f"{base}/validate", headers=island.headers()).status_code == 200
    assert (
        island.client.put(
            f"{base}/draft",
            json={
                "graph": {
                    "steps": [
                        {"step_key": "research", "kind": "agent_task", "agent_key": "researcher"}
                    ],
                    "edges": [],
                }
            },
            headers=island.headers(),
        ).status_code
        == 200
    )
    assert island.client.post(f"{base}/publish", headers=island.headers()).status_code == 201
    assert island.client.post(f"{base}/run", json={}, headers=island.headers()).status_code == 202
    assert island.client.delete(base, headers=island.headers()).status_code == 204

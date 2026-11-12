"""What an agent's status has been, read back.

Stage D1. `agent_status_history` has been written since migration 0010 - one
row per transition, `from_status` read inside the same INSERT, indexed for
exactly this query - and until now nothing read it. So this file is mostly
about the read: the order, the chain of statuses, the limit, and the three
refusals.

The transitions come from real ingested events rather than seeded rows,
because `from_status` is the thing worth testing and only the reducer
produces it. The volume cases seed directly, which is what
`test_agent_work.py` already does for this table.

**A managed agent has no history, and that is the right answer.** It reports
nothing - its work is in `workflow_executions` - so an empty list here says
where to look rather than hiding a record. That is asserted deliberately,
twice.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_ingest import KEY_HEADER, Fixture, event, post

from agentworld.domain.enums import MemberRole
from agentworld.infra.db import Database
from helpers import Actor, seed_membership, seed_user

pytestmark = pytest.mark.integration


@pytest.fixture
def ingest(client: TestClient, actor: Actor) -> Fixture:
    return Fixture(client, actor)


@pytest.fixture
async def agent(client: TestClient, actor: Actor, ingest: Fixture) -> Any:
    """One agent of the project the ingest key is pinned to.

    Created through the API, like a person would: the agent row is
    configuration, and nothing in the protocol can create one.
    """
    created = client.post(
        f"/v1/projects/{ingest.project_id}/agents",
        json={
            "display_name": "Researcher",
            "agent_key": "researcher",
            "interaction_mode": "managed",
        },
        headers=actor.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


def history(
    client: TestClient,
    ingest: Fixture,
    actor: Actor,
    agent_id: Any,
    query: str = "",
    project_id: Any = None,
) -> Any:
    return client.get(
        f"/v1/projects/{project_id or ingest.project_id}/agents/{agent_id}/status-history{query}",
        headers=actor.headers,
    )


def status_event(status: str, reason: str | None = None, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status}
    if reason is not None:
        payload["reason"] = reason
    return event(type="agent.status_changed", payload=payload, **overrides)


def _second_producer(client: TestClient, actor: Actor) -> dict[str, Any]:
    """A second workspace, project and ingest key, built by hand.

    Not `Fixture` twice: it names its workspace "Ingest", a workspace name
    is a slug, and a slug is unique - so the second one would be refused.
    """
    workspace = client.post("/v1/workspaces", json={"name": "Other ingest"}, headers=actor.headers)
    assert workspace.status_code == 201, workspace.text
    project = client.post(
        f"/v1/workspaces/{workspace.json()['id']}/projects",
        json={"name": "Island"},
        headers=actor.headers,
    ).json()
    minted = client.post(
        f"/v1/workspaces/{workspace.json()['id']}/api-keys",
        json={"name": "Worker", "scope": "ingest", "project_id": project["id"]},
        headers=actor.headers,
    )
    assert minted.status_code == 201, minted.text
    return {"project_id": str(project["id"]), "key": str(minted.json()["key"])}


async def _seed_history(
    owner_database: Database, ingest: Fixture, agent_id: Any, count: int
) -> None:
    """Transitions written straight in, for the cases that need volume.

    The reducer is what the tests above exercise; these rows exist only to
    be counted and ordered, and `changed_at` is spread so the ordering is
    unambiguous without relying on the tiebreak.
    """
    start = dt.datetime.now(dt.UTC) - dt.timedelta(hours=count)
    async with owner_database.session() as session:
        for index in range(count):
            await session.execute(
                text(
                    "INSERT INTO agent_status_history "
                    "(agent_id, workspace_id, project_id, from_status, to_status, changed_at) "
                    "VALUES (:a, :w, :p, 'idle', 'working', :when)"
                ),
                {
                    "a": uuid.UUID(str(agent_id)),
                    "w": ingest.workspace_id,
                    "p": ingest.project_id,
                    "when": start + dt.timedelta(minutes=index),
                },
            )


# ------------------------------------------------------------- the timeline ---


def test_transitions_come_back_newest_first(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any
) -> None:
    """Three events, three rows, newest first, each naming its event.

    The `to_status` sequence is the timeline a reader came for, and `reason`
    travels from the event payload. What `from_status` currently holds is the
    next test's subject, and not a happy story.
    """
    sent = post(
        client,
        ingest,
        status_event("working", "picked up a task"),
        status_event("blocked", "waiting on input"),
        event(type="agent.paused", payload={"reason": "operator"}),
    )
    assert sent.status_code == 202, sent.text

    response = history(client, ingest, actor, agent["id"])

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["to_status"] for row in rows] == ["paused", "blocked", "working"]
    assert rows[0]["reason"] == "operator"
    assert rows[2]["reason"] == "picked up a task"
    assert all(row["changed_at"] for row in rows)
    assert all(row["event_id"] for row in rows), "each row names the event it came from"
    assert rows[0]["changed_at"] >= rows[-1]["changed_at"]


def test_from_status_currently_repeats_to_status_which_is_a_defect(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any
) -> None:
    """A characterisation test: this asserts a bug, deliberately.

    `agent_status_history.from_status` is documented as the status being left
    - NULL for the first transition - and `derived._append_status` reads it
    inside the INSERT precisely so that no window exists in which it could
    change. But `fold` returns `(Patch(status=new), Append(history))` in that
    order, and `ingest` applies mutations in order, so the agent row already
    carries the new status when the history row reads it. Every row therefore
    says it came from where it went.

    Stage D1 found this because D1 is the first code ever to read the
    column, and D1's brief forbids changing status folding - so the defect is
    recorded here rather than fixed. The fix is to emit the `Append` before
    the `Patch`; when that happens, this test must be inverted and the
    chain asserted properly (`["blocked", "working", None]` for the stream
    in the test above).
    """
    post(client, ingest, status_event("idle"))

    [row] = history(client, ingest, actor, agent["id"]).json()

    assert row["to_status"] == "idle"
    assert row["from_status"] == "idle", "the defect: 'provisioning' is the honest answer"


def test_a_managed_agent_that_reports_nothing_has_no_history(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any
) -> None:
    """200 and an empty list, deliberately.

    An agent AgentWorld runs itself sends no events, so there is nothing to
    fold. Its work is in `workflow_executions`; inventing a row here to make
    the timeline look populated would be a second source of truth for what
    an agent has been doing.
    """
    response = history(client, ingest, actor, agent["id"])

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_a_test_run_does_not_appear_in_history(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any
) -> None:
    """The same point from the other side: running the agent changes nothing.

    `agent.test` publishes a harness and starts a real execution. None of
    that is a status transition the agent reported, so the timeline stays
    empty and the execution list is where the run shows up.
    """
    tested = client.post(
        f"/v1/projects/{ingest.project_id}/agents/{agent['id']}/test",
        json={"input": "Summarise the fintech funding news."},
        headers=actor.headers,
    )
    assert tested.status_code == 202, tested.text

    assert history(client, ingest, actor, agent["id"]).json() == []
    listed = client.get(
        f"/v1/projects/{ingest.project_id}/executions", headers=actor.headers
    ).json()
    assert [row["id"] for row in listed] == [tested.json()["id"]]


# --------------------------------------------------------------- the limit ---


async def test_the_default_limit_returns_the_newest_fifty(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any, owner_database: Database
) -> None:
    """Capped, because history grows with every transition an agent reports.

    The oldest row is absent at the default, which is the half of the claim
    that a count alone would not prove.
    """
    await _seed_history(owner_database, ingest, agent["id"], 60)

    rows = history(client, ingest, actor, agent["id"]).json()

    assert len(rows) == 50
    oldest = sorted(row["changed_at"] for row in rows)[0]
    all_rows = history(client, ingest, actor, agent["id"], "?limit=200").json()
    assert len(all_rows) == 60
    assert sorted(row["changed_at"] for row in all_rows)[0] < oldest


async def test_an_explicit_limit_is_honoured_and_bounded(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any, owner_database: Database
) -> None:
    await _seed_history(owner_database, ingest, agent["id"], 12)

    assert len(history(client, ingest, actor, agent["id"], "?limit=5").json()) == 5
    assert history(client, ingest, actor, agent["id"], "?limit=0").status_code == 422
    assert history(client, ingest, actor, agent["id"], "?limit=201").status_code == 422
    assert history(client, ingest, actor, agent["id"], "?limit=notanumber").status_code == 422


# ------------------------------------------------------------ the refusals ---


def test_an_unknown_agent_is_a_404(client: TestClient, ingest: Fixture, actor: Actor) -> None:
    """Not an empty list, which would say the agent has never changed status."""
    assert history(client, ingest, actor, uuid.uuid4()).status_code == 404


def test_another_projects_agent_is_a_404(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any
) -> None:
    """Same workspace, same caller, wrong project.

    The repository filters on `project_id` as well as the agent id, so an
    agent the caller may genuinely read elsewhere is not readable through
    this project's path.
    """
    elsewhere = client.post(
        f"/v1/workspaces/{ingest.workspace_id}/projects",
        json={"name": "Other island"},
        headers=actor.headers,
    ).json()

    response = history(client, ingest, actor, agent["id"], project_id=elsewhere["id"])

    assert response.status_code == 404, response.text


def test_an_archived_agent_keeps_its_history(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any
) -> None:
    """A read, so archiving does not hide it.

    What an agent did before it was archived is precisely what a reader is
    asking about - and stage C2's 409 is for writes, not for this.
    """
    post(client, ingest, status_event("working"))
    archived = client.delete(
        f"/v1/projects/{ingest.project_id}/agents/{agent['id']}", headers=actor.headers
    )
    assert archived.status_code == 204, archived.text

    response = history(client, ingest, actor, agent["id"])

    assert response.status_code == 200, response.text
    assert [row["to_status"] for row in response.json()] == ["working"]


def test_a_stranger_sees_nothing(
    client: TestClient, ingest: Fixture, actor: Actor, agent: Any, make_actor: Any
) -> None:
    """404: RLS hides the project, so the refusal cannot confirm the agent."""
    post(client, ingest, status_event("working"))
    stranger = make_actor("history-outsider@example.com")

    assert history(client, ingest, stranger, agent["id"]).status_code == 404


async def test_a_viewer_may_read_the_timeline(
    client: TestClient,
    ingest: Fixture,
    actor: Actor,
    agent: Any,
    owner_database: Database,
    make_actor: Any,
) -> None:
    """`agent.read_history` is `viewer`: watching is not operating."""
    post(client, ingest, status_event("working"))
    viewer = make_actor("history-viewer@example.com")
    await seed_user(owner_database, viewer.user_id, viewer.email)
    await seed_membership(owner_database, ingest.workspace_id, viewer.user_id, MemberRole.VIEWER)

    response = history(client, ingest, viewer, agent["id"])

    assert response.status_code == 200, response.text
    assert [row["to_status"] for row in response.json()] == ["working"]


async def test_another_workspaces_history_is_invisible(
    client: TestClient,
    ingest: Fixture,
    actor: Actor,
    agent: Any,
    owner_database: Database,
) -> None:
    """Tenant isolation, asserted on the rows rather than on the route.

    A second workspace's agent carries its own transitions; the read is
    scoped to this project, and the counts prove the rows did not mix.
    """
    post(client, ingest, status_event("working"))
    other = _second_producer(client, actor)
    theirs = client.post(
        f"/v1/projects/{other['project_id']}/agents",
        json={
            "display_name": "Researcher",
            "agent_key": "researcher",
            "interaction_mode": "managed",
        },
        headers=actor.headers,
    ).json()
    sent = client.post(
        "/v1/ingest/events",
        json={"events": [status_event("blocked", dedup_key=f"o-{uuid.uuid4()}")]},
        headers={KEY_HEADER: other["key"]},
    )
    assert sent.status_code == 202, sent.text

    mine = history(client, ingest, actor, agent["id"]).json()
    other_rows = client.get(
        f"/v1/projects/{other['project_id']}/agents/{theirs['id']}/status-history",
        headers=actor.headers,
    ).json()

    assert [row["to_status"] for row in mine] == ["working"]
    assert [row["to_status"] for row in other_rows] == ["blocked"]

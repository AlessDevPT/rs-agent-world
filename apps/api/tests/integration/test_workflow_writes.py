"""Renaming a workflow, and the four things that must not change with it.

Stage B2a. The metadata PATCH writes the `workflows` row and nothing else,
so the tests here are as much about what stays put - the graph, the pointer,
the slug, a published version - as about the name that moves.

`workflow.update` is its own permission rather than `workflow.update_draft`:
renaming a workflow changes what it is called and nothing about what it
would do. Both are `member` today, which is exactly why the two have to be
separable before one of them stops being.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_agent_test_harness import Harness
from test_executions_api import Scenario

from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(client: TestClient, actor: Actor) -> Scenario:
    return Scenario(client, actor)


def patch(scenario: Scenario, workflow_id: str, body: Any) -> Any:
    return scenario.client.patch(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}",
        json=body,
        headers=scenario.actor.headers,
    )


def read(scenario: Scenario, workflow_id: str, suffix: str = "") -> Any:
    return scenario.client.get(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}{suffix}",
        headers=scenario.actor.headers,
    )


async def _audit(owner_database: Database, workspace_id: str, action: str) -> list[Any]:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT resource_id, metadata FROM audit_log "
                    "WHERE workspace_id = :w AND action = :a ORDER BY created_at, id"
                ),
                {"w": uuid.UUID(workspace_id), "a": action},
            )
        ).all()


# --------------------------------------------------------------- the rename ---


def test_a_workflow_is_renamed_and_re_described(scenario: Scenario) -> None:
    workflow_id = scenario.workflow(name="Competitor brief")

    response = patch(scenario, workflow_id, {"name": "Rival brief", "description": "Weekly."})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["name"], body["description"]) == ("Rival brief", "Weekly.")
    assert read(scenario, workflow_id).json() == body


def test_the_slug_does_not_follow_the_name(scenario: Scenario) -> None:
    """Identity, not a label. The same rule `agent_key` has."""
    workflow_id = scenario.workflow(name="Competitor brief")

    patch(scenario, workflow_id, {"name": "Something else entirely"})

    assert read(scenario, workflow_id).json()["slug"] == "competitor-brief"


def test_an_absent_field_is_left_alone_and_an_explicit_null_clears_it(
    scenario: Scenario,
) -> None:
    """The distinction `exclude_unset` exists for."""
    workflow_id = scenario.workflow(name="Keeper")
    patch(scenario, workflow_id, {"description": "Still here."})

    renamed = patch(scenario, workflow_id, {"name": "Renamed"}).json()
    assert renamed["description"] == "Still here.", "absent means leave it alone"

    cleared = patch(scenario, workflow_id, {"description": None}).json()
    assert cleared["description"] is None
    assert cleared["name"] == "Renamed", "clearing one field left the other"


def test_an_empty_body_changes_nothing(scenario: Scenario) -> None:
    workflow_id = scenario.workflow(name="Untouched")

    response = patch(scenario, workflow_id, {})

    assert response.status_code == 200, response.text
    assert response.json() == read(scenario, workflow_id).json()


# ------------------------------------------------------- what cannot change ---


def test_the_slug_and_the_graph_are_refused_by_name(scenario: Scenario) -> None:
    """422, not silence: a client that sent a graph here believes it changed
    what the workflow runs."""
    workflow_id = scenario.workflow()

    for body in (
        {"slug": "something-else"},
        {"graph": {"steps": [], "edges": []}},
        {"current_version_id": str(uuid.uuid4())},
        {"enabled": True},
        {"archived_at": None},
        {"test_for_agent_id": str(uuid.uuid4())},
    ):
        response = patch(scenario, workflow_id, {"name": "Fine", **body})
        assert response.status_code == 422, f"{body}: {response.text}"
        assert next(iter(body)) in response.text


def test_renaming_leaves_the_published_version_and_the_pointer_alone(
    scenario: Scenario,
) -> None:
    """The whole point of a metadata write: the frozen half does not move.

    A published version is what a running execution means, and 0018 makes it
    immutable - but the first line of defence is that this endpoint cannot
    reach it. The draft, the version id, the pointer and the history are all
    compared before and after.
    """
    workflow_id = scenario.workflow(name="Before")
    version_id = str(scenario.publish(workflow_id).json()["version_id"])
    started = scenario.run(workflow_id).json()
    before = (
        read(scenario, workflow_id, "/draft").json(),
        read(scenario, workflow_id, "/versions").json(),
    )

    patch(scenario, workflow_id, {"name": "After", "description": "Renamed mid-flight."})

    assert read(scenario, workflow_id).json()["current_version_id"] == version_id
    assert (
        read(scenario, workflow_id, "/draft").json(),
        read(scenario, workflow_id, "/versions").json(),
    ) == before
    execution = scenario.client.get(
        f"/v1/projects/{scenario.project_id}/executions/{started['id']}",
        headers=scenario.actor.headers,
    ).json()
    assert execution["workflow_version_id"] == version_id


# ------------------------------------------------------------- the refusals ---


def test_an_unknown_workflow_is_a_404(scenario: Scenario) -> None:
    assert patch(scenario, str(uuid.uuid4()), {"name": "Nobody"}).status_code == 404


def test_an_archived_workflow_cannot_be_renamed(scenario: Scenario) -> None:
    """409 naming the remedy, while the read still answers 200.

    This asserted a 404 until stage C2. There was nothing a caller could do
    about an archived workflow then, so "not found" was only unhelpful;
    now that it can be restored, the refusal says which state it is in and
    what to do about it.
    """
    workflow_id = scenario.workflow()
    scenario.client.delete(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}",
        headers=scenario.actor.headers,
    )

    refused = patch(scenario, workflow_id, {"name": "Back from the dead"})
    assert refused.status_code == 409, refused.text
    assert "archived" in refused.json()["detail"]
    assert "restored" in refused.json()["detail"]
    assert read(scenario, workflow_id).status_code == 200, "still readable"


def test_another_projects_workflow_cannot_be_renamed(
    scenario: Scenario, client: TestClient, actor: Actor
) -> None:
    """Same workspace, same caller, wrong project: 404 and no write."""
    workflow_id = scenario.workflow(name="Mine")
    elsewhere = client.post(
        f"/v1/workspaces/{scenario.workspace_id}/projects",
        json={"name": "Other island"},
        headers=actor.headers,
    ).json()

    response = client.patch(
        f"/v1/projects/{elsewhere['id']}/workflows/{workflow_id}",
        json={"name": "Theirs"},
        headers=actor.headers,
    )

    assert response.status_code == 404, response.text
    assert read(scenario, workflow_id).json()["name"] == "Mine"


def test_a_stranger_cannot_rename_anything(scenario: Scenario, make_actor: Any) -> None:
    workflow_id = scenario.workflow(name="Mine")
    stranger = make_actor("outsider@example.com")

    response = scenario.client.patch(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}",
        json={"name": "Theirs"},
        headers=stranger.headers,
    )

    assert response.status_code == 404, response.text
    assert read(scenario, workflow_id).json()["name"] == "Mine"


def test_a_harness_cannot_be_renamed(client: TestClient, actor: Actor) -> None:
    """409 naming the agent, the same refusal the draft editor gives.

    A harness's name came from the compiler. A renamed one would be a
    workflow claiming to be something `agent.test` did not compile.
    """
    harness = Harness(client, actor)
    started = client.post(
        f"/v1/projects/{harness.project_id}/agents/{harness.agent_id}/test",
        json={"input": "Summarise the fintech funding news."},
        headers=actor.headers,
    ).json()
    base = f"/v1/projects/{harness.project_id}/workflows/{started['workflow_id']}"
    before = client.get(base, headers=actor.headers).json()

    refused = client.patch(base, json={"name": "Mine now"}, headers=actor.headers)

    assert refused.status_code == 409, refused.text
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert "test harness" in refused.json()["detail"]
    # The agent's id is in the log, not the body: `LOCATOR_KEYS` carries
    # `field` and `index` and nothing a caller cannot work out for itself.
    assert "agent_id" not in refused.json()
    assert client.get(base, headers=actor.headers).json() == before


# ------------------------------------------------------------------- audit ---


async def test_one_audit_row_naming_the_fields_and_not_their_values(
    scenario: Scenario, owner_database: Database
) -> None:
    """`workflow.update`, carrying field names only.

    A description can be long, and the audit log answers "who changed what"
    rather than "what did it say" - the same shape `agent.update` records.
    """
    workflow_id = scenario.workflow(name="Before")
    patch(scenario, workflow_id, {"name": "After", "description": "A sentence nobody logs."})

    rows = await _audit(owner_database, scenario.workspace_id, "workflow.update")

    assert len(rows) == 1
    assert str(rows[0].resource_id) == workflow_id
    assert rows[0].metadata == {"fields": ["description", "name"]}
    assert "A sentence nobody logs." not in str(rows[0].metadata)


async def test_a_refused_rename_records_nothing(
    scenario: Scenario, client: TestClient, actor: Actor, owner_database: Database
) -> None:
    """The guard is raised inside the transaction, so there is no row for
    something that did not happen."""
    harness = Harness(client, actor)
    started = client.post(
        f"/v1/projects/{harness.project_id}/agents/{harness.agent_id}/test",
        json={"input": "Summarise the fintech funding news."},
        headers=actor.headers,
    ).json()
    refused = client.patch(
        f"/v1/projects/{harness.project_id}/workflows/{started['workflow_id']}",
        json={"name": "Mine now"},
        headers=actor.headers,
    )
    assert refused.status_code == 409

    assert await _audit(owner_database, str(harness.workspace_id), "workflow.update") == []

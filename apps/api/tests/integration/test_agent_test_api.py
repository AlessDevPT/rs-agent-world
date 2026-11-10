"""`POST /v1/projects/{id}/agents/{id}/test` over real HTTP.

S5. The service is tested in `test_agent_test_service.py`; this file is about
the route: the status code, the body it accepts and refuses, the view it
returns, and the fact that the answer is read back through the **existing**
execution read rather than through anything new.

The role matrix covers who may call it, in `test_role_matrix.py`, where a row
is mandatory - the coverage guard there fails for any route without one.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_agent_test_harness import Harness

from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration

SAMPLE = "Summarise the fintech funding news."

#: What building the fixture through the API records before any of these
#: tests acts: a workspace, a project and an agent, each its own action.
#: Spelled out so the audit assertions below can compare the *whole* log
#: and still say which rows are the fixture's.
SETUP = {"workspace.create": 1, "project.create": 1, "agent.create": 1}


@pytest.fixture
def harness(client: TestClient, actor: Actor) -> Harness:
    return Harness(client, actor)


def post_test(harness: Harness, actor: Actor, body: Any, agent_id: str | None = None) -> Any:
    return harness.client.post(
        f"/v1/projects/{harness.project_id}/agents/{agent_id or harness.agent_id}/test",
        json=body,
        headers=actor.headers,
    )


async def _actions(owner_database: Database, workspace_id: Any) -> dict[str, int]:
    """Every audit action recorded in this workspace, counted by name."""
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text("SELECT action FROM audit_log WHERE workspace_id = :w"),
                {"w": workspace_id},
            )
        ).all()
    counted: dict[str, int] = {}
    for (action,) in rows:
        counted[action] = counted.get(action, 0) + 1
    return counted


def test_a_test_returns_202_and_the_execution(harness: Harness, actor: Actor) -> None:
    """202, and the ordinary `ExecutionView`.

    Not 201: the execution exists when this returns and the turn has not
    happened yet, which is the same reason `POST .../run` is 202. The view is
    the one the execution routes return, so a client has one shape for both.
    """
    response = post_test(harness, actor, {"input": SAMPLE})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["input"] == {"input": SAMPLE}
    assert body["status"] in ("pending", "running")
    assert body["initiator_kind"] == "user"
    assert body["initiator_label"] == actor.email
    assert body["workflow_id"] and body["workflow_version_id"]
    assert body["output"] is None, "nothing has run yet"


def test_the_answer_is_read_through_the_existing_execution_endpoint(
    harness: Harness, actor: Actor
) -> None:
    """No second result API, which is the whole of decision 6.

    The execution the test started is readable by id through the route that
    already exists, with the same fields - including `output`, which is where
    the agent's answer lands once a worker has performed the turn. That last
    step is `test_agent_test_execution.py`'s; what is asserted here is that
    no new endpoint is needed to reach it.
    """
    started = post_test(harness, actor, {"input": SAMPLE}).json()

    read = harness.client.get(
        f"/v1/projects/{harness.project_id}/executions/{started['id']}",
        headers=actor.headers,
    )
    assert read.status_code == 200, read.text
    assert read.json() == started

    listed = harness.client.get(
        f"/v1/projects/{harness.project_id}/executions", headers=actor.headers
    )
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [started["id"]]


def test_the_harness_it_created_is_not_in_the_workflow_list(harness: Harness, actor: Actor) -> None:
    """Over HTTP, because the listing a person sees is the one that matters."""
    started = post_test(harness, actor, {"input": SAMPLE}).json()

    listed = harness.client.get(
        f"/v1/projects/{harness.project_id}/workflows", headers=actor.headers
    )
    assert listed.status_code == 200
    assert listed.json() == []

    # Reachable by id, though: publishing and running it is how a test
    # happens, and the execution names it.
    one = harness.client.get(
        f"/v1/projects/{harness.project_id}/workflows/{started['workflow_id']}/draft",
        headers=actor.headers,
    )
    assert one.status_code == 200, one.text


def test_editing_the_harness_through_the_draft_api_is_refused(
    harness: Harness, actor: Actor
) -> None:
    """409 naming the agent, over HTTP, with a problem document."""
    started = post_test(harness, actor, {"input": SAMPLE}).json()

    refused = harness.client.put(
        f"/v1/projects/{harness.project_id}/workflows/{started['workflow_id']}/draft",
        json={"graph": {"steps": [], "edges": []}},
        headers=actor.headers,
    )
    assert refused.status_code == 409, refused.text
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert "test harness" in refused.json()["detail"]


def test_publishing_the_harness_through_the_workflow_api_is_refused(
    harness: Harness, actor: Actor
) -> None:
    """409, and the draft the next test would run is still the draft it was.

    The public publish route was the one way a person could give a harness a
    second published version - S4's version churn, arriving from outside the
    service S4 fixed. The harness is not hidden from this route: it is
    readable by id and its draft is readable, as the listing test above
    shows, so the refusal is what stands in the way and not obscurity.
    """
    started = post_test(harness, actor, {"input": SAMPLE}).json()
    draft_url = f"/v1/projects/{harness.project_id}/workflows/{started['workflow_id']}/draft"
    before = harness.client.get(draft_url, headers=actor.headers).json()

    refused = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows/{started['workflow_id']}/publish",
        headers=actor.headers,
    )

    assert refused.status_code == 409, refused.text
    assert refused.headers["content-type"].startswith("application/problem+json")
    detail = refused.json()["detail"]
    assert "test harness" in detail
    assert "cannot be published" in detail
    assert "workflow_versions" not in refused.text, "no schema detail in a refusal"

    after = harness.client.get(draft_url, headers=actor.headers).json()
    assert (after["id"], after["version"]) == (before["id"], before["version"]), (
        "the draft was frozen after all"
    )


def test_running_the_harness_through_the_workflow_api_is_refused(
    harness: Harness, actor: Actor
) -> None:
    """409, and no second execution.

    A harness run from here would be an execution of a workflow nobody drew,
    outside the four actions `agent.test` records and with nothing saying who
    asked for it. The refusal is checked before the published-version check,
    so it reads the same whether or not a test has ever run.
    """
    started = post_test(harness, actor, {"input": SAMPLE}).json()

    refused = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows/{started['workflow_id']}/run",
        json={"input": {"input": SAMPLE}},
        headers=actor.headers,
    )

    assert refused.status_code == 409, refused.text
    assert refused.headers["content-type"].startswith("application/problem+json")
    detail = refused.json()["detail"]
    assert "test harness" in detail
    assert "cannot be run" in detail

    listed = harness.client.get(
        f"/v1/projects/{harness.project_id}/executions", headers=actor.headers
    )
    assert [row["id"] for row in listed.json()] == [started["id"]], "a run got through"


async def test_the_refusals_record_nothing_and_leave_agent_test_working(
    harness: Harness, actor: Actor, owner_database: Database
) -> None:
    """The refusals are not a half-done publish, and they change nothing.

    Two claims in one test, because they are the same claim from both sides.
    A refused call writes no audit row - the guard is raised before
    `audit.record` and inside the transaction, so there is no `workflow.publish`
    a digest could report for something that did not happen. And the service
    path is untouched: a second test still runs, still on the version the
    first one published, because `publish_test_harness` and
    `start_test_harness` are the same body with only the harness refusal
    lifted.
    """
    first = post_test(harness, actor, {"input": SAMPLE}).json()
    workflow_id = first["workflow_id"]

    assert await _actions(owner_database, harness.workspace_id) == SETUP | {
        "workflow.create": 1,
        "workflow.publish": 1,
        "agent.test": 1,
        "workflow.run": 1,
    }

    for url, body in (
        (f"/v1/projects/{harness.project_id}/workflows/{workflow_id}/publish", None),
        (f"/v1/projects/{harness.project_id}/workflows/{workflow_id}/run", {"input": {}}),
    ):
        assert harness.client.post(url, json=body, headers=actor.headers).status_code == 409

    assert await _actions(owner_database, harness.workspace_id) == SETUP | {
        "workflow.create": 1,
        "workflow.publish": 1,
        "agent.test": 1,
        "workflow.run": 1,
    }, "a refused call recorded something"

    second = post_test(harness, actor, {"input": SAMPLE})
    assert second.status_code == 202, second.text
    assert second.json()["workflow_version_id"] == first["workflow_version_id"], (
        "the second test should run the version the first one published"
    )
    assert await _actions(owner_database, harness.workspace_id) == SETUP | {
        "workflow.create": 1,
        # Still one: `agent.test` publishes only while the pointer is NULL,
        # and the refusals above did not move it.
        "workflow.publish": 1,
        "agent.test": 2,
        "workflow.run": 2,
    }


def test_an_ordinary_workflow_is_still_published_and_run(harness: Harness, actor: Actor) -> None:
    """The guard reads the marker, not the verb: a drawn workflow is unaffected.

    `test_executions_api.py` is where publish and run are tested properly.
    This is the one assertion that belongs *here*: that the refusal added for
    harnesses did not arrive as a refusal for everybody.
    """
    created = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows",
        json={
            "name": "Drawn by hand",
            "graph": {
                "steps": [
                    {
                        "step_key": "writer",
                        "kind": "agent_task",
                        "agent_key": "writer",
                        "input_mapping": {"input": "$.input.input"},
                    }
                ],
                "edges": [],
            },
        },
        headers=actor.headers,
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["id"]

    published = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows/{workflow_id}/publish",
        headers=actor.headers,
    )
    assert published.status_code == 201, published.text

    ran = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows/{workflow_id}/run",
        json={"input": {"input": SAMPLE}},
        headers=actor.headers,
    )
    assert ran.status_code == 202, ran.text
    assert ran.json()["workflow_version_id"] == published.json()["version_id"]


def test_an_empty_sample_is_refused(harness: Harness, actor: Actor) -> None:
    """A test with nothing in it would spend money to ask nothing."""
    assert post_test(harness, actor, {"input": ""}).status_code == 422
    assert post_test(harness, actor, {}).status_code == 422


def test_configuration_cannot_be_overridden_for_one_run(harness: Harness, actor: Actor) -> None:
    """`extra="forbid"`: a test runs the agent *as configured*.

    A client that sends a model or a system prompt here is asking a different
    question from the one this endpoint answers, and being told so is better
    than having it silently ignored.
    """
    response = post_test(harness, actor, {"input": SAMPLE, "model_id": "something-else"})
    assert response.status_code == 422, response.text


def test_an_observed_agent_is_refused_with_findings(harness: Harness, actor: Actor) -> None:
    """422 carrying the compiler's finding, in the `errors` array.

    The same shape a publish refusal uses, so a client has one parser for
    both - which is why `AgentNotTestableError` is a subclass rather than a
    sibling.
    """
    observed = harness.client.post(
        f"/v1/projects/{harness.project_id}/agents",
        json={
            "display_name": "Watcher",
            "agent_key": "watcher",
            "interaction_mode": "observed",
        },
        headers=actor.headers,
    ).json()

    response = post_test(harness, actor, {"input": SAMPLE}, agent_id=observed["id"])

    assert response.status_code == 422, response.text
    problem = response.json()
    assert problem["title"] == "Agent not testable"
    assert [error["code"] for error in problem["errors"]] == ["observed_agent"]
    assert problem["errors"][0]["pointer"] == f"/agents/{observed['id']}"


def test_an_unknown_agent_is_a_404(harness: Harness, actor: Actor) -> None:
    response = post_test(
        harness, actor, {"input": SAMPLE}, agent_id="00000000-0000-0000-0000-00000000dead"
    )
    assert response.status_code == 404, response.text


def test_the_route_is_project_nested_like_every_other_agent_route(
    harness: Harness, actor: Actor
) -> None:
    """The flat `/v1/agents/{id}/test` of the specification is not served.

    `03-API-AND-FRONTEND.md` writes agent routes flat; every implemented one
    is nested under its project, because the project is what scopes the read.
    This asserts the implementation's form rather than leaving the mismatch to
    be discovered by a client.
    """
    flat = harness.client.post(
        f"/v1/agents/{harness.agent_id}/test",
        json={"input": SAMPLE},
        headers=actor.headers,
    )
    assert flat.status_code == 404
    schema = harness.client.app.openapi()  # type: ignore[attr-defined]
    assert "/v1/projects/{project_id}/agents/{agent_id}/test" in schema["paths"]
    assert "/v1/agents/{agent_id}/test" not in schema["paths"]

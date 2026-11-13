"""What an agent has done, read back - from both of the places it is recorded.

Stage D2, and the whole of it is that there are two places and they are not
the same place.

`runs` is what an **external** agent reports about work it did wherever it
lives: folded from its own events, one row per attempt, with the trace in
`run_steps`. `workflow_executions` is AgentWorld's own record of work it
drove itself, reached from an agent through the step rows bound to it. The
brief for this stage said not to merge the two into a common "activity"
record, and these are two endpoints for that reason: a reader who cannot tell
a self-report from a first-hand record cannot tell which one to trust.

**The read tests seed `runs` directly, and that is still deliberate.** R1
gave the table a producer - `run.started` creates a row, and
`test_run_producer.py` covers it end to end - but these tests are about the
read: its order, its limit, its refusals and its isolation. Seeding is how
they get a dozen runs with unambiguous timestamps without a dozen ingest
round trips, exactly as `test_agent_work.py` already does for this table.
One test here does go through ingest, because the thing R1 changed is that
this endpoint returns rows at all.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_executions_api import Scenario
from test_ingest import Fixture, event, post

from agentworld.domain.enums import MemberRole
from agentworld.infra.db import Database
from helpers import Actor, seed_membership, seed_user

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(client: TestClient, actor: Actor) -> Scenario:
    return Scenario(client, actor)


# ------------------------------------------------------------------ helpers ---


def runs(scenario: Scenario, agent_id: Any, query: str = "", project_id: Any = None) -> Any:
    return scenario.client.get(
        f"/v1/projects/{project_id or scenario.project_id}/agents/{agent_id}/runs{query}",
        headers=scenario.actor.headers,
    )


def executions(scenario: Scenario, agent_id: Any, query: str = "", project_id: Any = None) -> Any:
    return scenario.client.get(
        f"/v1/projects/{project_id or scenario.project_id}/agents/{agent_id}/executions{query}",
        headers=scenario.actor.headers,
    )


def second_agent(scenario: Scenario, key: str = "reviewer") -> str:
    created = scenario.client.post(
        f"/v1/projects/{scenario.project_id}/agents",
        json={
            "display_name": key.title(),
            "agent_key": key,
            "interaction_mode": "managed",
        },
        headers=scenario.actor.headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def second_project(scenario: Scenario) -> tuple[str, str]:
    """Another project in the same workspace, with an agent of its own.

    Not a second `Scenario`: that names its workspace "Runner", a workspace
    name is a slug, and a slug is unique - so the second one is refused.
    """
    project = scenario.client.post(
        f"/v1/workspaces/{scenario.workspace_id}/projects",
        json={"name": "Mainland"},
        headers=scenario.actor.headers,
    )
    assert project.status_code == 201, project.text
    project_id = str(project.json()["id"])
    agent = scenario.client.post(
        f"/v1/projects/{project_id}/agents",
        json={
            "display_name": "Researcher",
            "agent_key": "researcher",
            "interaction_mode": "managed",
        },
        headers=scenario.actor.headers,
    )
    assert agent.status_code == 201, agent.text
    return project_id, str(agent.json()["id"])


def two_steps_one_agent() -> dict[str, Any]:
    """One agent, twice in one graph.

    The dedup case: `execution_steps` gets a row per step, so without the
    `EXISTS` this execution would be listed twice for one agent.
    """
    return {
        "steps": [
            {"step_key": "research", "kind": "agent_task", "agent_key": "researcher"},
            {"step_key": "review", "kind": "agent_task", "agent_key": "researcher"},
        ],
        "edges": [{"from": "research", "to": "review"}],
    }


def no_agent_graph() -> dict[str, Any]:
    """A graph with nothing bound to an agent, so no agent may claim it."""
    return {
        "steps": [{"step_key": "shape", "kind": "transform", "input_mapping": {"n": "$.input.n"}}],
        "edges": [],
    }


def started(scenario: Scenario, graph: dict[str, Any] | None = None, name: str = "Flow") -> str:
    """Publish a workflow and run it, returning the execution id."""
    workflow_id = scenario.workflow(graph, name=name)
    published = scenario.publish(workflow_id)
    assert published.status_code == 201, published.text
    running = scenario.run(workflow_id)
    assert running.status_code == 202, running.text
    return str(running.json()["id"])


async def seed_runs(
    owner_database: Database,
    scenario: Scenario,
    agent_id: Any,
    count: int,
    *,
    finished: bool = False,
) -> list[str]:
    """Runs written straight in, because nothing in the application writes one.

    `started_at` is spread so the ordering is unambiguous without relying on
    the `id` tiebreak, and returned newest first - the order the endpoint is
    expected to produce.
    """
    base = dt.datetime.now(dt.UTC) - dt.timedelta(hours=count + 1)
    created: list[str] = []
    async with owner_database.session() as session:
        for index in range(count):
            run_id = uuid.uuid4()
            when = base + dt.timedelta(minutes=index)
            await session.execute(
                text(
                    "INSERT INTO runs (id, workspace_id, project_id, agent_id, run_ref, "
                    "status, step_count, tool_call_count, started_at, finished_at) "
                    "VALUES (:i, :w, :p, :a, :ref, :status, :steps, :tools, :started, :ended)"
                ),
                {
                    "i": run_id,
                    "w": uuid.UUID(scenario.workspace_id),
                    "p": uuid.UUID(scenario.project_id),
                    "a": uuid.UUID(str(agent_id)),
                    "ref": f"run-{uuid.uuid4().hex[:8]}",
                    "status": "succeeded" if finished else "running",
                    "steps": index,
                    "tools": index * 2,
                    "started": when,
                    "ended": when + dt.timedelta(seconds=30) if finished else None,
                },
            )
            created.append(str(run_id))
    return list(reversed(created))


async def seed_retry(owner_database: Database, execution_id: str) -> None:
    """A second attempt at a step that already has one.

    Copied from the row that is there, so the composite keys hold: the same
    execution, the same version, the same step, the next attempt. This is
    what a retry leaves behind, and what would duplicate the execution in a
    listing that joined instead of asking whether the agent is in it.
    """
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO execution_steps (id, execution_id, workspace_id, project_id, "
                "workflow_version_id, workflow_step_id, step_key, status, attempt, agent_id) "
                "SELECT gen_random_uuid(), execution_id, workspace_id, project_id, "
                "workflow_version_id, workflow_step_id, step_key, 'running', attempt + 1, "
                "agent_id FROM execution_steps "
                "WHERE execution_id = :e AND agent_id IS NOT NULL LIMIT 1"
            ),
            {"e": uuid.UUID(execution_id)},
        )


# ---------------------------------------------------------------- the runs ---


async def test_an_agents_runs_come_back_newest_first(
    scenario: Scenario, owner_database: Database
) -> None:
    expected = await seed_runs(owner_database, scenario, scenario.agent["id"], 3)

    response = runs(scenario, scenario.agent["id"])

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == expected


async def test_a_run_carries_its_counters_and_the_database_computed_duration(
    scenario: Scenario, owner_database: Database
) -> None:
    """Every field is read, none is derived here.

    `duration_ms` is a generated column, so a value coming back at all is
    the database's arithmetic rather than the API's - which is why no code
    may write it.
    """
    await seed_runs(owner_database, scenario, scenario.agent["id"], 1, finished=True)

    row = runs(scenario, scenario.agent["id"]).json()[0]

    assert row["status"] == "succeeded"
    assert row["step_count"] == 0
    assert row["tool_call_count"] == 0
    assert row["run_ref"].startswith("run-")
    assert row["duration_ms"] == 30_000
    assert row["finished_at"] is not None
    assert row["task_id"] is None
    assert row["execution_step_id"] is None


async def test_a_run_in_flight_has_no_duration(
    scenario: Scenario, owner_database: Database
) -> None:
    await seed_runs(owner_database, scenario, scenario.agent["id"], 1)

    row = runs(scenario, scenario.agent["id"]).json()[0]

    assert row["status"] == "running"
    assert row["finished_at"] is None
    assert row["duration_ms"] is None


async def test_another_agents_runs_never_appear(
    scenario: Scenario, owner_database: Database
) -> None:
    mine = await seed_runs(owner_database, scenario, scenario.agent["id"], 2)
    other = second_agent(scenario)
    theirs = await seed_runs(owner_database, scenario, other, 2)

    assert [row["id"] for row in runs(scenario, scenario.agent["id"]).json()] == mine
    assert [row["id"] for row in runs(scenario, other).json()] == theirs


async def test_the_newest_runs_are_returned_up_to_the_limit(
    scenario: Scenario, owner_database: Database
) -> None:
    expected = await seed_runs(owner_database, scenario, scenario.agent["id"], 5)

    listed = runs(scenario, scenario.agent["id"], "?limit=2").json()

    assert [row["id"] for row in listed] == expected[:2], "the newest two, not the oldest"


async def test_the_limit_is_bounded_at_both_ends(scenario: Scenario) -> None:
    """The same bounds as the status history, and for the same reason.

    A run is written per attempt, so this listing grows with use rather than
    with something a person did deliberately.
    """
    assert runs(scenario, scenario.agent["id"], "?limit=0").status_code == 422
    assert runs(scenario, scenario.agent["id"], "?limit=201").status_code == 422
    assert runs(scenario, scenario.agent["id"], "?limit=200").status_code == 200


def test_a_managed_agent_has_no_runs_and_none_is_invented(scenario: Scenario) -> None:
    """The empty list is the answer, and the route says where to look instead.

    A managed agent's work is in `workflow_executions`; nothing here
    synthesises a run record from an execution to fill the gap.
    """
    started(scenario)

    assert runs(scenario, scenario.agent["id"]).json() == []


async def test_an_archived_agent_keeps_its_runs(
    scenario: Scenario, owner_database: Database
) -> None:
    expected = await seed_runs(owner_database, scenario, scenario.agent["id"], 2)

    archived = scenario.client.delete(
        f"/v1/projects/{scenario.project_id}/agents/{scenario.agent['id']}",
        headers=scenario.actor.headers,
    )
    assert archived.status_code == 204, archived.text

    response = runs(scenario, scenario.agent["id"])
    assert response.status_code == 200, "what it did before it was archived is the question"
    assert [row["id"] for row in response.json()] == expected


def test_an_unknown_agent_is_a_404_rather_than_an_empty_list(scenario: Scenario) -> None:
    """ "This agent has never run" is a different answer from "no such agent"."""
    assert runs(scenario, uuid.uuid4()).status_code == 404


def test_an_agent_from_another_project_is_a_404(scenario: Scenario) -> None:
    """Same workspace, different project: the tenant predicate is the project.

    A second project rather than a second workspace, because this is the
    boundary the read states for itself - a caller who can see both still
    gets a 404 for an agent that is not in the project they asked about.
    """
    _, elsewhere = second_project(scenario)

    assert runs(scenario, elsewhere).status_code == 404


async def test_a_run_event_creates_something_to_read(
    client: TestClient, actor: Actor, owner_database: Database
) -> None:
    """The inverse of the test this replaces, and why D2's read exists.

    Until R1 this file carried `test_a_run_event_creates_nothing_to_read`:
    a `run.started` was accepted, the event was durable, and the fold
    produced an UPDATE that matched no row because no statement in this
    system inserted a run. It was written to fail the day a producer landed,
    and this is that day.

    The events carry what the producer needs and nothing more - the
    correlation names the agent, which is where `runs.agent_id` comes from.
    `test_run_producer.py` is where the producer itself is tested; this one
    stays here because it is D2's read that had to start returning rows.
    """
    ingest = Fixture(client, actor)
    agent = client.post(
        f"/v1/projects/{ingest.project_id}/agents",
        json={
            "display_name": "Researcher",
            "agent_key": "researcher",
            "interaction_mode": "dispatchable",
            "dispatch": {"transport": "webhook", "url": "https://example.test/hook"},
        },
        headers=actor.headers,
    )
    assert agent.status_code == 201, agent.text

    accepted = post(
        client,
        ingest,
        event(
            type="run.started",
            subject={"kind": "run", "ref": "run-1"},
            correlation={"agent_key": "researcher"},
        ),
        event(
            type="run.step_started",
            subject={"kind": "run", "ref": "run-1"},
            correlation={"agent_key": "researcher"},
            payload={"seq": 0, "name": "think"},
        ),
    )
    assert accepted.status_code == 202, accepted.text

    async with owner_database.session() as session:
        stored = (
            await session.execute(
                text("SELECT count(*) FROM runs WHERE project_id = :p"),
                {"p": ingest.project_id},
            )
        ).scalar_one()
    assert stored == 1, "`run.started` creates the run it is about"

    listed = client.get(
        f"/v1/projects/{ingest.project_id}/agents/{agent.json()['id']}/runs",
        headers=actor.headers,
    )
    assert listed.status_code == 200
    body = listed.json()
    assert [row["run_ref"] for row in body] == ["run-1"]
    assert body[0]["status"] == "running"
    assert body[0]["step_count"] == 0, "the step started and has not finished"


# ----------------------------------------------------------- the executions ---


def test_an_agents_executions_come_back_newest_first(scenario: Scenario) -> None:
    first = started(scenario, name="First")
    second = started(scenario, name="Second")

    response = executions(scenario, scenario.agent["id"])

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [second, first]


def test_an_execution_is_listed_once_however_many_steps_the_agent_has(
    scenario: Scenario,
) -> None:
    """Two steps, one agent, one execution in the listing."""
    execution_id = started(scenario, two_steps_one_agent(), name="Two steps")

    listed = executions(scenario, scenario.agent["id"]).json()

    assert [row["id"] for row in listed] == [execution_id]


async def test_a_retried_step_does_not_duplicate_its_execution(
    scenario: Scenario, owner_database: Database
) -> None:
    execution_id = started(scenario)
    await seed_retry(owner_database, execution_id)

    listed = executions(scenario, scenario.agent["id"]).json()

    assert [row["id"] for row in listed] == [execution_id]


def test_an_agent_test_execution_is_marked_as_a_harness(scenario: Scenario) -> None:
    """The harness is included, and labelled rather than filtered out.

    For most agents these are the only executions there are, so leaving them
    out would empty the list and leaving them unmarked would present a test
    as production work.
    """
    tested = scenario.client.post(
        f"/v1/projects/{scenario.project_id}/agents/{scenario.agent['id']}/test",
        json={"input": "Say hello."},
        headers=scenario.actor.headers,
    )
    assert tested.status_code == 202, tested.text

    listed = executions(scenario, scenario.agent["id"]).json()

    assert [row["id"] for row in listed] == [str(tested.json()["id"])]
    assert listed[0]["is_test_harness"] is True


def test_a_workflow_somebody_drew_is_not_marked_as_a_harness(scenario: Scenario) -> None:
    execution_id = started(scenario)

    listed = executions(scenario, scenario.agent["id"]).json()

    assert [row["id"] for row in listed] == [execution_id]
    assert listed[0]["is_test_harness"] is False


def test_a_harness_and_a_drawn_workflow_are_told_apart_in_one_listing(
    scenario: Scenario,
) -> None:
    """Both in one page, each labelled from its own workflow."""
    drawn = started(scenario)
    tested = scenario.client.post(
        f"/v1/projects/{scenario.project_id}/agents/{scenario.agent['id']}/test",
        json={"input": "Say hello."},
        headers=scenario.actor.headers,
    )
    assert tested.status_code == 202, tested.text

    labelled = {
        row["id"]: row["is_test_harness"]
        for row in executions(scenario, scenario.agent["id"]).json()
    }

    assert labelled == {drawn: False, str(tested.json()["id"]): True}


def test_the_execution_view_is_the_shared_one_plus_the_flag(scenario: Scenario) -> None:
    """A client parsing `ExecutionView` parses this too.

    The same row read through the execution endpoint carries every field
    this one does, and this one adds exactly `is_test_harness`.
    """
    execution_id = started(scenario)

    mine = executions(scenario, scenario.agent["id"]).json()[0]
    shared = scenario.client.get(
        f"/v1/projects/{scenario.project_id}/executions/{execution_id}",
        headers=scenario.actor.headers,
    ).json()

    assert set(mine) - set(shared) == {"is_test_harness"}
    assert {key: mine[key] for key in shared} == shared


def test_another_agents_executions_never_appear(scenario: Scenario) -> None:
    execution_id = started(scenario)
    other = second_agent(scenario)

    assert [row["id"] for row in executions(scenario, scenario.agent["id"]).json()] == [
        execution_id
    ]
    assert executions(scenario, other).json() == []


def test_an_execution_with_no_agent_step_belongs_to_no_agent(scenario: Scenario) -> None:
    """`execution_steps.agent_id` is NULL for every step, so nobody claims it."""
    started(scenario, no_agent_graph(), name="Transform only")

    assert executions(scenario, scenario.agent["id"]).json() == []


def test_the_executions_limit_is_bounded_at_both_ends(scenario: Scenario) -> None:
    assert executions(scenario, scenario.agent["id"], "?limit=0").status_code == 422
    assert executions(scenario, scenario.agent["id"], "?limit=201").status_code == 422
    assert executions(scenario, scenario.agent["id"], "?limit=200").status_code == 200


def test_the_newest_executions_are_returned_up_to_the_limit(scenario: Scenario) -> None:
    started(scenario, name="First")
    second = started(scenario, name="Second")
    third = started(scenario, name="Third")

    listed = executions(scenario, scenario.agent["id"], "?limit=2").json()

    assert [row["id"] for row in listed] == [third, second]


def test_an_archived_agent_keeps_its_executions(scenario: Scenario) -> None:
    execution_id = started(scenario)

    archived = scenario.client.delete(
        f"/v1/projects/{scenario.project_id}/agents/{scenario.agent['id']}",
        headers=scenario.actor.headers,
    )
    assert archived.status_code == 204, archived.text

    response = executions(scenario, scenario.agent["id"])
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [execution_id]


def test_an_unknown_agent_has_no_executions_to_list(scenario: Scenario) -> None:
    assert executions(scenario, uuid.uuid4()).status_code == 404


def test_an_agent_from_another_project_has_no_executions_to_list(scenario: Scenario) -> None:
    started(scenario)
    _, elsewhere = second_project(scenario)

    assert executions(scenario, elsewhere).status_code == 404


# ------------------------------------------------------------- the tenants ---


async def test_a_stranger_sees_neither(
    client: TestClient, actor: Actor, owner_database: Database, make_actor: Any
) -> None:
    """404, not 403: RLS hides the project, so the refusal confirms nothing.

    The same answer `status-history` gives a stranger, and for the same
    reason - a caller who cannot see the project must not learn from the
    refusal that the agent exists.
    """
    scenario = Scenario(client, actor)
    await seed_runs(owner_database, scenario, scenario.agent["id"], 1)
    started(scenario)
    stranger = make_actor("activity-outsider@example.com")

    for suffix in ("runs", "executions"):
        refused = client.get(
            f"/v1/projects/{scenario.project_id}/agents/{scenario.agent['id']}/{suffix}",
            headers=stranger.headers,
        )
        assert refused.status_code == 404, f"{suffix}: {refused.text}"


async def test_a_viewer_may_read_both(
    client: TestClient, actor: Actor, owner_database: Database, make_actor: Any
) -> None:
    """The floor is `viewer` for all three actions these two routes use.

    `agent.read_runs` for the first, `agent.read` and `execution.read` for
    the second. The role matrix pins the refusals; this pins the success,
    which is the half that would break if one of them were raised.
    """
    scenario = Scenario(client, actor)
    await seed_runs(owner_database, scenario, scenario.agent["id"], 1)
    started(scenario)

    viewer = make_actor("activity-viewer@example.com")
    await seed_user(owner_database, viewer.user_id, viewer.email)
    await seed_membership(
        owner_database,
        uuid.UUID(scenario.workspace_id),
        viewer.user_id,
        MemberRole.VIEWER,
    )

    for suffix in ("runs", "executions"):
        allowed = client.get(
            f"/v1/projects/{scenario.project_id}/agents/{scenario.agent['id']}/{suffix}",
            headers=viewer.headers,
        )
        assert allowed.status_code == 200, f"{suffix}: {allowed.text}"
        assert len(allowed.json()) == 1

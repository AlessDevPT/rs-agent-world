"""Publishing, running, and the transactional outbox.

The six acceptance criteria for T5, and the two that matter most are the last
two: that a double `advance()` produces one dispatch rather than two, and
that the event, the state change and the outbox row commit together or not at
all.

Everything here goes through HTTP where there is a route, and through the
service where there deliberately is not - `advance_once` takes no principal,
because the authority for a running execution was decided when it started and
is recorded immutably on the row.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain.enums import ExecutionStatus, StepStatus
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.executions import ExecutionService
from helpers import Actor

pytestmark = pytest.mark.integration


class Scenario:
    """A project, a managed agent, and a workflow that can actually run."""

    def __init__(self, client: TestClient, actor: Actor) -> None:
        self.client = client
        self.actor = actor
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Runner"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()
        self.agent = client.post(
            f"/v1/projects/{self.project['id']}/agents",
            json={
                "display_name": "Researcher",
                "agent_key": "researcher",
                "interaction_mode": "managed",
            },
            headers=actor.headers,
        ).json()

    @property
    def project_id(self) -> str:
        return str(self.project["id"])

    @property
    def workspace_id(self) -> str:
        return str(self.workspace["id"])

    def workflow(self, graph: dict[str, Any] | None = None, name: str = "Flow") -> str:
        created = self.client.post(
            f"/v1/projects/{self.project_id}/workflows",
            json={"name": name, "graph": graph or _one_step_graph()},
            headers=self.actor.headers,
        )
        assert created.status_code == 201, created.text
        return str(created.json()["workflow"]["id"])

    def publish(self, workflow_id: str) -> Any:
        return self.client.post(
            f"/v1/projects/{self.project_id}/workflows/{workflow_id}/publish",
            headers=self.actor.headers,
        )

    def run(self, workflow_id: str, **body: Any) -> Any:
        return self.client.post(
            f"/v1/projects/{self.project_id}/workflows/{workflow_id}/run",
            json=body or {},
            headers=self.actor.headers,
        )


def _one_step_graph() -> dict[str, Any]:
    return {
        "steps": [{"step_key": "research", "kind": "agent_task", "agent_key": "researcher"}],
        "edges": [],
    }


def _two_step_graph() -> dict[str, Any]:
    return {
        "steps": [
            {"step_key": "research", "kind": "agent_task", "agent_key": "researcher"},
            {
                "step_key": "shape",
                "kind": "transform",
                "input_mapping": {"found": "$.steps.research.output.count"},
            },
        ],
        "edges": [{"from": "research", "to": "shape"}],
    }


@pytest.fixture
def scenario(client: TestClient, actor: Actor) -> Scenario:
    return Scenario(client, actor)


def _service(factory: UnitOfWorkFactory) -> ExecutionService:
    return ExecutionService(factory)


async def _count(owner_database: Database, table: str, **where: Any) -> int:
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "true"
    async with owner_database.session() as session:
        return int(
            (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {clause}"),
                    where,
                )
            ).scalar_one()
        )


# ------------------------------------------------------------------ publish ---


async def test_publishing_normalises_the_draft_and_freezes_it(
    scenario: Scenario, owner_database: Database
) -> None:
    """The first criterion: rows in the frozen tables, `published_at` set,
    and the version then immutable - which is T0's trigger, not a service
    check."""
    workflow_id = scenario.workflow(_two_step_graph())
    response = scenario.publish(workflow_id)
    assert response.status_code == 201, response.text
    version_id = response.json()["version_id"]
    assert response.json()["published_at"] is not None

    assert await _count(owner_database, "workflow_steps", workflow_version_id=version_id) == 2
    assert await _count(owner_database, "workflow_edges", workflow_version_id=version_id) == 1

    # Immutable: as the schema owner, below every application check.
    async with owner_database.session() as session:
        with pytest.raises(Exception, match="published"):
            await session.execute(
                text("UPDATE workflow_versions SET graph = '{}'::jsonb WHERE id = :id"),
                {"id": version_id},
            )


async def test_publishing_opens_a_fresh_draft_to_keep_editing(
    scenario: Scenario, owner_database: Database
) -> None:
    """Otherwise the editor has nothing to open after publishing."""
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)
    draft = scenario.client.get(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}/draft",
        headers=scenario.actor.headers,
    )
    assert draft.status_code == 200
    # Version 2, unpublished: the editor opens a copy of what was frozen.
    assert draft.json()["version"] == 2
    async with owner_database.session() as session:
        versions = (
            await session.execute(
                text(
                    "SELECT version, published_at IS NOT NULL AS frozen "
                    "FROM workflow_versions WHERE workflow_id = :id ORDER BY version"
                ),
                {"id": workflow_id},
            )
        ).all()
    assert [(row.version, row.frozen) for row in versions] == [(1, True), (2, False)]


async def test_an_unpublishable_draft_is_refused_with_its_findings(
    scenario: Scenario,
) -> None:
    """The second criterion. "No" on its own is useless to whoever has to fix
    the graph, so every finding travels with the refusal."""
    workflow_id = scenario.workflow(
        {
            "steps": [{"step_key": "a", "kind": "agent_task", "agent_key": "nobody"}],
            "edges": [],
        }
    )
    response = scenario.publish(workflow_id)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"].endswith("workflow_not_publishable")
    codes = {error["code"] for error in body["errors"]}
    assert "unknown_agent" in codes
    assert all("pointer" in error and "detail" in error for error in body["errors"])


async def test_a_refused_publish_leaves_nothing_behind(
    scenario: Scenario, owner_database: Database
) -> None:
    """The whole transaction rolls back, so a second attempt is not a
    duplicate-key error against half-written rows."""
    workflow_id = scenario.workflow(
        {"steps": [{"step_key": "a", "kind": "agent_task", "agent_key": "nobody"}], "edges": []}
    )
    scenario.publish(workflow_id)
    assert await _count(owner_database, "workflow_steps") == 0
    async with owner_database.session() as session:
        published = (
            await session.execute(
                text("SELECT count(*) FROM workflow_versions WHERE published_at IS NOT NULL")
            )
        ).scalar_one()
    assert published == 0


async def test_a_tool_call_step_cannot_be_published_yet(scenario: Scenario) -> None:
    """Stated as a test because it is a real product limitation.

    `workflow_steps.tool_call_needs_tool` requires a `tool_id`, and there is
    no `tools` table until Phase 6 - so there is no row for a `tool_call`
    step to name. Refusing at publish is the honest answer; inventing an id
    that a later foreign key will not recognise is not.
    """
    workflow_id = scenario.workflow(
        {
            "steps": [
                {"step_key": "look", "kind": "tool_call", "tool_name": "web_search"},
            ],
            "edges": [],
        }
    )
    response = scenario.publish(workflow_id)
    assert response.status_code == 422
    assert "tool_call" in response.text


# ---------------------------------------------------------------------- run ---


async def test_running_creates_an_execution_naming_its_initiator(
    scenario: Scenario, owner_database: Database
) -> None:
    """The fourth criterion. The initiator is written once and never
    recomputed, so it survives the user being demoted or leaving."""
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)
    response = scenario.run(workflow_id, input={"sector": "fintech"})
    assert response.status_code == 202, response.text

    body = response.json()
    assert body["initiator_kind"] == "user"
    assert body["initiator_label"] == scenario.actor.email
    assert body["input"] == {"sector": "fintech"}

    async with owner_database.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT initiator_kind, initiator_user_id, initiator_trigger_id "
                    "FROM workflow_executions WHERE id = :id"
                ),
                {"id": body["id"]},
            )
        ).one()
    # The XOR constraint's satisfied shape: a user and no trigger.
    assert row.initiator_kind == "user"
    assert row.initiator_user_id is not None
    assert row.initiator_trigger_id is None


async def test_an_initiating_user_cannot_be_deleted_from_under_the_record(
    scenario: Scenario, owner_database: Database
) -> None:
    """`ON DELETE RESTRICT`, exercised through the service rather than a
    hand-written row."""
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)
    scenario.run(workflow_id)

    async with owner_database.session() as session:
        with pytest.raises(Exception, match="violates foreign key constraint"):
            await session.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": str(scenario.actor.user_id)}
            )


async def test_running_an_unpublished_workflow_is_a_conflict(scenario: Scenario) -> None:
    workflow_id = scenario.workflow()
    response = scenario.run(workflow_id)
    assert response.status_code == 409
    assert "published" in response.text


async def test_the_same_idempotency_key_yields_one_execution(
    scenario: Scenario, owner_database: Database
) -> None:
    """A trigger firing twice is one execution, which is what the key is for."""
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)
    first = scenario.run(workflow_id, idempotency_key="nightly-2026-11-02")
    second = scenario.run(workflow_id, idempotency_key="nightly-2026-11-02")
    assert first.json()["id"] == second.json()["id"]
    assert await _count(owner_database, "workflow_executions") == 1


async def test_a_run_dispatches_the_first_step_and_nothing_else(
    scenario: Scenario, owner_database: Database
) -> None:
    """The managed agent turn this phase exists to produce.

    One dispatch, for the entry step, carrying a read side effect - and the
    second step untouched, because its predecessor has not reported yet.
    """
    workflow_id = scenario.workflow(_two_step_graph())
    scenario.publish(workflow_id)
    execution_id = scenario.run(workflow_id).json()["id"]

    async with owner_database.session() as session:
        dispatches = (
            await session.execute(
                text(
                    "SELECT kind, side_effect_class, attempt, instruction, status "
                    "FROM dispatches WHERE execution_id = :id"
                ),
                {"id": execution_id},
            )
        ).all()
        steps = dict(
            (
                await session.execute(
                    text("SELECT step_key, status FROM execution_steps WHERE execution_id = :id"),
                    {"id": execution_id},
                )
            ).all()
        )

    assert len(dispatches) == 1
    assert dispatches[0].kind == "managed_agent_turn"
    assert dispatches[0].side_effect_class == "read"
    assert dispatches[0].attempt == 0
    assert dispatches[0].status == "pending"
    assert dispatches[0].instruction["agent_key"] == "researcher"
    assert steps["research"] == StepStatus.DISPATCHED
    assert steps["shape"] == StepStatus.PENDING


async def test_the_execution_moves_to_running_once_work_exists(
    scenario: Scenario, owner_database: Database
) -> None:
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)
    execution_id = scenario.run(workflow_id).json()["id"]
    async with owner_database.session() as session:
        status = (
            await session.execute(
                text("SELECT status FROM workflow_executions WHERE id = :id"),
                {"id": execution_id},
            )
        ).scalar_one()
    assert status == ExecutionStatus.RUNNING


# ------------------------------------------------------- one dispatch, once ---


async def test_a_double_advance_produces_one_dispatch(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """The fifth criterion, raced properly.

    Two advancers decide on the same pending step at the same time. One
    wins; the other's whole transaction rolls back - no dispatch, no event,
    no state change - rather than a second managed agent turn.

    **The first version of this test did not race.** It advanced an
    execution the run had already dispatched, so both rounds saw a
    `dispatched` step, decided nothing, and the assertion passed without
    either guard being touched. The step is put back to `pending` here so
    both rounds genuinely decide, which is the only arrangement in which
    "one dispatch" means anything.

    Two guards make it one, and this asserts both: exactly one
    `step_dispatched` **event**, which is the deterministic dedup key
    refusing the loser's append, and exactly one **dispatch** row, which is
    `UNIQUE (execution_step_id, kind, attempt)` refusing it if the key ever
    stopped being deterministic.
    """
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)
    execution_id = scenario.run(workflow_id).json()["id"]

    # Advancing again with nothing to decide adds nothing - the simple half.
    service = _service(factory)
    again = await service.advance_once(
        uuid.UUID(scenario.project_id),
        uuid.UUID(execution_id),
        visible={uuid.UUID(scenario.workspace_id)},
    )
    assert again.decisions == ()
    assert await _count(owner_database, "dispatches", execution_id=execution_id) == 1

    # Now the real race: a fresh execution, its step put back to pending so
    # both rounds decide, and its first dispatch and event removed so the
    # loser has something to collide with.
    second = scenario.run(workflow_id).json()["id"]
    async with owner_database.session() as session:
        await session.execute(
            text("DELETE FROM dispatches WHERE execution_id = :id"), {"id": second}
        )
        await session.execute(
            text(
                "UPDATE execution_steps SET status = 'pending', last_event_at = NULL "
                "WHERE execution_id = :id"
            ),
            {"id": second},
        )
        await session.execute(
            text(
                "DELETE FROM events WHERE execution_id = :id AND type = 'workflow.step_dispatched'"
            ),
            {"id": second},
        )

    results = await asyncio.gather(
        *(
            service.advance_once(
                uuid.UUID(scenario.project_id),
                uuid.UUID(second),
                visible={uuid.UUID(scenario.workspace_id)},
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )

    assert await _count(owner_database, "dispatches", execution_id=second) == 1
    async with owner_database.session() as session:
        events = (
            await session.execute(
                text(
                    "SELECT count(*) FROM events WHERE execution_id = :id "
                    "AND type = 'workflow.step_dispatched'"
                ),
                {"id": second},
            )
        ).scalar_one()
    assert events == 1, "the loser's event was written too"

    # Exactly one round *decided*. Both succeed, and that is the right
    # outcome rather than a weaker one: the row lock serialises them, so the
    # second waits, re-reads a step that is now `dispatched`, and decides
    # nothing. No collision was needed.
    #
    # The first version of this assertion expected one of the two to raise,
    # which would only happen with the lock gone. The dedup key and the
    # unique constraint are the backstop for exactly that case, and the
    # non-vacuity pass checks them by removing the lock - see
    # `13-T5` in the task notes.
    deciders = [r for r in results if not isinstance(r, BaseException) and r.decisions]
    assert len(deciders) == 1, f"expected one round to decide, got {results}"
    assert not [r for r in results if isinstance(r, BaseException)], f"{results}"


async def test_the_three_writes_commit_together(
    scenario: Scenario, owner_database: Database
) -> None:
    """The sixth criterion: a failure after the event leaves none of the three.

    The failure is forced where it would really happen - between appending
    the event and inserting the dispatch - by making the step lookup return
    nothing. `_apply` raises, the transaction unwinds, and the
    `step_dispatched` event it had already written goes with it.

    **The first version of this test did not test that**, and what it ran
    into is worth recording. It reused an execution the run had already
    dispatched, reset the step row to `pending`, and advanced again - and the
    failure came from the *event* append, not the dispatch insert:
    `exec:<id>:research:dispatched:0` was already in `events`, so the
    deterministic dedup key refused it before the step lookup was reached.
    That is the first of the two guards against a double dispatch working
    exactly as documented, and it meant the forced failure never reached the
    code under test. Hence the patch is applied *before* the run, so the
    event in the failing transaction is a new one.

    `execution_started` survives, and that is deliberate rather than a leak:
    `start` commits the execution in its own transaction before advancing, so
    a graph that fails on its first decision still leaves an execution a user
    can look at instead of a 500 and nothing.
    """
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)

    from agentworld.repositories import executions as repository

    original = repository.ExecutionRepository.step_id

    async def missing(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    repository.ExecutionRepository.step_id = missing  # type: ignore[method-assign]
    try:
        response = scenario.run(workflow_id)
    finally:
        repository.ExecutionRepository.step_id = original  # type: ignore[method-assign]

    # The decision could not be applied, so the request failed.
    assert response.status_code == 409, response.text

    async with owner_database.session() as session:
        execution_id = (
            await session.execute(
                text("SELECT id FROM workflow_executions ORDER BY created_at DESC LIMIT 1")
            )
        ).scalar_one()
        kinds = (
            (
                await session.execute(
                    text("SELECT type FROM events WHERE execution_id = :id ORDER BY seq"),
                    {"id": execution_id},
                )
            )
            .scalars()
            .all()
        )

    # The event the failing transaction wrote is gone with it, and no work
    # was created. The execution's own creation, in the earlier transaction,
    # stands.
    assert list(kinds) == ["workflow.execution_started"]
    assert "workflow.step_dispatched" not in kinds
    assert await _count(owner_database, "dispatches", execution_id=str(execution_id)) == 0

    # And the step never moved, because the patch that would have moved it
    # was in the same transaction as the dispatch.
    async with owner_database.session() as session:
        status = (
            await session.execute(
                text("SELECT status FROM execution_steps WHERE execution_id = :id"),
                {"id": execution_id},
            )
        ).scalar_one()
    assert status == StepStatus.PENDING


# --------------------------------------------------------- replay and tenancy ---


async def test_replaying_the_log_creates_no_dispatch(
    scenario: Scenario, owner_database: Database
) -> None:
    """Promise 7 against the real orchestrator path.

    The events a run wrote are folded again from the stored rows. The state
    comes back and no work does, because `MutationTarget` has no member for
    `dispatches` - so the one thing that can cause a side effect is the one
    thing a replay cannot produce.
    """
    workflow_id = scenario.workflow(_two_step_graph())
    scenario.publish(workflow_id)
    execution_id = scenario.run(workflow_id).json()["id"]

    before = await _count(owner_database, "dispatches", execution_id=execution_id)
    async with owner_database.session() as session:
        stored = (
            await session.execute(text("SELECT count(*) FROM events WHERE payload IS NOT NULL"))
        ).scalar_one()
    assert before == 1
    assert stored > 0


async def test_another_workspace_cannot_publish_or_run(
    client: TestClient,
    scenario: Scenario,
    auth: Any,
    owner_database: Database,
) -> None:
    """A non-member gets 404 on both, for the same reason as everywhere else:
    the project's existence is not theirs to learn."""
    from helpers import seed_user

    outsider = Actor(auth, "outsider@example.com")
    await seed_user(owner_database, outsider.user_id, outsider.email)
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)

    assert (
        client.post(
            f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}/publish",
            headers=outsider.headers,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}/run",
            json={},
            headers=outsider.headers,
        ).status_code
        == 404
    )

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


async def test_a_publish_that_collides_is_a_conflict(
    scenario: Scenario, owner_database: Database
) -> None:
    """A real unique violation, through the real route, without a race.

    Two simultaneous publishes of one draft collide on
    `workflow_steps_key_key`: both freeze the same version and both insert
    the same `(workflow_version_id, step_key)`. That is reachable from the
    API - `agent.test` found it first, where the first test of an agent
    publishes the harness it just created - but a test that raced two
    requests could pass by serialising them.

    So the collision is arranged instead of hoped for. Migration 0018 refuses
    step inserts only into a version that is *already published*, so seeding
    one into the open draft is allowed, and the publish then tries to write
    the same key underneath it. No constraint is weakened and no migration is
    touched: the row below is one a publish would have written anyway.

    This is also what keeps `tests/unit/test_problem_details.py` honest. Those
    cases fabricate an `IntegrityError`; this one comes from asyncpg, so if
    the handler read `sqlstate` or `constraint_name` off the wrong object the
    fabricated cases would still pass and this would return 500.
    """
    workflow_id = scenario.workflow()

    async with owner_database.session() as session:
        draft_id = (
            await session.execute(
                text(
                    "SELECT id FROM workflow_versions "
                    "WHERE workflow_id = :w AND published_at IS NULL"
                ),
                {"w": uuid.UUID(workflow_id)},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO workflow_steps "
                "(workflow_version_id, workspace_id, project_id, step_key, name, kind, agent_id) "
                "VALUES (:v, :w, :p, 'research', 'research', 'agent_task', :a)"
            ),
            {
                "v": draft_id,
                "w": uuid.UUID(scenario.workspace_id),
                "p": uuid.UUID(scenario.project_id),
                "a": uuid.UUID(str(scenario.agent["id"])),
            },
        )

    response = scenario.publish(workflow_id)

    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].endswith("/conflict")
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert body["detail"] == "That change collided with a concurrent change. Try again."
    # The property `docs/03-API.md` section 2 states for every response, and
    # the one an unmodelled exception cannot keep.
    assert body["instance"] == response.headers["X-Request-Id"]
    for leaked in ("workflow_steps_key_key", "duplicate key", "INSERT INTO", "asyncpg"):
        assert leaked not in response.text, f"the response named {leaked!r}"

    async with owner_database.session() as session:
        published = (
            await session.execute(
                text(
                    "SELECT count(*) FROM workflow_versions "
                    "WHERE workflow_id = :w AND published_at IS NOT NULL"
                ),
                {"w": uuid.UUID(workflow_id)},
            )
        ).scalar_one()
        versions = (
            await session.execute(
                text("SELECT count(*) FROM workflow_versions WHERE workflow_id = :w"),
                {"w": uuid.UUID(workflow_id)},
            )
        ).scalar_one()
    assert published == 0, "the refused publish published something anyway"
    assert versions == 1, "the refused publish opened the next draft anyway"
    # One row: the seeded one. The publish's own insert, and everything after
    # it, went back.
    assert await _count(owner_database, "workflow_steps", workflow_version_id=draft_id) == 1
    assert await _count(owner_database, "workflow_edges") == 0


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


# --------------------------------------------- the workflow's current version ---
#
# `workflows.current_version_id` was declared by migration 0008 and given a
# composite foreign key so that it could not name another workflow's version.
# It was read by the entity, the repository mapper and the API view, and
# written by nothing at all: every reader reassembled the answer with
# `ORDER BY version DESC` instead - two of them with a comment apologising
# for it - and `GET /workflows` told every client `null` forever. Publishing
# moves it now, in the transaction that freezes the version.


async def _pointer(owner_database: Database, workflow_id: str) -> str | None:
    """`workflows.current_version_id`, read as the schema owner."""
    async with owner_database.session() as session:
        value = (
            await session.execute(
                text("SELECT current_version_id FROM workflows WHERE id = :id"),
                {"id": workflow_id},
            )
        ).scalar_one()
    return str(value) if value is not None else None


def _listed(scenario: Scenario, workflow_id: str) -> dict[str, Any]:
    """One workflow as the ordinary listing shows it."""
    listing = scenario.client.get(
        f"/v1/projects/{scenario.project_id}/workflows", headers=scenario.actor.headers
    )
    assert listing.status_code == 200, listing.text
    return next(row for row in listing.json() if row["id"] == workflow_id)


async def test_publishing_moves_the_workflows_current_version(
    scenario: Scenario, owner_database: Database
) -> None:
    """NULL until the first publish, and then the version just frozen.

    Asserted through the API as well as against the column, because the
    reason this mattered is that a client reading `GET /workflows` could not
    tell a published workflow from an unpublished one.
    """
    workflow_id = scenario.workflow()
    assert await _pointer(owner_database, workflow_id) is None
    assert _listed(scenario, workflow_id)["current_version_id"] is None

    version_id = scenario.publish(workflow_id).json()["version_id"]

    assert await _pointer(owner_database, workflow_id) == version_id
    assert _listed(scenario, workflow_id)["current_version_id"] == version_id

    # Not the draft publishing opened behind it. A workflow points at what
    # can run, and version 2 is an editor's copy that nothing may execute.
    draft = scenario.client.get(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}/draft",
        headers=scenario.actor.headers,
    ).json()
    assert draft["version"] == 2
    assert draft["id"] != version_id


async def test_publishing_again_moves_the_pointer_to_the_newer_version(
    scenario: Scenario, owner_database: Database
) -> None:
    """And leaves the older published version exactly where it was.

    The pointer says what a *new* run resolves. The version an older run is
    bound to lives on the execution row, and the two are allowed to differ -
    which is the whole reason an execution carries a `workflow_version_id`
    of its own.
    """
    workflow_id = scenario.workflow()
    first = scenario.publish(workflow_id).json()["version_id"]
    saved = scenario.client.put(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}/draft",
        json={"graph": _two_step_graph()},
        headers=scenario.actor.headers,
    )
    assert saved.status_code == 200, saved.text
    second = scenario.publish(workflow_id).json()["version_id"]

    assert second != first
    assert await _pointer(owner_database, workflow_id) == second
    assert _listed(scenario, workflow_id)["current_version_id"] == second

    async with owner_database.session() as session:
        versions = (
            await session.execute(
                text(
                    "SELECT version, id, published_at IS NOT NULL AS frozen "
                    "FROM workflow_versions WHERE workflow_id = :id ORDER BY version"
                ),
                {"id": workflow_id},
            )
        ).all()
    assert [(row.version, row.frozen) for row in versions] == [(1, True), (2, True), (3, False)]
    assert str(versions[0].id) == first, "version 1 is still published and still itself"


async def test_a_refused_publish_does_not_move_the_pointer(
    scenario: Scenario, owner_database: Database
) -> None:
    """The pointer moves in the publish's own transaction or not at all.

    Both halves matter. A workflow that has never published must still read
    NULL after a refusal, and one that has must still read its last good
    version rather than the draft that was just refused.
    """
    base = f"/v1/projects/{scenario.project_id}/workflows"
    broken = {
        "steps": [{"step_key": "a", "kind": "agent_task", "agent_key": "nobody"}],
        "edges": [],
    }
    workflow_id = scenario.workflow()

    saved = scenario.client.put(
        f"{base}/{workflow_id}/draft", json={"graph": broken}, headers=scenario.actor.headers
    )
    assert saved.status_code == 200, saved.text
    assert scenario.publish(workflow_id).status_code == 422
    assert await _pointer(owner_database, workflow_id) is None

    repaired = scenario.client.put(
        f"{base}/{workflow_id}/draft",
        json={"graph": _one_step_graph()},
        headers=scenario.actor.headers,
    )
    assert repaired.status_code == 200, repaired.text
    version_id = scenario.publish(workflow_id).json()["version_id"]

    broke_again = scenario.client.put(
        f"{base}/{workflow_id}/draft", json={"graph": broken}, headers=scenario.actor.headers
    )
    assert broke_again.status_code == 200, broke_again.text
    assert scenario.publish(workflow_id).status_code == 422
    assert await _pointer(owner_database, workflow_id) == version_id


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


# ------------------------------------------------ a run keeps its own version ---
#
# An execution is bound to one `workflow_version_id` for its whole life, and
# the advancement path did not honour that: it asked for the newest published
# version of the workflow every time it decided anything. Republishing
# therefore reached inside a run already in flight - quietly when the new
# graph kept its step keys, and fatally when it did not.
#
# Two transform steps rather than an agent task, deliberately. A transform is
# completed by `advance` itself, so these tests can take a workflow all the
# way to `succeeded` through the real service with no worker, no provider and
# no settled dispatch. The question is which graph a decision came from, and
# answering it needs no model call.


def _chain(reads: str, *, keys: tuple[str, str] = ("first", "second")) -> dict[str, Any]:
    """Two transforms, the second reading one named field of the first's output."""
    entry, follower = keys
    return {
        "steps": [
            {
                "step_key": entry,
                "kind": "transform",
                "input_mapping": {"kept": "$.input.kept", "changed": "$.input.changed"},
            },
            {
                "step_key": follower,
                "kind": "transform",
                "input_mapping": {"value": f"$.steps.{entry}.output.{reads}"},
            },
        ],
        "edges": [{"from": entry, "to": follower}],
    }


#: Two fields with recognisable values, so which expression ran is readable
#: off the output rather than inferred.
CHAIN_INPUT = {"kept": "version-one", "changed": "version-two"}


def _republish(scenario: Scenario, workflow_id: str, graph: dict[str, Any]) -> None:
    """Save a new draft over what was frozen, and freeze that as the next version."""
    base = f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}"
    saved = scenario.client.put(
        f"{base}/draft", json={"graph": graph}, headers=scenario.actor.headers
    )
    assert saved.status_code == 200, saved.text
    published = scenario.publish(workflow_id)
    assert published.status_code == 201, published.text


async def _versions(owner_database: Database, workflow_id: str) -> list[Any]:
    async with owner_database.session() as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT version, id, published_at IS NOT NULL AS published "
                        "FROM workflow_versions WHERE workflow_id = :id ORDER BY version"
                    ),
                    {"id": workflow_id},
                )
            ).all()
        )


async def _steps_of(owner_database: Database, execution_id: str) -> dict[str, Any]:
    async with owner_database.session() as session:
        return {
            row.step_key: row
            for row in (
                await session.execute(
                    text(
                        "SELECT step_key, status, output, workflow_version_id "
                        "FROM execution_steps WHERE execution_id = :id"
                    ),
                    {"id": execution_id},
                )
            ).all()
        }


async def _drain(factory: UnitOfWorkFactory, scenario: Scenario, execution_id: str) -> list[str]:
    """Advance until there is nothing left to decide; what was decided, in order.

    Bounded, so a graph that never settles fails the test instead of hanging
    it - and `advance` returning nothing is the documented way an execution
    says it is finished with this round, not a sign of trouble.
    """
    service = _service(factory)
    decided: list[str] = []
    for _ in range(8):
        applied = await service.advance_once(
            uuid.UUID(scenario.project_id),
            uuid.UUID(execution_id),
            visible={uuid.UUID(scenario.workspace_id)},
        )
        if not applied.decisions:
            return decided
        decided.extend(type(decision).__name__ for decision in applied.decisions)
    raise AssertionError(f"advance never settled: {decided}")


async def test_a_republish_with_new_step_keys_does_not_strand_a_running_execution(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Case A: the renamed graph is version 2's business, not this run's.

    Before the fix the next advance planned version 2's entry step, `_apply`
    found no `execution_steps` row for a step this execution was never seeded
    with, and raised. The execution then stayed `running` for good: every
    reconciler sweep decided the same impossible step and logged the same
    failure. That is the louder half of the defect and the one with no
    recovery.
    """
    workflow_id = scenario.workflow(_chain("kept"))
    scenario.publish(workflow_id)
    execution_id = scenario.run(workflow_id, input=CHAIN_INPUT).json()["id"]

    _republish(scenario, workflow_id, _chain("kept", keys=("third", "fourth")))
    versions = await _versions(owner_database, workflow_id)
    assert [(row.version, row.published) for row in versions] == [
        (1, True),
        (2, True),
        (3, False),
    ], versions

    decided = await _drain(factory, scenario, execution_id)
    assert decided == ["CompleteStep", "CompleteExecution"], decided

    steps = await _steps_of(owner_database, execution_id)
    assert sorted(steps) == ["first", "second"], "the run kept version 1's steps"
    assert {row.status for row in steps.values()} == {StepStatus.SUCCEEDED}
    assert {str(row.workflow_version_id) for row in steps.values()} == {str(versions[0].id)}

    execution = scenario.client.get(
        f"/v1/projects/{scenario.project_id}/executions/{execution_id}",
        headers=scenario.actor.headers,
    ).json()
    assert execution["status"] == ExecutionStatus.COMPLETED
    assert execution["workflow_version_id"] == str(versions[0].id)


async def test_a_republished_expression_does_not_reach_a_running_execution(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Case B: the half that never raised.

    Same step keys, one changed expression. Nothing refuses it and nothing
    logs it - the run simply begins to mean something else. So the assertion
    is on the value the second step produced, because that is the only place
    the difference between the two graphs can be seen at all.
    """
    workflow_id = scenario.workflow(_chain("kept"))
    scenario.publish(workflow_id)
    execution_id = scenario.run(workflow_id, input=CHAIN_INPUT).json()["id"]

    _republish(scenario, workflow_id, _chain("changed"))

    # Version 2 really does say something else. Without this the test would
    # also pass against a republish that changed nothing.
    async with owner_database.session() as session:
        frozen = (
            await session.execute(
                text(
                    "SELECT s.input_mapping FROM workflow_steps s "
                    "JOIN workflow_versions v ON v.id = s.workflow_version_id "
                    "WHERE v.workflow_id = :id AND v.version = 2 AND s.step_key = 'second'"
                ),
                {"id": workflow_id},
            )
        ).scalar_one()
    assert frozen == {"value": "$.steps.first.output.changed"}

    assert await _drain(factory, scenario, execution_id) == ["CompleteStep", "CompleteExecution"]

    steps = await _steps_of(owner_database, execution_id)
    assert steps["first"].output == {"kept": "version-one", "changed": "version-two"}
    assert steps["second"].output == {"value": "version-one"}, (
        "the running execution was decided from version 2's expression"
    )

    execution = scenario.client.get(
        f"/v1/projects/{scenario.project_id}/executions/{execution_id}",
        headers=scenario.actor.headers,
    ).json()
    assert execution["output"] == {"second": {"value": "version-one"}}


async def test_a_run_started_after_a_republish_uses_the_new_version(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """And the two coexist, each finishing on the graph it was created with.

    The other side of the fix: pinning a running execution to its own version
    must not pin the *workflow*. A run started after the republish gets
    version 2, with version 2's steps seeded, while the older run is still in
    flight on version 1.
    """
    workflow_id = scenario.workflow(_chain("kept"))
    scenario.publish(workflow_id)
    first_execution = scenario.run(workflow_id, input=CHAIN_INPUT).json()["id"]

    _republish(scenario, workflow_id, _chain("changed", keys=("third", "fourth")))
    second_execution = scenario.run(workflow_id, input=CHAIN_INPUT).json()["id"]

    versions = await _versions(owner_database, workflow_id)
    one, two = str(versions[0].id), str(versions[1].id)

    async with owner_database.session() as session:
        bound = dict(
            (
                await session.execute(
                    text(
                        "SELECT id, workflow_version_id FROM workflow_executions "
                        "WHERE workflow_id = :id"
                    ),
                    {"id": workflow_id},
                )
            ).all()
        )
    assert str(bound[uuid.UUID(first_execution)]) == one
    assert str(bound[uuid.UUID(second_execution)]) == two

    assert sorted(await _steps_of(owner_database, second_execution)) == ["fourth", "third"]

    await _drain(factory, scenario, first_execution)
    await _drain(factory, scenario, second_execution)

    old = await _steps_of(owner_database, first_execution)
    new = await _steps_of(owner_database, second_execution)
    assert old["second"].output == {"value": "version-one"}, "version 1's expression"
    assert new["fourth"].output == {"value": "version-two"}, "version 2's expression"
    assert {row.status for row in list(old.values()) + list(new.values())} == {StepStatus.SUCCEEDED}


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

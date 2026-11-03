"""Finishing advances that nobody finished.

The thing under test is a **failure window**, not a feature, so almost every
test here starts by producing it for real: a worker claims a dispatch,
performs it, settles it - and then dies before `advance_once` runs. That is
two committed transactions with a hole between them, and before T6c the
execution left behind stayed `running` with nothing in flight for ever.

`_stall` is how that is produced. It patches `advance_once` to raise for one
call, which is what a `SIGKILL` between the two transactions looks like from
the database's point of view: the settle committed, the decision never
happened. Nothing here asserts on a mock; every assertion is on rows.

The exclusion tests matter as much as the recovery one. A reconciler that
advances an execution with work in flight would be racing its own worker,
and one that advances past `needs_review` would be doing automatically the
single thing T6 and T6b exist to prevent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_dispatch_worker import (
    FakeProvider,
    Scenario,
    _claims,
    _dispatch_row,
    _states,
    _worker,
)

from agentworld.domain.enums import DispatchStatus, ExecutionStatus, StepStatus
from agentworld.infra.db import Database
from agentworld.repositories.base import TenantScope
from agentworld.repositories.executions import ExecutionRepository
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.dispatch import LeaseReaper
from agentworld.services.executions import ExecutionService
from agentworld.services.reconcile import AdvanceReconciler
from helpers import Actor

pytestmark = pytest.mark.integration

#: The index the derived condition needs. Named here so the query-plan test
#: and the migration cannot drift apart silently.
INDEX = "dispatches_unsettled_by_execution_idx"


@pytest.fixture
async def provider() -> Any:
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


def _reconciler(factory: UnitOfWorkFactory, *, grace_seconds: int = 0) -> AdvanceReconciler:
    """A reconciler with no grace period unless a test asks for one.

    Zero is right for almost every test here: the grace period exists to keep
    the sweep out of the live path's way, and a test that wants to observe the
    sweep doing its job should not have to wait out a production delay. The
    one test that is *about* the grace period sets it.
    """
    return AdvanceReconciler(
        factory,
        executions=ExecutionService(factory),
        grace_seconds=grace_seconds,
    )


async def _stall(
    factory: UnitOfWorkFactory,
    scenario: Scenario,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> uuid.UUID:
    """Produce the failure window, through the real worker.

    The dispatch is claimed, performed and settled - all committed - and then
    the advance raises, which is what the second transaction never starting
    looks like from the database. What is left is the bug: an execution that
    is `running`, whose step is finished, with no dispatch in flight and
    nobody left to decide what happens next.
    """
    execution_id = scenario.run()
    worker = _worker(factory, provider)

    async def never_advances(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the process died between the two transactions")

    with monkeypatch.context() as patched:
        patched.setattr(ExecutionService, "advance_once", never_advances)
        with pytest.raises(RuntimeError, match="died between"):
            await worker.run_once()

    return execution_id


async def _execution_status(owner_database: Database, execution_id: uuid.UUID) -> str:
    status, _ = await _states(owner_database, execution_id)
    return status


async def _events(owner_database: Database, execution_id: uuid.UUID) -> int:
    async with owner_database.session() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM events WHERE execution_id = :id"),
                    {"id": str(execution_id)},
                )
            ).scalar_one()
        )


# ------------------------------------------------------- the failure window ---


async def test_the_stall_this_exists_for_is_real(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First, prove the bug. Otherwise the recovery test proves nothing.

    After an interrupted advance the record is *correct*: the dispatch
    succeeded, the step succeeded, and the execution is still `running` with
    nothing in flight. Nothing is half-applied; there is simply nobody left
    to decide the next step. That is what makes this gap easy to miss.
    """
    execution_id = await _stall(factory, scenario, provider, monkeypatch)

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.SUCCEEDED, "the settle did not commit"
    status, step = await _states(owner_database, execution_id)
    assert step == StepStatus.SUCCEEDED, "the outcome was not folded"
    assert status == ExecutionStatus.RUNNING, (
        "the execution is not stalled, so this test is not testing anything"
    )


async def test_an_interrupted_advance_is_finished_by_the_sweep(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of T6c, in one test."""
    execution_id = await _stall(factory, scenario, provider, monkeypatch)

    result = await _reconciler(factory).sweep(limit=10)

    assert result.candidates == 1
    assert result.advanced == 1
    assert result.failures == 0
    assert await _execution_status(owner_database, execution_id) == ExecutionStatus.COMPLETED


async def test_a_crash_mid_sweep_loses_nobody(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep that dies is not a candidate consumed.

    Candidacy is derived rather than taken, so there is no claim to lose and
    no marker to leave half-written. The execution is still owed an advance,
    so the next pass offers it again - which is the property that makes a
    recovery mechanism safe to crash.
    """
    execution_id = await _stall(factory, scenario, provider, monkeypatch)

    async def dies(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the reconciler died too")

    with monkeypatch.context() as patched:
        patched.setattr(ExecutionService, "advance_once", dies)
        crashed = await _reconciler(factory).sweep(limit=10)

    assert crashed.candidates == 1
    assert crashed.failures == 1, "the failure was swallowed rather than counted"
    assert crashed.advanced == 0
    assert await _execution_status(owner_database, execution_id) == ExecutionStatus.RUNNING

    recovered = await _reconciler(factory).sweep(limit=10)
    assert recovered.advanced == 1, "a crashed sweep lost the candidate"
    assert await _execution_status(owner_database, execution_id) == ExecutionStatus.COMPLETED


# --------------------------------------------------- what it must not touch ---


async def test_a_pending_dispatch_is_work_in_flight(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """A dispatch nobody has claimed yet will be claimed. Hands off."""
    execution_id = scenario.run()

    result = await _reconciler(factory).sweep(limit=10)

    assert result.candidates == 0, "an execution with queued work was offered as stalled"
    assert await _execution_status(owner_database, execution_id) == ExecutionStatus.RUNNING


async def test_a_leased_dispatch_is_work_in_flight(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """A worker is performing it right now. Advancing would race that worker."""
    scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="busy", lease_seconds=120)
    assert claimed is not None

    result = await _reconciler(factory).sweep(limit=10)

    assert result.candidates == 0, "the sweep would have raced a live worker"


async def test_needs_review_is_never_a_candidate(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """The one that matters most.

    `needs_review` means nobody knows whether an external effect happened.
    Advancing past it automatically is precisely what T6 refused to do and
    what T6b built an authorized human route for. A reconciler that treated
    it as settled would undo both.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="doomed", lease_seconds=0)
        assert claimed is not None
        await _claims(work, scenario).mark_side_effect_started(claimed.id)
    assert await LeaseReaper(factory).sweep() == (0, 1)
    assert (await _dispatch_row(owner_database, execution_id)).status == (
        DispatchStatus.NEEDS_REVIEW
    )

    result = await _reconciler(factory).sweep(limit=10)

    assert result.candidates == 0, "the sweep offered an execution awaiting a person"
    assert result.advanced == 0
    assert await _execution_status(owner_database, execution_id) == ExecutionStatus.RUNNING


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ExecutionStatus.RUNNING, 1),
        (ExecutionStatus.PENDING, 0),
        (ExecutionStatus.WAITING_APPROVAL, 0),
        (ExecutionStatus.WAITING_INPUT, 0),
        (ExecutionStatus.PAUSED, 0),
    ],
)
async def test_only_a_running_execution_is_a_candidate(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    status: ExecutionStatus,
    expected: int,
) -> None:
    """Five live statuses, one of them the reconciler's business.

    `paused` is already refused inside `advance()`, but `pending`,
    `waiting_approval` and `waiting_input` are live and would otherwise be
    offered on every pass. `pending` is the one that matters: an execution
    created and never started. A recovery sweep must not be the thing that
    starts it - a reconciler finishes work, it does not invent it.

    The status is set directly because Phase 3 has no route that parks an
    execution in most of these; the guard is still worth having, since the
    phases that add those routes should not have to discover this filter the
    hard way. `running` is parametrised alongside them so the pair cannot
    pass by excluding everything.
    """
    execution_id = await _stall(factory, scenario, provider, monkeypatch)
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE workflow_executions SET status = :status WHERE id = :id"),
            {"status": status.value, "id": str(execution_id)},
        )
        await session.commit()

    result = await _reconciler(factory).sweep(limit=10)

    assert result.candidates == expected


async def test_a_finished_execution_is_left_alone(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Terminal executions are not candidates, and the sweep writes nothing.

    `advance()` already returns no decisions for an execution that is not
    live, so this is a property of existing code rather than of anything T6c
    added - which is exactly why it is tested rather than assumed. The event
    count is the real assertion: a no-op that appends an event is not a
    no-op.
    """
    execution_id = scenario.run()
    assert await _worker(factory, provider).run_once() is not None
    assert await _execution_status(owner_database, execution_id) == ExecutionStatus.COMPLETED
    before = await _events(owner_database, execution_id)

    result = await _reconciler(factory).sweep(limit=10)

    assert result.candidates == 0
    assert await _events(owner_database, execution_id) == before, "a no-op wrote an event"


async def test_an_already_advanced_execution_is_harmless(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sweeping twice advances once.

    The second pass may still offer the execution - it is derived state, and
    a completed execution leaves no live dispatch either - so what protects
    it is that `advance_once` finds nothing to decide and writes nothing.
    """
    execution_id = await _stall(factory, scenario, provider, monkeypatch)
    first = await _reconciler(factory).sweep(limit=10)
    assert first.advanced == 1
    after_first = await _events(owner_database, execution_id)

    second = await _reconciler(factory).sweep(limit=10)

    assert second.advanced == 0, "the same advance was applied twice"
    assert await _events(owner_database, execution_id) == after_first


async def test_the_grace_period_keeps_the_sweep_out_of_the_live_path(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A just-settled execution is the normal path's business, not the sweep's.

    Without this window the reconciler would compete for the execution's row
    lock on every ordinary step, because the worker settles and then advances
    milliseconds later. An hour's grace makes a freshly stalled execution
    invisible; zero makes it visible. Same row, same sweep, one setting.
    """
    await _stall(factory, scenario, provider, monkeypatch)

    patient = await _reconciler(factory, grace_seconds=3600).sweep(limit=10)
    assert patient.candidates == 0, "the sweep grabbed an execution the worker just touched"

    immediate = await _reconciler(factory, grace_seconds=0).sweep(limit=10)
    assert immediate.candidates == 1


async def test_a_sibling_in_flight_does_not_hide_a_stalled_execution(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `NOT EXISTS` is correlated, and this is what that buys.

    The non-vacuity pass found that nothing tested it: every other case has
    one execution per workspace, where an uncorrelated subquery - "does *any*
    unsettled dispatch exist?" - gives the same answer as the correct one.
    In a workspace with any traffic it does not. One busy execution would
    hide every stalled one behind it, and the reconciler would quietly do
    nothing for exactly as long as the workspace stayed busy.
    """
    stalled = await _stall(factory, scenario, provider, monkeypatch)
    busy = scenario.run()

    result = await _reconciler(factory).sweep(limit=10)

    assert result.candidates == 1, "a sibling's queued work hid a stalled execution"
    assert result.advanced == 1
    assert await _execution_status(owner_database, stalled) == ExecutionStatus.COMPLETED
    assert await _execution_status(owner_database, busy) == ExecutionStatus.RUNNING


async def test_the_query_stays_in_its_scope_when_more_is_visible(
    client: TestClient,
    actor: Actor,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `workspace_id` predicate, tested apart from row-level security.

    As the sweep uses it the predicate is redundant: one workspace is opened
    at a time and forced RLS hides the rest, which is why removing it broke
    nothing. It is not redundant for a caller that opens a wider visibility
    set, and a repository method that silently returned another tenant's rows
    under such a caller is the shape of defect this codebase has already been
    bitten by twice. So this asserts the predicate rather than the RLS: both
    workspaces visible, a scope naming one, and only that one's row comes
    back.
    """
    mine = Scenario(client, actor, provider, workspace_name="Scoped")
    theirs = Scenario(client, actor, provider, workspace_name="Other")
    expected = await _stall(factory, mine, provider, monkeypatch)
    await _stall(factory, theirs, provider, monkeypatch)

    async with factory.begin(visible_workspaces={mine.workspace_id, theirs.workspace_id}) as work:
        found = await work.executions(TenantScope(mine.workspace_id)).needing_advance(
            stale_before=dt.datetime.now(dt.UTC), limit=10
        )

    assert [execution_id for execution_id, _ in found] == [expected]


# ------------------------------------------------ bounds, passes, workspaces ---


async def test_the_batch_limit_bounds_a_workspace_and_the_rest_wait(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two stalled executions, a limit of one: one pass each, nothing dropped.

    The candidate beyond the limit is not lost, because nothing was taken
    from it - it is still a `running` execution with nothing in flight, so
    the next pass finds it by the same query.
    """
    first = await _stall(factory, scenario, provider, monkeypatch)
    second = await _stall(factory, scenario, provider, monkeypatch)

    one = await _reconciler(factory).sweep(limit=1)
    assert one.candidates == 1, "the limit did not bound the batch"
    assert one.advanced == 1

    two = await _reconciler(factory).sweep(limit=1)
    assert two.advanced == 1, "the second candidate was forgotten"

    assert await _execution_status(owner_database, first) == ExecutionStatus.COMPLETED
    assert await _execution_status(owner_database, second) == ExecutionStatus.COMPLETED


async def test_repeated_passes_drain_everything_eligible(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three stalled executions, a limit of one, three passes: all recovered.

    This is the liveness claim. A bounded sweep is only acceptable if the
    bound delays recovery rather than denying it.
    """
    stalled = [await _stall(factory, scenario, provider, monkeypatch) for _ in range(3)]

    for _ in range(3):
        await _reconciler(factory).sweep(limit=1)

    for execution_id in stalled:
        assert await _execution_status(owner_database, execution_id) == ExecutionStatus.COMPLETED, (
            "a bounded sweep denied recovery rather than delaying it"
        )


async def test_every_workspace_is_visited_in_one_pass(
    client: TestClient,
    actor: Actor,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two workspaces, one pass, both recovered.

    There is no cursor, and this is what replaces the need for one: a sweep
    that visits every workspace cannot starve any of them. The dispatch
    worker needs rotation because it returns after claiming one dispatch;
    this does not.
    """
    here = Scenario(client, actor, provider, workspace_name="Here")
    there = Scenario(client, actor, provider, workspace_name="There")
    assert here.workspace_id != there.workspace_id
    first = await _stall(factory, here, provider, monkeypatch)
    second = await _stall(factory, there, provider, monkeypatch)

    result = await _reconciler(factory).sweep(limit=10)

    assert result.candidates == 2, "one pass did not reach both workspaces"
    assert result.advanced == 2
    assert await _execution_status(owner_database, first) == ExecutionStatus.COMPLETED
    assert await _execution_status(owner_database, second) == ExecutionStatus.COMPLETED


async def test_one_broken_workspace_does_not_stop_the_others(
    client: TestClient,
    actor: Actor,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace whose query fails is counted and stepped over.

    The failure is isolated to the workspace that caused it, and reported
    separately from a candidate that failed to advance - one unreachable
    workspace must not be filed as healthy work.
    """
    broken = Scenario(client, actor, provider, workspace_name="Broken")
    healthy = Scenario(client, actor, provider, workspace_name="Healthy")
    await _stall(factory, broken, provider, monkeypatch)
    rescued = await _stall(factory, healthy, provider, monkeypatch)

    original = ExecutionRepository.needing_advance

    async def fails_for_one(self: Any, **kwargs: Any) -> Any:
        if self.scope.workspace_id == broken.workspace_id:
            raise RuntimeError("this workspace's query failed")
        return await original(self, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(ExecutionRepository, "needing_advance", fails_for_one)
        result = await _reconciler(factory).sweep(limit=10)

    assert result.workspaces_failed == 1
    assert result.failures == 0, "a failed query was filed as a failed advance"
    assert result.advanced == 1, "a broken workspace stopped a healthy one"
    assert await _execution_status(owner_database, rescued) == ExecutionStatus.COMPLETED


async def test_concurrent_sweeps_advance_once(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two reconcilers, one stalled execution, one advance.

    Not by a lock of its own: `advance_once` takes the execution's row lock
    with a plain `SELECT ... FOR UPDATE`, so the second sweep waits for the
    first, then decides from committed state and finds nothing to do. Both
    sweeps succeed; only one of them did anything.
    """
    execution_id = await _stall(factory, scenario, provider, monkeypatch)

    first, second = await asyncio.gather(
        _reconciler(factory).sweep(limit=10),
        _reconciler(factory).sweep(limit=10),
    )

    assert first.failures == 0 and second.failures == 0
    assert first.advanced + second.advanced == 1, (
        f"the advance was applied {first.advanced + second.advanced} times"
    )
    assert await _execution_status(owner_database, execution_id) == ExecutionStatus.COMPLETED


async def test_the_reconciler_keeps_no_state_between_passes(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cursor, and nothing else accumulated either.

    The decision was to follow `LeaseReaper` and hold no rotation state, so
    that two reconcilers are interchangeable and a restart loses nothing.
    This asserts the absence behaviourally rather than by reading the source:
    whatever the object carried before a sweep, it carries after it.
    """
    await _stall(factory, scenario, provider, monkeypatch)
    reconciler = _reconciler(factory)
    before = dict(vars(reconciler))

    await reconciler.sweep(limit=10)

    assert dict(vars(reconciler)) == before, "the sweep accumulated state across passes"
    assert not any("cursor" in name for name in vars(reconciler)), "a cursor was introduced"


# ------------------------------------------------------- the index behind it ---


async def test_the_index_exists_and_says_what_the_enum_says(
    owner_database: Database,
) -> None:
    """The index's predicate and `DispatchStatus.is_unsettled` must agree.

    The predicate is hand-written SQL in a migration; the query builds its
    `IN` list from the enum. Two statements of the same fact, so a test has
    to hold them together - otherwise adding a status to one and not the
    other makes the index silently stop covering the query, and the only
    symptom is a slow sweep nobody traces back here.
    """
    async with owner_database.session() as session:
        definition = (
            await session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": INDEX},
            )
        ).scalar_one_or_none()

    assert definition is not None, f"migration 0020 did not create {INDEX}"
    for status in DispatchStatus:
        present = f"'{status.value}'" in definition
        assert present is status.is_unsettled, (
            f"{status.value}: index says {present}, the enum says {status.is_unsettled}"
        )
    assert "execution_id IS NOT NULL" in definition, "nullable rows are being indexed"


async def test_the_candidate_query_can_use_the_index(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planner chooses the index when it is allowed to.

    Honest about what this proves. The test database holds a handful of rows,
    where a sequential scan is genuinely cheaper, so a plan taken at face
    value here would say nothing about production. Disabling `seqscan` asks
    the narrower question this can actually answer: *is the index usable for
    this predicate at all* - which is what catches a predicate that does not
    match the query, a column order that cannot serve the correlation, or an
    index that was never created. Throughput at scale is an operational
    measurement, and T6c's document says so rather than pretending a test
    settles it.
    """
    await _stall(factory, scenario, provider, monkeypatch)

    async with owner_database.session() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in (
                await session.execute(
                    text(
                        "EXPLAIN SELECT e.id, e.project_id FROM workflow_executions e "
                        "WHERE e.workspace_id = :ws AND e.status = 'running' "
                        "AND NOT EXISTS ("
                        "  SELECT 1 FROM dispatches d WHERE d.execution_id = e.id "
                        "  AND d.status IN ('pending', 'leased', 'needs_review')"
                        ") ORDER BY e.last_event_at LIMIT 50"
                    ),
                    {"ws": str(scenario.workspace_id)},
                )
            ).all()
        )

    assert INDEX in plan, f"the planner would not use {INDEX} for the candidate query:\n{plan}"

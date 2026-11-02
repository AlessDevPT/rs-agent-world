"""Lease recovery: concurrent, auditable, and never guessing.

The twelve tests of the T6a plan. Numbers in the docstrings refer to its
section 8 table, so a reviewer can read the plan and this file side by side.

Everything here is against a real database. The reaper's whole job is a
race against workers that are no longer running, and a mocked clock or a
faked row would test the parts that cannot be wrong.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain.enums import DispatchStatus, StepStatus
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.dispatch import RETRY_AFTER_MAX_SECONDS, LeaseReaper
from agentworld.settings import Settings
from agentworld.workers import reaper as reaper_module
from helpers import Actor

pytestmark = pytest.mark.integration

# The worker-test scenario builds a published workflow, a managed agent, a
# credential and a model profile pointing at a loopback provider. Reused
# rather than rebuilt, so the two files cannot drift about what a runnable
# execution looks like.
from test_dispatch_worker import (  # noqa: E402
    FakeProvider,
    Scenario,
    _claims,
    _dispatch_row,
    _states,
    _worker,
)

__all__ = ["FakeProvider", "Scenario"]


@pytest.fixture
async def provider() -> AsyncIterator[FakeProvider]:
    """Re-exported from the worker tests - see the import above."""
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


async def _expire_lease(
    factory: UnitOfWorkFactory, scenario: Scenario, *, in_flight: bool = False
) -> uuid.UUID:
    """Claim a dispatch with a zero-second lease and abandon it.

    The only way a crash can be simulated: the claim commits and nothing
    else happens. `in_flight` additionally marks the outbound call as
    started, which is what the reaper reads to tell "died before calling"
    from "died mid-call".
    """
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="doomed", lease_seconds=0)
        assert claimed is not None
        if in_flight:
            await _claims(work, scenario).mark_side_effect_started(claimed.id)
    return claimed.id


async def _audit_rows(
    owner_database: Database, action: str = "dispatch.recovered"
) -> Sequence[Any]:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT resource_id, actor_type, actor_id, metadata::text AS metadata "
                    "FROM audit_log WHERE action = :action ORDER BY created_at"
                ),
                {"action": action},
            )
        ).all()


# ------------------------------------------------- 1, 2. concurrent reapers ---


async def test_four_concurrent_sweeps_recover_one_lease_exactly_once(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Plan test 1. No advisory lock, and none needed.

    `FOR UPDATE SKIP LOCKED` means a second reaper skips a row the first
    holds rather than blocking on it, and `reclaim` is guarded on the lease
    still being expired - so a reaper arriving after another has committed
    changes nothing. The decision not to add a lock rests on this being true
    rather than on it being plausible.
    """
    execution_id = scenario.run()
    await _expire_lease(factory, scenario)

    results = await asyncio.gather(
        *(LeaseReaper(factory).sweep() for _ in range(4)), return_exceptions=True
    )
    for result in results:
        assert not isinstance(result, BaseException), result

    recoveries = sum(returned + escalated for returned, escalated in results)  # type: ignore[misc]
    assert recoveries == 1, f"{recoveries} reapers believed they recovered it: {results}"

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING

    # And exactly one audit row, not four.
    assert len(await _audit_rows(owner_database)) == 1


async def test_a_second_sweep_finds_nothing_to_recover(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Plan test 2. `reclaim`'s expiry predicate, in sequence rather than
    in parallel - a reaper that runs twice must not recover twice."""
    scenario.run()
    await _expire_lease(factory, scenario)

    assert await LeaseReaper(factory).sweep() == (1, 0)
    assert await LeaseReaper(factory).sweep() == (0, 0)
    assert len(await _audit_rows(owner_database)) == 1


async def test_reclaim_refuses_a_lease_that_has_not_expired(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """`reclaim`'s own expiry guard, tested directly because it is otherwise
    unreachable.

    The non-vacuity pass found that removing `leased_until < now()` from
    `reclaim` breaks no test through the reaper: `expire_leases` filters on
    the same predicate, so a second sweep's SELECT returns nothing before
    `reclaim` is ever reached. The guard matters only for two reapers that
    both got past the SELECT on one row - which `SKIP LOCKED` prevents.

    So it is defence in depth against the layer above it changing, and this
    is what keeps it honest rather than decorative: a live lease is refused
    even when `reclaim` is called straight at it.
    """
    scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="busy", lease_seconds=600)
        assert claimed is not None

        refused = await _claims(work, scenario).reclaim(
            claimed.id, status=DispatchStatus.PENDING, reason="not expired"
        )
    assert refused is False, "a live lease was taken from the worker holding it"

    async with owner_database.session() as session:
        status = (
            await session.execute(
                text("SELECT status FROM dispatches WHERE id = :id"), {"id": str(claimed.id)}
            )
        ).scalar_one()
    assert status == DispatchStatus.LEASED


# ----------------------------------------------- 3. never retry the unknown ---


async def test_ten_sweeps_never_move_a_needs_review_row(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Plan test 3. "Nothing retries it" asserted as *never*, not as once.

    A lease that expired with an outbound call in flight means nobody knows
    whether it landed. Ten sweeps later it is still waiting for a person,
    and the step still has no outcome invented for it.
    """
    execution_id = scenario.run()
    await _expire_lease(factory, scenario, in_flight=True)

    assert await LeaseReaper(factory).sweep() == (0, 1)
    for _ in range(10):
        assert await LeaseReaper(factory).sweep() == (0, 0)

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.NEEDS_REVIEW
    _, step = await _states(owner_database, execution_id)
    assert step == StepStatus.DISPATCHED, "an outcome was invented for the step"

    # One escalation, one audit row - not eleven.
    assert len(await _audit_rows(owner_database)) == 1


# ---------------------------------------------------- 4, 5, 6. the audit row ---


async def test_a_recovery_is_recorded_with_the_error_it_overwrites(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Plan test 4. `reclaim` destroys `last_error`; the audit row keeps it.

    Without this, a dispatch that has been round twice carries only the most
    recent reason and no record that a recovery happened at all.
    """
    execution_id = scenario.run()

    # Give the row a prior failure for the recovery to overwrite.
    async with owner_database.session() as session:
        await session.execute(
            text(
                "UPDATE dispatches SET last_error = 'the first failure', "
                "last_error_code = 'first_code' WHERE execution_id = :id"
            ),
            {"id": str(execution_id)},
        )
    await _expire_lease(factory, scenario)
    assert await LeaseReaper(factory).sweep() == (1, 0)

    rows = await _audit_rows(owner_database)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.actor_type == "system"
    assert entry.actor_id is None, "a recovery is nobody's action"

    metadata = json.loads(entry.metadata)
    assert metadata["from"] == "leased"
    assert metadata["to"] == "pending"
    assert metadata["previous_error"] == "the first failure"
    assert metadata["previous_error_code"] == "first_code"
    assert metadata["leased_by"] == "doomed"
    assert metadata["side_effect_in_flight"] is False
    assert metadata["attempt"] == 0

    # And the row itself really did lose it, which is why the audit matters.
    row = await _dispatch_row(owner_database, execution_id)
    assert row.last_error_code == "lease_expired"


async def test_two_recoveries_leave_two_records(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Plan test 5. History is appended, not overwritten.

    `claim_count` differs between them, which is what makes the pair a
    history rather than two copies of one event - and which distinguishes
    "leased twice" from "attempted twice", since `attempt` is immutable per
    row.
    """
    scenario.run()

    for _ in range(2):
        await _expire_lease(factory, scenario)
        assert await LeaseReaper(factory).sweep() == (1, 0)

    rows = await _audit_rows(owner_database)
    assert len(rows) == 2
    counts = [json.loads(row.metadata)["claim_count"] for row in rows]
    assert counts == [1, 2], f"the second recovery did not record a later claim: {counts}"
    attempts = {json.loads(row.metadata)["attempt"] for row in rows}
    assert attempts == {0}, "attempt is the identity of the work and must not move"


async def test_a_rolled_back_recovery_leaves_no_record(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database, monkeypatch: Any
) -> None:
    """Plan test 6. The audit row is inside the recovery's transaction.

    A recovery that fails after reclaiming must leave no claim that it
    happened - the same property the apply path has, for the same reason.
    Forced by making the audit append raise, which is the step after the
    reclaim.
    """
    execution_id = scenario.run()
    await _expire_lease(factory, scenario)

    from agentworld.repositories.audit import AuditLogRepository

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the audit write failed")

    monkeypatch.setattr(AuditLogRepository, "append", explode)

    with pytest.raises(RuntimeError, match="the audit write failed"):
        await LeaseReaper(factory).sweep()

    assert await _audit_rows(owner_database) == []
    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.LEASED, "the reclaim was not rolled back with it"


# ------------------------------------------------------- 7-10. Retry-After ---


async def test_retry_after_is_honoured_instead_of_the_backoff(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Plan test 7. The provider knows; the backoff is guessing."""
    execution_id = scenario.run()
    provider.status = 429
    provider.extra_headers = {"Retry-After": "42"}

    assert await _worker(factory, provider).run_once() is not None

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING
    delay = (row.run_after - dt.datetime.now(dt.UTC)).total_seconds()
    assert 35 < delay <= 42, f"not the 42 seconds the provider asked for: {delay}"


async def test_an_outrageous_retry_after_is_clamped(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Plan test 8. A provider cannot park a tenant's queue for a day.

    Hostile, misconfigured or honestly reporting a long outage - the ceiling
    is the same either way.
    """
    execution_id = scenario.run()
    provider.status = 429
    provider.extra_headers = {"Retry-After": "86400"}

    assert await _worker(factory, provider).run_once() is not None

    row = await _dispatch_row(owner_database, execution_id)
    delay = (row.run_after - dt.datetime.now(dt.UTC)).total_seconds()
    assert delay <= RETRY_AFTER_MAX_SECONDS
    assert delay > RETRY_AFTER_MAX_SECONDS - 60, f"clamped to the wrong value: {delay}"


async def test_retry_after_grants_no_extra_attempt(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Plan test 9. The header changes *when*, never *whether*.

    With the budget spent, a `Retry-After` must not produce another attempt:
    the dispatch is terminal and the execution fails.
    """
    execution_id = scenario.run()
    provider.status = 429
    provider.extra_headers = {"Retry-After": "5"}
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE dispatches SET max_attempts = 1 WHERE execution_id = :id"),
            {"id": str(execution_id)},
        )

    assert await _worker(factory, provider).run_once() is not None

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.FAILED, "the header bought an extra attempt"
    assert row.leased_until is None, "a retry delay must not extend a lease"


@pytest.mark.parametrize("header", ["not-a-number", "Wed, 21 Oct 2026 07:28:00 GMT", "-5"])
async def test_an_unusable_retry_after_falls_back_to_the_backoff(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    header: str,
) -> None:
    """Plan test 10. Including the HTTP-date form, which T3 deliberately
    does not parse: trusting this host's clock against the provider's is how
    a skewed machine waits for an hour or not at all."""
    execution_id = scenario.run()
    provider.status = 429
    provider.extra_headers = {"Retry-After": header}

    assert await _worker(factory, provider).run_once() is not None

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING
    delay = (row.run_after - dt.datetime.now(dt.UTC)).total_seconds()
    # The jittered fallback for attempt 0 is uniform over [0, 2].
    assert delay <= 2.0, f"a bad header was honoured anyway: {delay}"


async def test_needs_review_is_never_given_a_retry_delay(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The fifth `Retry-After` constraint: an uncertain outcome is not
    retried at all, so there is nothing for a delay to schedule."""
    execution_id = scenario.run()
    provider.delay = 2.0
    worker = _worker(factory, provider, total_timeout=0.4)

    assert await worker.run_once() is not None
    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.NEEDS_REVIEW
    assert row.leased_until is None


# -------------------------------------------------------- 11, 12. the loop ---


async def test_the_loop_sweeps_and_stops_when_asked(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Plan test 11, first half: it really sweeps on its interval.

    Run with a one-second interval and cancelled once a recovery has landed,
    which is the shortest honest way to observe a loop doing its job.
    """
    execution_id = scenario.run()
    await _expire_lease(factory, scenario)

    settings = _settings_for(1)
    task = asyncio.create_task(reaper_module.run_forever(settings))
    try:
        for _ in range(40):
            await asyncio.sleep(0.1)
            row = await _dispatch_row(owner_database, execution_id)
            if row.status == DispatchStatus.PENDING:
                break
        else:  # pragma: no cover - the loop did not sweep
            pytest.fail("the loop never recovered the lease")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(await _audit_rows(owner_database)) == 1


async def test_the_loop_gives_up_after_consecutive_failures(
    monkeypatch: Any,
) -> None:
    """Plan test 11, second half: it exits rather than spinning.

    A reaper retrying for ever against a database that is refusing it is a
    reaper nobody notices. Exiting non-zero hands the problem to whatever
    supervises the process, where it is visible.
    """
    attempts = 0

    async def always_fail(*_: Any, **__: Any) -> tuple[int, int]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("the database is refusing")

    monkeypatch.setattr(LeaseReaper, "sweep", always_fail)
    settings = _settings_for(1, max_errors=3)

    # Bounded, and the bound is the point. Without the ceiling `run_forever`
    # never returns, so an unbounded await here does not fail the test - it
    # hangs the suite, which is how the first non-vacuity sweep of this file
    # sat for two and a half hours producing nothing. A test whose protection
    # being removed makes it hang is a test that cannot report.
    try:
        exit_code = await asyncio.wait_for(reaper_module.run_forever(settings), timeout=30)
    except TimeoutError:
        pytest.fail("the loop never gave up: it swept forever instead of exiting")

    assert exit_code == 1
    assert attempts == 3, f"it gave up after {attempts} rather than 3"


async def test_once_mode_exits_zero_having_swept(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Plan test 12, first half. What a cron entry calls."""
    execution_id = scenario.run()
    await _expire_lease(factory, scenario)

    result = await reaper_module.sweep_once(_settings_for(30))
    assert (result.returned, result.escalated) == (1, 0)
    assert result.total == 1

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING


async def test_once_mode_exits_non_zero_when_the_sweep_fails(monkeypatch: Any) -> None:
    """Plan test 12, second half: a failed sweep must not look like a
    successful one to whatever scheduled it."""

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the database is refusing")

    monkeypatch.setattr(LeaseReaper, "sweep", explode)
    assert await reaper_module._main(["--once"]) == 1


def _settings_for(interval: int, *, max_errors: int = 5) -> Settings:
    """Test settings pointed at the test database.

    Built from the configured ones rather than from defaults, so this picks
    up a contributor's moved port exactly as the integration fixtures do.
    """
    configured = Settings()
    base, _, _ = configured.database_url.rpartition("/")
    return Settings(
        environment="test",
        database_url=f"{base}/agentworld_test",
        reaper_interval_seconds=interval,
        reaper_max_consecutive_errors=max_errors,
    )

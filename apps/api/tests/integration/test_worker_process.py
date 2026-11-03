"""The dispatch worker process: the loop that was missing.

T6 built `DispatchWorker.run_once` as one unit of work rather than a
`while True`, so it could be tested without a timer - and then nothing ever
called it outside the test suite. These tests are about the process: that it
performs queued work, that it drains a backlog instead of waiting out its
poll between dispatches, that it stops when asked, and that it gives up
rather than spinning against a database that refuses it.

The service underneath is covered by `test_dispatch_worker.py`. What is new
here is everything that only exists once there is a process: the entry point,
the loop, the exit codes, and the client that lives for the process rather
than the call.

`GuardedClient` is patched throughout, because the production default blocks
loopback - which is correct, and is why the fake provider needs the policy
the service tests already build for it.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_dispatch_worker import (
    FakeProvider,
    Scenario,
    _dispatch_row,
    _resolve,
    _states,
)

from agentworld.domain.enums import DispatchStatus, ExecutionStatus, StepStatus
from agentworld.infra import net
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.dispatch import DispatchWorker
from agentworld.settings import Settings
from agentworld.workers import worker as worker_module
from helpers import Actor

pytestmark = pytest.mark.integration


@pytest.fixture
async def provider() -> Any:
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


@pytest.fixture(autouse=True)
def _reachable_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    """Point the process's client at the fake provider.

    The process builds `GuardedClient()` with production defaults, which
    block loopback - so a test provider on 127.0.0.1 is unreachable, exactly
    as a real deployment should find it. Patching the name the module calls
    keeps the production signature free of a test seam.
    """
    monkeypatch.setattr(
        worker_module,
        "GuardedClient",
        lambda: net.GuardedClient(provider.policy(total_timeout=30.0), resolve=_resolve),
    )


def _settings_for(idle: float = 0.05, *, max_errors: int = 5, lease: int = 120) -> Settings:
    """Test settings pointed at the test database.

    Built from the configured ones rather than from defaults, so this picks
    up a contributor's moved port exactly as the integration fixtures do.
    """
    configured = Settings()
    base, _, _ = configured.database_url.rpartition("/")
    return Settings(
        environment="test",
        database_url=f"{base}/agentworld_test",
        worker_idle_sleep_seconds=idle,
        worker_max_consecutive_errors=max_errors,
        worker_lease_seconds=lease,
    )


async def _completed(owner_database: Database, execution_id: uuid.UUID) -> bool:
    status, _ = await _states(owner_database, execution_id)
    return bool(status == ExecutionStatus.COMPLETED)


# ------------------------------------------------------------ one dispatch ---


async def test_the_process_performs_a_queued_dispatch(
    scenario: Scenario, owner_database: Database
) -> None:
    """The gap this process closes, in one test.

    Before it existed a deployment could publish a workflow, start an
    execution, write a dispatch row - and nothing would ever claim it. The
    run route's acceptance criterion ("calls a real provider … through
    `curl`, with no frontend") was undemonstrable for that reason.
    """
    execution_id = scenario.run()

    result = await worker_module.work_once(_settings_for())

    assert result.found_work is True
    assert result.dispatch_id is not None
    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.SUCCEEDED
    _, step = await _states(owner_database, execution_id)
    assert step == StepStatus.SUCCEEDED
    assert await _completed(owner_database, execution_id)


async def test_an_empty_queue_is_not_a_failure(scenario: Scenario) -> None:
    """Nothing to do is the ordinary case, and exits 0.

    `scenario` is built but not run, so the workspace and its workflow exist
    with no dispatch queued - which is what a quiet deployment looks like.
    """
    result = await worker_module.work_once(_settings_for())

    assert result.found_work is False
    assert result.dispatch_id is None


async def test_once_mode_exits_zero_with_nothing_to_do(scenario: Scenario) -> None:
    assert await worker_module._main(["--once"]) == 0


async def test_once_mode_exits_one_when_a_turn_raises(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure is reported to the supervisor, not swallowed."""
    scenario.run()

    async def explode(self: Any) -> Any:
        raise RuntimeError("the turn failed")

    monkeypatch.setattr(DispatchWorker, "run_once", explode)

    assert await worker_module._main(["--once"]) == 1


# ------------------------------------------------------------------ the loop ---


async def test_the_loop_drains_a_backlog_without_waiting_for_its_poll(
    scenario: Scenario, owner_database: Database
) -> None:
    """Two dispatches, an idle sleep of thirty seconds, both done at once.

    This is the one behavioural claim the loop makes that the service does
    not: a claim that *finds* work goes straight back for the next one, so a
    backlog drains at the speed of the work rather than the speed of the
    poll. With the sleep set to thirty seconds, a loop that waited between
    dispatches could not finish both inside the timeout.
    """
    first = scenario.run()
    second = scenario.run()

    settings = _settings_for(idle=30.0)
    task = asyncio.create_task(worker_module.run_forever(settings))
    try:

        async def both_done() -> None:
            while not (
                await _completed(owner_database, first) and await _completed(owner_database, second)
            ):
                await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(both_done(), timeout=25)
        except TimeoutError:  # pragma: no cover - the loop slept between dispatches
            pytest.fail("the loop waited out its idle sleep between dispatches")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_the_loop_stops_when_asked(
    scenario: Scenario, owner_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop finishes the dispatch in hand and then returns 0.

    The event is captured rather than signalled: `add_signal_handler` is not
    implemented on Windows, so a test that sent SIGTERM would pass on CI and
    do nothing here. Setting the same event the handler would set tests the
    path the handler uses.

    Cancelling instead would test cancellation, which is the thing this loop
    exists to avoid - an abandoned dispatch leaves a lease held, and if the
    outbound call had started, only a human can resolve it.
    """
    execution_id = scenario.run()
    captured: list[asyncio.Event] = []

    def capture(stopping: asyncio.Event) -> None:
        captured.append(stopping)

    monkeypatch.setattr(worker_module, "install_stop_handlers", capture)

    task = asyncio.create_task(worker_module.run_forever(_settings_for()))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if captured and await _completed(owner_database, execution_id):
            break
    else:  # pragma: no cover - the loop never performed the dispatch
        task.cancel()
        pytest.fail("the loop never got to the queued dispatch")

    captured[0].set()

    assert await asyncio.wait_for(task, timeout=30) == 0


async def test_the_loop_gives_up_after_consecutive_failures(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker spinning against a refusing database is one nobody notices.

    Bounded, and the bound is the point: without the ceiling `run_forever`
    never returns, so an unbounded await here would hang the suite rather
    than fail it. That lesson cost two and a half hours on T6a.
    """
    calls = {"n": 0}

    async def explode(self: Any) -> Any:
        calls["n"] += 1
        raise RuntimeError("the database is refusing")

    monkeypatch.setattr(DispatchWorker, "run_once", explode)

    try:
        exit_code = await asyncio.wait_for(
            worker_module.run_forever(_settings_for(max_errors=3)), timeout=30
        )
    except TimeoutError:  # pragma: no cover - the ceiling is gone
        pytest.fail("the loop never gave up")

    assert exit_code == 1
    assert calls["n"] == 3, "the ceiling is not the number of consecutive failures"


# ----------------------------------------------------------------- the lease ---


async def test_the_lease_comes_from_settings(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """An operator raising the provider timeout must be able to raise this too.

    Observed while the provider holds the request open, which is the only
    moment the lease is visible. The bound is loose on purpose - the claim
    happens somewhere inside the window - but 3600 cannot be mistaken for
    the 120 default.
    """
    execution_id = scenario.run()
    provider.hold = asyncio.Event()

    task = asyncio.create_task(worker_module.work_once(_settings_for(lease=3600)))
    try:
        await asyncio.wait_for(provider.arrived.wait(), timeout=30)
        async with owner_database.session() as session:
            remaining = (
                await session.execute(
                    text(
                        "SELECT extract(epoch from (leased_until - now())) "
                        "FROM dispatches WHERE execution_id = :id"
                    ),
                    {"id": str(execution_id)},
                )
            ).scalar_one()
        assert remaining is not None
        assert 3000 < float(remaining) <= 3600, f"the lease ignored the setting: {remaining}s"
    finally:
        provider.hold.set()
        done, _ = await asyncio.wait([task], timeout=30)
        if not done:  # pragma: no cover - the turn never returned
            task.cancel()

    assert await task is not None
    assert await _completed(owner_database, execution_id)


async def test_a_second_worker_finds_nothing_while_the_first_holds_the_lease(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
) -> None:
    """Running several processes is safe, which is how this scales.

    The service test proves `FOR UPDATE SKIP LOCKED` at the repository. This
    proves the claim survives being made from two separate processes, each
    with its own pool and its own client, which is the deployment shape.
    """
    scenario.run()
    provider.hold = asyncio.Event()

    first = asyncio.create_task(worker_module.work_once(_settings_for()))
    try:
        await asyncio.wait_for(provider.arrived.wait(), timeout=30)
        second = await worker_module.work_once(_settings_for())
        assert second.found_work is False, "two processes claimed the same dispatch"
    finally:
        provider.hold.set()
        done, _ = await asyncio.wait([first], timeout=30)
        if not done:  # pragma: no cover - the first turn never returned
            first.cancel()

    assert (await first).found_work is True

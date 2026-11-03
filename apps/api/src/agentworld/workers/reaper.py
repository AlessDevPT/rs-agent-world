"""The recovery process: take back lost leases, and finish stranded advances.

Two jobs, one process, because they recover from the same kind of event - a
worker that stopped existing part-way through something - and they fail for
the same reason, the database being unreachable. Splitting them would double
the deployment surface without buying independence.

1. **Lease recovery** (T6a). A dispatch whose worker died stays `leased` for
   ever unless something takes the lease back. Every `REAPER_INTERVAL_SECONDS`.
2. **Advancement reconciliation** (T6c). Settling a dispatch and advancing its
   execution are two transactions; a process that dies between them leaves an
   execution that is correct, consistent and stalled. Every
   `RECONCILE_INTERVAL_SECONDS`, which is longer, because that window is
   milliseconds wide and what it strands is quiet rather than holding a lease.

```
python -m agentworld.workers.reaper            # loop, sleeping between sweeps
python -m agentworld.workers.reaper --once     # one sweep, then exit
```

**This must run somewhere.** Without it a crashed worker's dispatch stays
`leased` for ever and its execution never progresses - which was the state
T6 shipped in, and the single most important thing this module fixes. The
lease is 120 seconds and the default interval is 30, so a crashed worker's
row moves within about a quarter of the window rather than at the end of it.
Without the reconciliation pass an execution whose advance was interrupted
stays `running` with nothing in flight, which no amount of lease recovery
fixes because there is no lease involved.

**Not in the API lifespan**, and that was a decision rather than an
oversight. A reaper inside a request-serving process competes with request
handling for the connection pool, dies mid-sweep in a rolling deploy, and
runs once per replica. One job, one process.

**Two modes, one code path.** The loop is what a long-running deployment
runs; `--once` is what a cron entry or a Kubernetes CronJob calls. They
differ in whether there is a sleep, so the scheduled deployment is not a
second implementation that can drift from the first.

**Running two is wasteful, not unsafe.** `expire_leases` selects
`FOR UPDATE SKIP LOCKED`, so a second reaper skips a row the first holds
rather than blocking on it, and `reclaim` is guarded on the lease still
being expired. Measured with four concurrent sweeps against one expired
lease: exactly one recovery. The reconciliation pass is safe for a different
reason: `advance_once` takes the execution's row lock, so two reconcilers
serialise rather than skip, and the loser finds nothing to decide.

There is deliberately no advisory lock on either job - that would be
efficiency dressed as safety, and the scans are cheap until a deployment is
large enough to want notification instead of polling.

**Exit codes.** 0 when a sweep succeeded, 1 when it did not. The loop exits 1
after `REAPER_MAX_CONSECUTIVE_ERRORS` sweeps in a row have failed, rather
than spinning against a database that is refusing it: a supervisor restarting
a process is visible, and a process quietly retrying for ever is not.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
from dataclasses import dataclass

from agentworld.infra.db import Database
from agentworld.infra.logging import configure_logging, get_logger
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.dispatch import LeaseReaper
from agentworld.services.executions import ExecutionService
from agentworld.services.reconcile import AdvanceReconciler, ReconcileResult
from agentworld.settings import Settings, get_settings
from agentworld.workers._signals import install_stop_handlers

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one sweep did, across every workspace.

    `returned` and `escalated` are the lease recovery. `reconciled` is the
    T6c advancement pass, which runs in this same process on its own
    interval; it is None only if a caller asked for the lease sweep alone.
    """

    returned: int
    escalated: int
    reconciled: ReconcileResult | None = None

    @property
    def total(self) -> int:
        """Leases recovered. Deliberately not counting advancements.

        A recovered lease and a resumed execution are different events with
        different causes, and summing them would hide which one happened.
        """
        return self.returned + self.escalated


async def sweep_once(settings: Settings | None = None) -> SweepResult:
    """One pass over every workspace. Opens and disposes its own pool.

    Used by `--once`, where the process is about to exit anyway, and by
    tests. The loop below holds one pool across sweeps instead.
    """
    resolved = settings or get_settings()
    database = Database(resolved.database_url)
    factory = UnitOfWorkFactory(database)
    try:
        returned, escalated = await LeaseReaper(factory).sweep(limit=resolved.reaper_batch_limit)
        # Both recoveries, because a scheduled invocation is the only thing
        # that will run in this deployment shape and an execution stalled
        # between two transactions needs someone to finish the advance.
        reconciled = await _reconciler(factory, resolved).sweep(
            limit=resolved.reconcile_batch_limit
        )
    finally:
        await database.dispose()
    return SweepResult(returned=returned, escalated=escalated, reconciled=reconciled)


async def run_forever(settings: Settings | None = None) -> int:
    """Sweep every `REAPER_INTERVAL_SECONDS` until told to stop.

    Holds one connection pool for the life of the process, because opening
    one per sweep would spend more time connecting than reaping.

    SIGINT and SIGTERM finish the sweep in progress and then return, so a
    deploy does not abandon a half-applied recovery - though a torn sweep
    would be safe anyway: every guard is on the row, not on the schedule.
    """
    resolved = settings or get_settings()
    database = Database(resolved.database_url)
    factory = UnitOfWorkFactory(database)
    reaper = LeaseReaper(factory)
    reconciler = _reconciler(factory, resolved)
    stopping = asyncio.Event()
    install_stop_handlers(stopping)

    log.info(
        "reaper_starting",
        interval_seconds=resolved.reaper_interval_seconds,
        batch_limit=resolved.reaper_batch_limit,
        reconcile_interval_seconds=resolved.reconcile_interval_seconds,
        reconcile_batch_limit=resolved.reconcile_batch_limit,
    )
    consecutive_errors = 0
    # Due immediately, so a process that starts after a crash does not wait
    # an interval before looking for what the crash stranded.
    reconcile_due = time.monotonic()
    try:
        while not stopping.is_set():
            try:
                returned, escalated = await reaper.sweep(limit=resolved.reaper_batch_limit)
                if returned or escalated:
                    log.info("reaper_swept", returned=returned, escalated=escalated)
                if time.monotonic() >= reconcile_due:
                    # Rescheduled before the await, not after: a reconcile
                    # that raises must not be retried on every tick.
                    reconcile_due = time.monotonic() + resolved.reconcile_interval_seconds
                    await reconciler.sweep(limit=resolved.reconcile_batch_limit)
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                log.exception("reaper_sweep_failed", consecutive=consecutive_errors)
                if consecutive_errors >= resolved.reaper_max_consecutive_errors:
                    log.error("reaper_giving_up", consecutive=consecutive_errors)
                    return 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=resolved.reaper_interval_seconds)
    finally:
        await database.dispose()
        log.info("reaper_stopped")
    return 0


def _reconciler(factory: UnitOfWorkFactory, settings: Settings) -> AdvanceReconciler:
    """The T6c pass, in this process rather than one of its own.

    It shares the loop, the error ceiling, the `--once` mode and the
    operational story T6a already built and documented. A second process
    would double the deployment surface to recover from a window measured in
    milliseconds, and the two jobs fail for the same reason - the database
    being unreachable - so splitting them would not buy independence either.
    """
    return AdvanceReconciler(
        factory,
        executions=ExecutionService(factory),
        grace_seconds=settings.reconcile_grace_seconds,
    )


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentworld-reaper",
        description="Take back dispatch leases whose worker is gone.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Sweep once and exit. For a cron entry or a CronJob.",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if not arguments.once:
        return await run_forever(settings)

    try:
        result = await sweep_once(settings)
    except Exception:
        log.exception("reaper_sweep_failed")
        return 1
    log.info(
        "reaper_swept",
        returned=result.returned,
        escalated=result.escalated,
        reconciled=result.reconciled.advanced if result.reconciled else 0,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_main(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The reaper process: take back leases whose worker is gone.

```
python -m agentworld.workers.reaper            # loop, sleeping between sweeps
python -m agentworld.workers.reaper --once     # one sweep, then exit
```

**This must run somewhere.** Without it a crashed worker's dispatch stays
`leased` for ever and its execution never progresses - which was the state
T6 shipped in, and the single most important thing this module fixes. The
lease is 120 seconds and the default interval is 30, so a crashed worker's
row moves within about a quarter of the window rather than at the end of it.

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
lease: exactly one recovery. There is deliberately no advisory lock - that
would be efficiency dressed as safety, and the scan is cheap until a
deployment is large enough to want notification instead of polling.

**Exit codes.** 0 when a sweep succeeded, 1 when it did not. The loop exits 1
after `REAPER_MAX_CONSECUTIVE_ERRORS` sweeps in a row have failed, rather
than spinning against a database that is refusing it: a supervisor restarting
a process is visible, and a process quietly retrying for ever is not.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from dataclasses import dataclass

from agentworld.infra.db import Database
from agentworld.infra.logging import configure_logging, get_logger
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.dispatch import LeaseReaper
from agentworld.settings import Settings, get_settings

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one sweep did, across every workspace."""

    returned: int
    escalated: int

    @property
    def total(self) -> int:
        return self.returned + self.escalated


async def sweep_once(settings: Settings | None = None) -> SweepResult:
    """One pass over every workspace. Opens and disposes its own pool.

    Used by `--once`, where the process is about to exit anyway, and by
    tests. The loop below holds one pool across sweeps instead.
    """
    resolved = settings or get_settings()
    database = Database(resolved.database_url)
    try:
        returned, escalated = await LeaseReaper(UnitOfWorkFactory(database)).sweep(
            limit=resolved.reaper_batch_limit
        )
    finally:
        await database.dispose()
    return SweepResult(returned=returned, escalated=escalated)


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
    reaper = LeaseReaper(UnitOfWorkFactory(database))
    stopping = asyncio.Event()
    _install_signal_handlers(stopping)

    log.info(
        "reaper_starting",
        interval_seconds=resolved.reaper_interval_seconds,
        batch_limit=resolved.reaper_batch_limit,
    )
    consecutive_errors = 0
    try:
        while not stopping.is_set():
            try:
                returned, escalated = await reaper.sweep(limit=resolved.reaper_batch_limit)
                consecutive_errors = 0
                if returned or escalated:
                    log.info("reaper_swept", returned=returned, escalated=escalated)
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


def _install_signal_handlers(stopping: asyncio.Event) -> None:
    """Ask the loop to finish its sweep and return.

    Best-effort: `add_signal_handler` is not implemented on Windows, where a
    developer running this in a terminal gets KeyboardInterrupt instead -
    which is the same outcome by a different route.
    """
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stopping.set)


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
    log.info("reaper_swept", returned=result.returned, escalated=result.escalated)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_main(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

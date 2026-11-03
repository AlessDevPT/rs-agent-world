"""The dispatch worker process: perform the work the outbox has queued.

    python -m agentworld.workers.worker            # loop, polling for work
    python -m agentworld.workers.worker --once     # one dispatch, then exit

**Nothing performed a dispatch before this existed.** T6 built
`DispatchWorker.run_once` deliberately as one unit of work rather than a
`while True`, so it could be tested without a timer - and then no process
ever called it. Every part of T0-T6c was exercised only by the test suite: a
real deployment could publish a workflow, start an execution and write a
dispatch row that nothing would ever claim. This is the loop that was
missing, and `04-ROADMAP-AND-MVP.md`'s "calls a real provider … through
`curl`, with no frontend" criterion cannot be demonstrated without it.

**Not in the API lifespan**, for the same reasons T6a kept the reaper out of
it: a worker inside a request-serving process competes with request handling
for the connection pool, dies mid-dispatch in a rolling deploy, and runs once
per replica. Worse here than for the reaper, because dying mid-dispatch can
mean dying mid-call, and a lease that expires with `side_effect_started_at`
set costs a human a decision. One job, one process.

**Two modes, one code path.** The loop is what a long-running deployment
runs; `--once` performs at most one dispatch and is what a test or a
scheduled invocation calls. They differ in whether there is a sleep.

**Sleeps only when idle.** A claim that returns work loops straight back:
a queue with a hundred dispatches should drain at the speed of the work, not
at the speed of the poll. A claim that returns nothing waits
`WORKER_IDLE_SLEEP_SECONDS`. That interval is the floor on how long a new
dispatch waits before anything notices it, and the cost of shortening it is
one claim query *per workspace* per poll - `_claim` walks the live workspace
list until one yields work, because the outbox is under forced row-level
security. Polling is T6a's deliberate choice; notification infrastructure is
still premature.

**Running several is safe, and is how this scales.** Claiming is
`FOR UPDATE SKIP LOCKED` under a lease, so two workers never hold the same
dispatch; the rotating cursor inside `DispatchWorker` keeps a busy workspace
from starving the others. Nothing here coordinates with anything else.

**One client for the process.** `GuardedClient` holds a connection pool and
its docstring asks for one instance per process rather than per call. It is
built once here and closed on the way out.

**Exit codes.** 0 when the work was done or there was none, 1 when a
dispatch could not be performed in `--once`, and 1 from the loop after
`WORKER_MAX_CONSECUTIVE_ERRORS` failures in a row - rather than spinning
against a database that is refusing it. A supervisor restarting a process is
visible; a process quietly retrying for ever is not.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass

from agentworld.infra.db import Database
from agentworld.infra.logging import configure_logging, get_logger
from agentworld.infra.net import GuardedClient
from agentworld.infra.secrets import EnvelopeSecretStore
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.credentials import CredentialService
from agentworld.services.dispatch import DispatchWorker, StepRunner
from agentworld.services.executions import ExecutionService
from agentworld.settings import Settings, get_settings
from agentworld.workers._signals import install_stop_handlers

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkResult:
    """Whether one turn of the loop found anything to do.

    `dispatch_id` is None when the queue was empty, which is the ordinary
    case and not a failure - the distinction `--once` needs in order to exit
    0 either way.
    """

    dispatch_id: str | None

    @property
    def found_work(self) -> bool:
        return self.dispatch_id is not None


def _worker(
    factory: UnitOfWorkFactory, client: GuardedClient, settings: Settings
) -> DispatchWorker:
    """The worker, with the production dependencies.

    The egress policy is `EgressPolicy()`'s documented defaults, which block
    loopback and the metadata addresses; there is no environment variable for
    `allowed_networks` yet, so a deployment that needs one has nowhere to put
    it. Noted rather than invented here.
    """
    return DispatchWorker(
        factory,
        runner=StepRunner(
            factory,
            credentials=CredentialService(
                factory, store=EnvelopeSecretStore(settings.credential_keyring)
            ),
            client=client,
        ),
        executions=ExecutionService(factory),
        lease_seconds=settings.worker_lease_seconds,
    )


async def work_once(settings: Settings | None = None) -> WorkResult:
    """Claim and perform at most one dispatch. Opens and disposes its own pool.

    For `--once` and for tests. The loop below holds one pool and one client
    across many dispatches instead.
    """
    resolved = settings or get_settings()
    database = Database(resolved.database_url)
    client = GuardedClient()
    try:
        claimed = await _worker(UnitOfWorkFactory(database), client, resolved).run_once()
    finally:
        await client.aclose()
        await database.dispose()
    return WorkResult(dispatch_id=str(claimed.id) if claimed is not None else None)


async def run_forever(settings: Settings | None = None) -> int:
    """Claim, perform, repeat until told to stop.

    SIGINT and SIGTERM finish the dispatch in progress and then return. That
    matters more here than in the reaper: abandoning a claimed dispatch
    leaves a lease held until it expires, and if the outbound call had
    started, the reaper can only escalate it to `needs_review`. Stopping
    between dispatches costs nothing.
    """
    resolved = settings or get_settings()
    database = Database(resolved.database_url)
    client = GuardedClient()
    worker = _worker(UnitOfWorkFactory(database), client, resolved)
    stopping = asyncio.Event()
    install_stop_handlers(stopping)

    log.info(
        "worker_starting",
        worker=worker.name,
        idle_sleep_seconds=resolved.worker_idle_sleep_seconds,
        lease_seconds=resolved.worker_lease_seconds,
    )
    consecutive_errors = 0
    try:
        while not stopping.is_set():
            try:
                claimed = await worker.run_once()
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                log.exception("worker_turn_failed", consecutive=consecutive_errors)
                if consecutive_errors >= resolved.worker_max_consecutive_errors:
                    log.error("worker_giving_up", consecutive=consecutive_errors)
                    return 1
                claimed = None

            if claimed is not None:
                # Straight back for the next one: a backlog drains at the
                # speed of the work rather than the speed of the poll.
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=resolved.worker_idle_sleep_seconds)
    finally:
        await client.aclose()
        await database.dispose()
        log.info("worker_stopped", worker=worker.name)
    return 0


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentworld-worker",
        description="Perform the dispatches the outbox has queued.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Perform at most one dispatch and exit. For a test or a scheduled run.",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if not arguments.once:
        return await run_forever(settings)

    try:
        result = await work_once(settings)
    except Exception:
        log.exception("worker_turn_failed")
        return 1
    log.info("worker_turn_done", dispatch_id=result.dispatch_id, found_work=result.found_work)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_main(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

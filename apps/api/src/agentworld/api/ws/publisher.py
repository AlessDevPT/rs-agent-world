"""Publication: committed event rows out to the bus, after the fact.

This is the half of P9 that was missing. Every definition change already
appends its `events` row **in the same transaction as the mutation** - that is
the durability half, and the fourteen producers of P13 do it. What nothing did
was publish: turn a committed row into frames and hand them to the bus. R8's
sentence has both halves, and a fire-and-forget publish inside the mutation's
own transaction satisfies neither - it would announce a change that a rollback
then un-made, and lose one to a crash between the commit and the send.

**The committed row is the publication record** (P10, Option A). There is no
outbox table and nothing to keep in step with one: the row is already durable,
already per-project and already ordered by the cursor the protocol carries. All
this needs is a position, which is `event_publication_cursors` (P10.4) - in
PostgreSQL, because Redis is the transport and never the source of truth for
how far publication has got.

**Publication is at-least-once, and nothing here claims otherwise.** A crash
between the publish and the position write re-sends those frames on the next
pass. That is correct rather than tolerated: every frame carries its `seq`, so
a client that has applied N discards anything at or below it, and ordering
stays exact. Claiming exactly-once would need the send and the position write
in one transaction, which they cannot be - one of them is a network call to
another process.

**Where this runs, which P10.5 left conditional and this settles.**

The publisher has to share a process with whoever can receive from the bus:

- On the **`redis`** bus - the acceptance bus (P4) and the only one allowed
  outside local and test - the worker hosts it, as P10.5 asks. It runs as its
  own task beside the dispatch loop rather than inside it: a thirty-second
  provider call must not stall a quarter-second publish, and - the limitation
  P10.5 asks to be written down if it exists - **the worker's
  `WORKER_MAX_CONSECUTIVE_ERRORS` give-up bound must not apply to this loop.**
  It is right for dispatches, where a worker spinning against a database that
  is refusing it should hand the problem to a supervisor, and wrong for a
  publisher that must keep trying through a Redis outage. So this loop counts
  its own failures, logs them and keeps going, and a failure here never exits
  the process or stops a dispatch. Nothing is lost by that: the position is in
  PostgreSQL, so an outage of any length costs latency only.
- On the **`inprocess`** bus there is nothing for a worker to publish *to* - a
  message put on that bus reaches the sockets of the process that owns it and
  no others, which is exactly why `Settings` refuses it outside local and
  test. So the API process runs the publisher for itself, in its lifespan.
  That is not a second host for the same job; it is the same job in the only
  process that can do it when the bus does not cross processes.

**One pass is a unit of work**, like `DispatchWorker.run_once`: `publish_once`
returns how many rows it published and sleeps nowhere. The loop is a thin
wrapper over it, and a test drives the unit rather than a timer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from agentworld.api.ws.frames import FrameBuilder, bus_message
from agentworld.infra.event_bus import EventBus, channel_for
from agentworld.infra.logging import get_logger
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What one pass did. Zero rows is the ordinary case, not a failure."""

    projects: int
    rows: int


class RealtimePublisher:
    """Reads committed event rows and publishes their frames."""

    def __init__(
        self,
        factory: UnitOfWorkFactory,
        bus: EventBus,
        *,
        batch_rows: int = 200,
    ) -> None:
        self._factory = factory
        self._bus = bus
        self._batch = batch_rows

    async def publish_once(self) -> PublishResult:
        """One pass over every project with unpublished rows.

        Workspace by workspace, because the tables are under forced row-level
        security and a sweep has to be scoped to be able to read at all - the
        same shape `DispatchWorker._claim` and the reconciler use.
        """
        async with self._factory.begin() as work:
            workspaces = list(await work.workspaces.live_ids())

        projects = 0
        rows = 0
        for workspace_id in workspaces:
            for project_id in await self._projects_behind(workspace_id):
                published = await self.publish_project(workspace_id, project_id)
                if published:
                    projects += 1
                    rows += published
        return PublishResult(projects=projects, rows=rows)

    async def publish_project(self, workspace_id: UUID, project_id: UUID) -> int:
        """Publish one project's unpublished rows. Returns how many.

        The read, the frame building and the position write are **three**
        pieces of work in two transactions, and the order is the point:

        1. One transaction reads the position and the rows after it, and
           builds the frames. It holds no lock on anything a writer wants -
           the project's cursor row is deliberately untouched (migration
           0027), so ingest and definition writes carry on beside this.
        2. The frames go to the bus, outside any transaction. A publish that
           happened inside one would hold a connection open across a network
           call to another process.
        3. A second, short transaction records the position.

        A crash at any point re-sends from the last recorded position, which
        is the at-least-once property this is built on rather than a hole in
        it.
        """
        scope = TenantScope(workspace_id, project_id)
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            position = await work.publication_cursor(scope).position()
            event_rows = await work.event_reads(scope).after(position, limit=self._batch)
            if not event_rows:
                return 0
            builder = FrameBuilder(work, scope)
            messages = [
                (row.seq, bus_message(row.seq, await builder.frames_for(row))) for row in event_rows
            ]

        channel = channel_for(project_id)
        highest = 0
        for seq, message in messages:
            # Published one row at a time and in cursor order, so a consumer
            # that is dropped partway through has still seen a prefix rather
            # than a scattering. The bus never blocks on a slow consumer, so
            # this loop runs at the speed of the transport.
            await self._bus.publish(channel, json.dumps(message, separators=(",", ":")))
            highest = seq

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            await work.publication_cursor(scope).record(highest)
        return len(messages)

    async def _projects_behind(self, workspace_id: UUID) -> Sequence[UUID]:
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            return await work.publication_sweep(
                TenantScope(workspace_id)
            ).projects_with_unpublished_events()


async def run_publisher(
    publisher: RealtimePublisher,
    *,
    interval_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Publish until asked to stop. Never raises, and never gives up.

    The absence of a give-up bound is deliberate and is the limitation P10.5
    asks to be recorded. A publisher that exited after N consecutive failures
    would stop delivering deltas for the duration of a Redis restart and then
    need a supervisor to notice - while the thing it is protecting, the data,
    is not at risk: the position is in PostgreSQL and an outage costs latency.
    The failure is made visible by logging every one of them rather than by
    killing the process that is still doing useful work beside this.

    A pass that published something does not sleep, so a backlog drains at the
    speed of the database rather than at the speed of the poll.
    """
    failures = 0
    while not stop.is_set():
        try:
            result = await publisher.publish_once()
            if failures:
                log.info("realtime_publisher_recovered", after_failures=failures)
            failures = 0
            if result.rows:
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            log.warning("realtime_publish_failed", consecutive=failures, exc_info=True)
        await _sleep_or_stop(stop, interval_seconds)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Idle, but wake immediately on shutdown rather than at the next tick."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


def publishes_in_api(event_bus_kind: str) -> bool:
    """Whether the API process must run the publisher itself.

    True for `inprocess`, where no other process can reach the sockets this
    one holds. False for `redis`, where the worker hosts it (P10.5) and a
    second publisher would only duplicate sends that clients already discard.
    """
    return event_bus_kind != "redis"

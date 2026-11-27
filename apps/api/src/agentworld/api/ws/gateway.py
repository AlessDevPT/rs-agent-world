"""The WebSocket gateway: snapshot, then deltas, then whatever the client asks.

`GET /v1/ws/projects/{project_id}?ticket=<ticket>`, from `03-API.md` §6. The
client frames are `subscribe`, `unsubscribe`, `resume` and `ping`; the server
frames are `snapshot`, the deltas of `03-API-AND-FRONTEND.md` §4, `pong`,
`resync_required` and `error`. Nothing here adds to either set.

**No ticket, no stream** (R1). The socket is accepted only after the ticket has
been spent, and a refusal closes it with `error` before a single frame of state
is sent. That order matters: accepting first and refusing after would have
already told the caller the project exists.

**The handshake is subscribe-then-snapshot-then-replay** (R6). In that order,
and each step is there because the obvious shorter version loses or repeats
something:

1. **Subscribe to the bus first.** Anything published while the snapshot is
   being read is then already queued rather than missed.
2. **Snapshot inside a `REPEATABLE READ` transaction.** The cursor is the
   highest `seq` that transaction can see, and because the sequence is
   allocated under a lock held until commit, a visible `seq` implies every
   lower one is visible too - so one number really is the boundary between
   "in the snapshot" and "arrives as a delta". Under `READ COMMITTED` a
   mutation landing mid-read would appear in the snapshot *and* be replayed,
   which is the "appears exactly once" half of R6 failing.
3. **Replay the gap.** Rows after the snapshot's cursor are read from
   `events` and sent before the live stream starts, which closes the window
   between the snapshot's transaction and the first bus message - including
   the case where the bus dropped one.
4. **Then live frames**, discarding any whose `seq` the connection has
   already sent.

**Deduplication is per row, never per frame.** One event row can produce
several frames on one cursor - an ingested step outcome is `event` *and*
`step.updated` - so a gateway that remembered "the last cursor I sent" and
dropped anything at or below it would drop the second frame of every such row.
What is tracked is the last *row* sequence handled; a bus message for a row at
or below it is dropped whole, and a message for a row more than one ahead is a
hole that is closed from `events` before the message is used. The sequence is
gapless per project, so that test is sound rather than a heuristic.

**A slow consumer is dropped, not buffered** (R12). The subscription is
bounded (`WS_OUTBOUND_QUEUE_FRAMES`); when it overflows the connection has a
hole it cannot close by reading on, so it is sent `resync_required` and closed.
That is the same answer as a cursor beyond the replay window, for the same
reason: the only honest next move is a fresh snapshot.

**Nothing is fabricated** (R14). The server sends no unsolicited *frame* at
all - no heartbeat, no status, no synthetic delta. Every frame it sends traces
to an `events` row or to a client message.

**That is a statement about frames, not about the socket.** `03-API.md` §6's
frame list has `ping` client-to-server only and `pong` server-to-client only,
so there is no server heartbeat frame to implement and R14 forbids inventing
one. Its separate "server pings every 30s" guarantee is a transport keepalive,
and uvicorn performs it - `ws_ping_interval` and `ws_ping_timeout` both
default to 20 seconds and nothing here overrides them. So a dead socket is
still noticed; it is noticed one layer below this module, which is where a
keepalive belongs.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from collections.abc import Iterable, Sequence
from typing import Any, Final
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from agentworld.api.ws.frames import CHANNELS, Frame, FrameBuilder
from agentworld.domain.errors import DomainError
from agentworld.infra.event_bus import EventBus, Subscription, channel_for
from agentworld.infra.logging import get_logger
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.realtime import RealtimeTicketService, StreamGrant
from agentworld.settings import Settings

log = get_logger(__name__)

router = APIRouter(tags=["realtime"])

#: Sent and then closed when a cursor cannot be served. The reasons are the
#: two ways that happens, and they are distinguishable on purpose: a client
#: that asked for something too old and a client that could not keep up both
#: have to re-snapshot, but an operator reading logs wants to know which.
REASON_TOO_OLD: Final = "cursor_too_old"
REASON_TOO_SLOW: Final = "consumer_too_slow"

#: Closed with this when the ticket does not check out. 1008 is the WebSocket
#: "policy violation" code, which is as close as the protocol gets to 401 -
#: there is no HTTP status to return once a socket is being negotiated.
CLOSE_POLICY: Final = 1008
#: Closed with this when the gateway itself failed. 1011 is "internal error",
#: which is all a client can usefully be told.
CLOSE_INTERNAL: Final = 1011


@router.websocket("/v1/ws/projects/{project_id}")
async def stream(
    websocket: WebSocket,
    project_id: UUID,
    ticket: str = "",
    cursor: str | None = None,
) -> None:
    """One project's live stream.

    `cursor` as a query parameter is the connect-time form of the `resume`
    frame: a client that has one does not want a snapshot it will discard, and
    there is no way to decline the first frame after the socket is open. The
    `resume` frame itself is still honoured at any time, for a client that
    wants to re-read from a position mid-connection.
    """
    settings: Settings = websocket.app.state.settings
    factory: UnitOfWorkFactory = websocket.app.state.unit_of_work_factory
    bus: EventBus = websocket.app.state.event_bus

    tickets = RealtimeTicketService(factory, ttl_seconds=settings.ws_ticket_ttl_seconds)
    try:
        grant = await tickets.redeem(ticket, project_id=project_id)
    except DomainError as error:
        # Accepted only so the refusal can be *said*. A bare close gives a
        # browser `onerror` and nothing to show a user; the frame names the
        # reason, and no state has been read or sent.
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": "invalid_ticket"})
        await websocket.close(code=CLOSE_POLICY)
        log.info("ws_refused", project_id=str(project_id), reason=error.code)
        return

    await websocket.accept()
    connection = _Connection(websocket, factory, bus, settings, grant)
    try:
        await connection.run(resume_from=_int_or_none(cursor))
    except WebSocketDisconnect:
        pass
    except Exception:
        # A socket is not a request: there is no error handler above this and
        # no response to turn a traceback into, so an exception here would
        # otherwise reach the ASGI server as an unexplained 1011. Logged with
        # the project, then closed - and the client's next move is the same
        # one every other failure gives it, a fresh snapshot.
        log.exception("ws_failed", project_id=str(grant.project_id))
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_INTERNAL)
    finally:
        log.info(
            "ws_closed",
            project_id=str(grant.project_id),
            user_id=str(grant.user_id),
            last_seq=connection.last_seq,
        )


class _Connection:
    """One socket's state: what it is subscribed to and how far it has got."""

    def __init__(
        self,
        websocket: WebSocket,
        factory: UnitOfWorkFactory,
        bus: EventBus,
        settings: Settings,
        grant: StreamGrant,
    ) -> None:
        self._ws = websocket
        self._factory = factory
        self._bus = bus
        self._settings = settings
        self._grant = grant
        self._scope = TenantScope(grant.workspace_id, grant.project_id)
        self._channels: set[str] = set(CHANNELS)
        self.last_seq = 0

    async def run(self, *, resume_from: int | None) -> None:
        async with self._bus.subscribe(
            channel_for(self._grant.project_id),
            maxsize=self._settings.ws_outbound_queue_frames,
        ) as subscription:
            if resume_from is None:
                await self._send_snapshot()
            elif not await self._replay_from(resume_from):
                return
            await self._pump(subscription)

    # -------------------------------------------------------- the handshake ---

    async def _send_snapshot(self) -> None:
        async with self._factory.begin(
            visible_workspaces={self._grant.workspace_id},
            isolation_level="REPEATABLE READ",
        ) as work:
            frame = await FrameBuilder(work, self._scope).snapshot()
        await self._ws.send_json(frame.as_json())
        self.last_seq = frame.cursor
        # Rows committed between the snapshot's transaction and the bus
        # subscription, if any, are sent here rather than waited for. The
        # subscription was opened first, so this is belt and braces - and it
        # is what makes the handshake correct even when the bus lost a
        # message, which `inprocess` cannot do and Redis can.
        await self._replay_from(frame.cursor, window_checked=False)

    async def _replay_from(self, cursor: int, *, window_checked: bool = True) -> bool:
        """Send every delta after `cursor`. False means the socket is finished.

        The window is checked only for a cursor the *client* chose: a cursor
        this connection produced a moment ago is inside any window by
        construction, and re-checking it would let a project with a tiny
        window refuse its own snapshot.
        """
        if window_checked and not await self._within_window(cursor):
            await self._resync(REASON_TOO_OLD)
            return False

        # Before reading a single row. A resume from the head replays nothing,
        # and if that left `last_seq` at zero the next live message would look
        # like a gap of its whole seq - and be "closed" by replaying the
        # project's entire history to a client that had asked for none of it.
        # The client has told us where it is; believe it, then catch it up.
        self.last_seq = max(self.last_seq, cursor)
        position = cursor
        while True:
            async with self._factory.begin(visible_workspaces={self._grant.workspace_id}) as work:
                rows = await work.event_reads(self._scope).after(
                    position, limit=self._settings.realtime_publish_batch_rows
                )
                if not rows:
                    return True
                builder = FrameBuilder(work, self._scope)
                batch = [(row.seq, await builder.frames_for(row)) for row in rows]

            for seq, frames in batch:
                await self._send(frames)
                position = seq
                self.last_seq = max(self.last_seq, seq)

    async def _within_window(self, cursor: int) -> bool:
        """Whether a replay from `cursor` can be served honestly (R11).

        Two questions, and both have to be yes. `WS_REPLAY_WINDOW_EVENTS`
        bounds how far back a cursor may *ask* - which is what bounds the work
        a reconnecting client can demand of the database. And the oldest
        retained row bounds how far back the rows actually go, which is what a
        dropped partition changes: `events` is partitioned monthly and nothing
        prunes a partition today, so that half is the one that will start
        mattering when retention arrives, and it is checked now so that
        arriving is not also a protocol change.

        A negative cursor is nonsense rather than old, and a cursor ahead of
        the project's latest is a client that has seen the future - both are
        refused the same way, because the answer to all three is a fresh
        snapshot.
        """
        if cursor < 0:
            return False
        async with self._factory.begin(visible_workspaces={self._grant.workspace_id}) as work:
            reads = work.event_reads(self._scope)
            latest = await reads.latest_seq()
            if cursor > latest:
                return False
            if latest - cursor > self._settings.ws_replay_window_events:
                return False
            oldest = await reads.oldest_seq()
        # `oldest is None` means nothing is retained, so there is nothing to
        # replay and no gap either: a cursor of 0 on an empty project is
        # served, not refused.
        return oldest is None or oldest <= cursor + 1

    # ------------------------------------------------------------ the pump ---

    async def _pump(self, subscription: Subscription) -> None:
        """Live frames and client frames, in one coroutine.

        One coroutine rather than a reader task and a writer task, and that is
        not merely simpler. Every frame this connection sends leaves from here,
        so the order is defined: pending bus frames are drained before a client
        message is answered. A client that sends `ping` therefore receives
        `pong` *after* everything the server had already queued for it, which
        is what makes the pong a usable fence - for a client, and for every
        test that has to assert "and nothing else".
        """
        receive = asyncio.ensure_future(self._ws.receive_text())
        pending = asyncio.ensure_future(subscription.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {receive, pending}, return_when=asyncio.FIRST_COMPLETED
                )
                if pending in done:
                    if subscription.overflowed:
                        await self._resync(REASON_TOO_SLOW)
                        return
                    await self._deliver(pending.result())
                    pending = asyncio.ensure_future(subscription.get())
                    # Back to the top even when a client message is also
                    # ready: bus frames first, always.
                    continue
                if subscription.overflowed:
                    await self._resync(REASON_TOO_SLOW)
                    return
                if receive in done:
                    if not await self._handle_client(receive.result()):
                        return
                    receive = asyncio.ensure_future(self._ws.receive_text())
        finally:
            for task in (receive, pending):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _deliver(self, raw: str) -> None:
        """One bus message: a row's frames, or a hole to close first."""
        try:
            message = json.loads(raw)
            seq = int(message["seq"])
            frames = message["frames"]
        except (ValueError, KeyError, TypeError):  # pragma: no cover - own publisher
            log.warning("ws_bus_message_unreadable", project_id=str(self._grant.project_id))
            return

        if seq <= self.last_seq:
            # Already sent, from the replay or from a duplicate publish.
            # Publication is at-least-once (P10), so this is the expected
            # path rather than an error, and dropping the whole message is
            # what keeps a multi-frame row from being half-applied twice.
            return
        if seq > self.last_seq + 1:
            # A hole: the bus dropped something, or the publisher restarted
            # mid-project. The rows are in `events`, so it is closed from
            # there rather than guessed at.
            if not await self._replay_from(self.last_seq, window_checked=False):
                return
            if seq <= self.last_seq:
                return

        await self._send(
            Frame(
                channel=str(entry["channel"]),
                type=str(entry["frame"]["type"]),
                cursor=int(entry["frame"]["cursor"]),
                data=entry["frame"]["data"],
            )
            for entry in frames
        )
        self.last_seq = max(self.last_seq, seq)

    async def _handle_client(self, raw: str) -> bool:
        """Answer one client frame. False closes the socket."""
        try:
            message = json.loads(raw)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            # Not just unparseable JSON: a well-formed `[1, 2]` has no `type`
            # to dispatch on either, and a public endpoint must answer that
            # rather than raise on it. Answered and kept open, because one
            # bad frame is a client bug and not a reason to drop a stream.
            await self._ws.send_json({"type": "error", "code": "malformed_frame"})
            return True
        kind = str(message.get("type", ""))

        if kind == "ping":
            await self._ws.send_json(
                {
                    "type": "pong",
                    "server_time": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
                }
            )
            return True
        if kind == "subscribe":
            # Additive, not a replacement. `03-API.md` §6 has a separate
            # `unsubscribe` frame and describes "an open log viewer adds
            # `events`", so a `subscribe` that narrowed would make the second
            # frame redundant and the first surprising.
            self._channels |= _channels_of(message)
            return True
        if kind == "unsubscribe":
            self._channels -= _channels_of(message)
            return True
        if kind == "resume":
            cursor = _int_or_none(message.get("cursor"))
            if cursor is None:
                await self._ws.send_json({"type": "error", "code": "malformed_frame"})
                return True
            return await self._replay_from(cursor)
        await self._ws.send_json({"type": "error", "code": "unknown_frame"})
        return True

    # ----------------------------------------------------------- sending ---

    async def _send(self, frames: Iterable[Frame]) -> None:
        for frame in frames:
            if frame.channel in self._channels:
                await self._ws.send_json(frame.as_json())

    async def _resync(self, reason: str) -> None:
        """`resync_required`, and nothing else, then close (R11, R12).

        Nothing else is the criterion's own wording. A client's only next move
        is a fresh snapshot, so continuing to stream deltas it cannot place
        would be worse than silence.
        """
        with contextlib.suppress(Exception):
            await self._ws.send_json({"type": "resync_required", "reason": reason})
            await self._ws.close(code=CLOSE_POLICY)
        log.info("ws_resync_required", project_id=str(self._grant.project_id), reason=reason)


def _channels_of(message: Any) -> set[str]:
    """The known channels a `subscribe`/`unsubscribe` frame names.

    Unknown names are ignored rather than refused: a later release may add a
    channel, and a client that asks for one an older server has never heard
    of should get the channels that do exist rather than an error.
    """
    raw = message.get("channels") if isinstance(message, dict) else None
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return set()
    return {name for name in raw if isinstance(name, str) and name in CHANNELS}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None

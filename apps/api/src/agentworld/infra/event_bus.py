"""Fan-out from the publisher to the sockets. Two implementations, one shape.

`01-ARCHITECTURE.md` §5.2 asks for "an `EventBus` interface with two
implementations ... so the WebSocket gateway never knows which one it is
using", and P4 settles which one the acceptance run uses: **`redis`**, because
R13 - two gateway processes seeing identical frames - is the criterion
`inprocess` cannot satisfy. `inprocess` stays for local development, where
there is one process and Redis would be a dependency bought for nothing.

**The bus is a transport and never a source of truth** (P10.4). What it
carries is already committed: a row in `events`, turned into frames by the
publisher. A message lost to a Redis restart costs latency, not data - the
publisher's recorded position is in PostgreSQL, so the next pass sends it
again, and every frame carries the `seq` that lets a client discard what it
has already applied. Nothing here retries, acknowledges or persists, because
anything it did would be a second, weaker copy of that guarantee.

**Subscriptions are bounded, and that is the interesting part.** A consumer
that stops reading cannot be allowed to grow the server's memory, so each
subscription holds at most `maxsize` messages and *drops* the rest, recording
that it did. It does not block the publisher: one slow socket must not stall
the project's whole fan-out, which is what back-pressure through this queue
would mean. A subscription that has dropped anything is poisoned by design -
its sequence now has a hole, and the only honest answer to a hole is
`resync_required` (R12). The gateway asks `overflowed` and closes.

**Why the queue is here rather than in the gateway.** The drop has to happen
at the point where the message arrives, before anything is retained; a bound
applied one layer up would already be holding the backlog it is meant to
prevent. For `RedisEventBus` that is doubly true: the connection is shared by
every subscription in the process, so its reader task must never await a slow
consumer - one socket would otherwise stop delivery to all of them.
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final
from uuid import UUID

from agentworld.infra.logging import get_logger

log = get_logger(__name__)

#: The channel a project's frames travel on. `01-ARCHITECTURE.md` §5.2 names
#: the shape (`project:{project_id}`); it is a function rather than an f-string
#: at each call site so the publisher and the gateway cannot disagree about it.
CHANNEL_PREFIX: Final = "project:"


def channel_for(project_id: UUID) -> str:
    return f"{CHANNEL_PREFIX}{project_id}"


class Subscription:
    """One consumer's bounded view of a channel.

    Not an iterator: `get()` is awaited and `overflowed` is read beside it,
    because the gateway has to be able to tell "nothing yet" from "something
    was lost". An `async for` can only express the first.
    """

    __slots__ = ("_overflowed", "_queue")

    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self._overflowed = False

    @property
    def overflowed(self) -> bool:
        """Whether anything was dropped. Never becomes False again.

        One drop is enough: the consumer's cursor now has a hole it cannot
        close by reading on, so the subscription is finished whatever happens
        next.
        """
        return self._overflowed

    def qsize(self) -> int:
        """How many messages are waiting. For the bound's own test."""
        return self._queue.qsize()

    def offer(self, message: str) -> bool:
        """Hand this subscription a message, dropping it if there is no room.

        Returns whether it was taken. Deliberately synchronous and
        non-blocking: a publisher that awaited here would be rate-limited by
        its slowest reader.
        """
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._overflowed = True
            return False
        return True

    async def get(self) -> str:
        return await self._queue.get()


class EventBus(ABC):
    """Publish to a channel, subscribe to a channel, close.

    An abstract base rather than a `Protocol` because both implementations
    want the same `subscribe` context-manager contract and the same close
    semantics, and inheriting them is how the two stay interchangeable in the
    way `01-ARCHITECTURE.md` §5.2 asks for.
    """

    @abstractmethod
    async def publish(self, channel: str, message: str) -> None:
        """Send one message. Returns once it has been handed to the transport."""

    @abstractmethod
    def subscribe(self, channel: str, *, maxsize: int) -> AbstractAsyncContextManager[Subscription]:
        """Listen to `channel` for as long as the context is open.

        The subscription starts receiving before the context is entered, so a
        caller that subscribes and *then* reads a snapshot cannot miss
        anything committed in between - which is the whole of R6's "loses
        nothing". Declared as a plain method returning a context manager
        rather than as a decorated generator, because `@asynccontextmanager`
        is each implementation's own business and an abstract declaration
        carrying it would bind both to the generator form.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release the transport. Idempotent."""


class InProcessEventBus(EventBus):
    """Fan-out within one process. Development, and the test suite.

    Cannot satisfy R13 and is not meant to: a message published here reaches
    the sockets this process holds and no others, which is exactly why
    `Settings` refuses it outside `local` and `test`. The consequence worth
    stating is where the publisher has to run - with this bus it must run *in
    the API process*, because a worker publishing into its own copy would
    reach nobody. See `api/ws/publisher.py`.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[Subscription]] = {}

    async def publish(self, channel: str, message: str) -> None:
        # No await between the offers: a burst published in one pass is
        # delivered to every subscription before any consumer is scheduled,
        # so a bound of N really does drop the N+1th. The gateway's R12
        # behaviour rests on that.
        for subscription in self._subscribers.get(channel, ()):
            subscription.offer(message)

    @asynccontextmanager
    async def subscribe(self, channel: str, *, maxsize: int) -> AsyncIterator[Subscription]:
        subscription = Subscription(maxsize)
        self._subscribers.setdefault(channel, set()).add(subscription)
        try:
            yield subscription
        finally:
            holders = self._subscribers.get(channel)
            if holders is not None:
                holders.discard(subscription)
                if not holders:
                    del self._subscribers[channel]

    async def aclose(self) -> None:
        self._subscribers.clear()


class RedisEventBus(EventBus):
    """Fan-out across processes, on Redis pub/sub. The acceptance bus (P4).

    One connection per process, one `PubSub` and one reader task per *channel*
    rather than per subscription: two sockets watching the same project share
    the reader, and the per-subscription bound is applied as the reader hands
    the message out. Pub/sub, not a stream: there is nothing to retain,
    because the retained copy is the `events` row the publisher read.

    `redis` is imported inside the methods that need it. The import contract
    forbids the name in `domain/`, and keeping it out of this module's import
    side effects means a process on the `inprocess` bus - every test but one,
    and every local run - never loads the client library at all.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: object | None = None
        self._readers: dict[str, _ChannelReader] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def _connect(self) -> object:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, channel: str, message: str) -> None:
        client = self._connect()
        await client.publish(channel, message)  # type: ignore[attr-defined]

    @asynccontextmanager
    async def subscribe(self, channel: str, *, maxsize: int) -> AsyncIterator[Subscription]:
        subscription = Subscription(maxsize)
        async with self._lock:
            reader = self._readers.get(channel)
            if reader is None:
                reader = _ChannelReader(self._connect(), channel)
                await reader.start()
                self._readers[channel] = reader
            reader.add(subscription)
        try:
            yield subscription
        finally:
            async with self._lock:
                reader.remove(subscription)
                if reader.idle and self._readers.get(channel) is reader:
                    del self._readers[channel]
                    await reader.stop()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            readers, self._readers = list(self._readers.values()), {}
        for reader in readers:
            await reader.stop()
        if self._client is not None:
            await self._client.aclose()  # type: ignore[attr-defined]
            self._client = None


class _ChannelReader:
    """One Redis subscription, shared by every consumer of one channel.

    Lives here rather than inside `RedisEventBus.subscribe` because the
    lifetime is the channel's, not any one consumer's: the first subscriber
    starts it and the last one stops it.
    """

    def __init__(self, client: object, channel: str) -> None:
        self._client = client
        self._channel = channel
        self._subscriptions: set[Subscription] = set()
        self._pubsub: object | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def idle(self) -> bool:
        return not self._subscriptions

    def add(self, subscription: Subscription) -> None:
        self._subscriptions.add(subscription)

    def remove(self, subscription: Subscription) -> None:
        self._subscriptions.discard(subscription)

    async def start(self) -> None:
        pubsub = self._client.pubsub()  # type: ignore[attr-defined]
        # Awaited before `subscribe()` returns, so a caller that subscribes
        # and then reads a snapshot cannot miss a message published in
        # between. A reader started lazily on the first `get()` would.
        await pubsub.subscribe(self._channel)
        self._pubsub = pubsub
        self._task = asyncio.create_task(self._pump(pubsub), name=f"bus:{self._channel}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.unsubscribe(self._channel)  # type: ignore[attr-defined]
                await self._pubsub.aclose()  # type: ignore[attr-defined]
            self._pubsub = None

    async def _pump(self, pubsub: object) -> None:
        """Hand each message to every subscription, dropping where full.

        Never awaits a consumer. The connection is shared, so blocking here
        for one slow socket would stop delivery to every other socket in the
        process - the failure the per-subscription bound exists to prevent,
        reintroduced one layer down.
        """
        while True:
            try:
                message = await pubsub.get_message(  # type: ignore[attr-defined]
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A dropped connection costs latency and not data: the
                # publisher's position is in PostgreSQL and will be read
                # again. Logged and retried rather than raised, because the
                # task that raises is one nobody is awaiting.
                log.warning("event_bus_reader_error", channel=self._channel, exc_info=True)
                await asyncio.sleep(0.5)
                continue
            if message is None:
                continue
            payload = message.get("data")
            if not isinstance(payload, str):
                continue
            for subscription in tuple(self._subscriptions):
                subscription.offer(payload)


def build_event_bus(kind: str, *, redis_url: str) -> EventBus:
    """The bus this process will use, chosen by `EVENT_BUS`.

    Takes the kind as a string rather than the whole settings object: this is
    `infra`, and the one thing it needs to know is which of two
    implementations to construct.
    """
    if kind == "redis":
        return RedisEventBus(redis_url)
    return InProcessEventBus()

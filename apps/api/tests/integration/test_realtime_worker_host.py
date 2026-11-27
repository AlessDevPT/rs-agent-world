"""Who hosts the publisher, asserted on the worker rather than inferred.

P10.5 settled that the worker hosts the publisher on the `redis` bus, and
`test_realtime_fanout.py` deliberately publishes from its *own*
`RealtimePublisher` - because two gateway processes, not three, is what R13
is about. That leaves the worker's half of the arrangement with nothing
exercising it: `test_worker_process.py` runs on `inprocess`, where
`_start_publisher` correctly does nothing at all.

So this file is the worker's half. It drives `workers.worker._start_publisher`
- the real decision point and the real loop - and asserts three things:

1. on `inprocess` it starts nothing, because a worker publishing into its own
   copy of that bus would reach no socket;
2. on `redis` it starts a loop that really publishes a committed row, which a
   subscriber on another bus instance receives;
3. the loop survives a failure rather than exiting, which is the limitation
   P10.5 asked to have written down: `WORKER_MAX_CONSECUTIVE_ERRORS` is right
   for dispatches and wrong for a publisher that must outlast a Redis restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from realtime_helpers import redis_reachable, require_redis

from agentworld.api.ws.publisher import PublishResult, RealtimePublisher, run_publisher
from agentworld.infra.event_bus import RedisEventBus, channel_for
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.settings import Settings
from agentworld.workers import worker as worker_module
from helpers import Actor

pytestmark = pytest.mark.integration

WAIT_SECONDS = 20.0


@pytest.fixture
def redis_url() -> str:
    return Settings().redis_url


@pytest.fixture
async def redis_gate(redis_url: str) -> None:
    require_redis(await redis_reachable(redis_url))


class World:
    def __init__(self, client: TestClient, actor: Actor) -> None:
        self.actor = actor
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Worker Host"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()
        self.agent = client.post(
            f"/v1/projects/{self.project['id']}/agents",
            json={
                "agent_key": "host-1",
                "display_name": "Hosted",
                "interaction_mode": "observed",
            },
            headers=actor.headers,
        ).json()

    @property
    def project_id(self) -> str:
        return str(self.project["id"])


def test_the_worker_starts_nothing_on_the_inprocess_bus(settings: Settings) -> None:
    """A worker publishing into its own in-process bus would reach no socket."""
    bus, task = worker_module._start_publisher(
        UnitOfWorkFactory.__new__(UnitOfWorkFactory),
        settings.model_copy(update={"event_bus": "inprocess"}),
        asyncio.Event(),
    )
    assert bus is None
    assert task is None


async def test_the_worker_publishes_a_committed_row_on_the_redis_bus(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    settings: Settings,
    redis_url: str,
    redis_gate: None,
) -> None:
    """The worker's publisher loop, started the way the worker starts it.

    `_start_publisher` is called rather than reimplemented, so this fails if
    the worker stops starting it, starts it for the wrong bus, or hands it the
    wrong interval - the three ways the hosting decision can rot.
    """
    world = World(client, actor)
    listener = RedisEventBus(redis_url)
    stopping = asyncio.Event()

    bus, task = worker_module._start_publisher(
        factory,
        settings.model_copy(
            update={
                "event_bus": "redis",
                "redis_url": redis_url,
                "realtime_publish_interval_seconds": 0.05,
            }
        ),
        stopping,
    )
    assert bus is not None and task is not None
    try:
        async with listener.subscribe(
            channel_for(uuid.UUID(world.project_id)), maxsize=64
        ) as subscription:
            renamed = client.patch(
                f"/v1/projects/{world.project_id}/agents/{world.agent['id']}",
                json={"display_name": "Published By The Worker"},
                headers=actor.headers,
            )
            assert renamed.status_code == 200, renamed.text

            frames = await _await_agent_delta(subscription)
    finally:
        stopping.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await bus.aclose()
        await listener.aclose()

    assert frames[0]["frame"]["type"] == "agent.updated"
    assert frames[0]["frame"]["data"]["display_name"] == "Published By The Worker"


async def test_the_publisher_loop_does_not_give_up_after_a_failure() -> None:
    """The limitation P10.5 asked to have recorded, asserted as behaviour.

    `WORKER_MAX_CONSECUTIVE_ERRORS` exits the dispatch loop after N failures
    in a row, which is right there and wrong here: a publisher that exited
    during a Redis restart would stop delivering deltas until a supervisor
    noticed, while nothing it holds is at risk - its position is in
    PostgreSQL. So it keeps going, however many times it fails, and that is
    what this pins.
    """
    attempts: list[int] = []

    class Failing(RealtimePublisher):
        def __init__(self) -> None:  # no factory, no bus: nothing is reached
            pass

        async def publish_once(self) -> PublishResult:
            attempts.append(len(attempts))
            if len(attempts) <= 6:
                raise RuntimeError("Redis is restarting")
            return PublishResult(projects=0, rows=0)

    stopping = asyncio.Event()
    task = asyncio.create_task(run_publisher(Failing(), interval_seconds=0.001, stop=stopping))
    for _ in range(int(WAIT_SECONDS / 0.01)):
        if len(attempts) > 8:
            break
        await asyncio.sleep(0.01)
    stopping.set()
    await asyncio.wait_for(task, timeout=5)

    # Six consecutive failures is past any give-up bound the worker uses, and
    # the loop was still running after them.
    assert len(attempts) > 8, f"the loop stopped after {len(attempts)} attempts"
    assert task.done() and task.exception() is None


async def _await_agent_delta(subscription: Any) -> list[dict[str, Any]]:
    """The next bus message carrying a definition delta.

    Bounded: a message that never comes fails the test rather than hanging
    the suite, which is the same rule the gateway tests' pong fence follows.
    """
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        raw = await asyncio.wait_for(subscription.get(), timeout=max(remaining, 0.1))
        frames = json.loads(raw)["frames"]
        if any(entry["frame"]["type"] == "agent.updated" for entry in frames):
            return [entry for entry in frames if entry["frame"]["type"] == "agent.updated"]
    raise AssertionError("the worker's publisher sent no agent delta")

"""R13: fan-out crosses processes. Two gateways, one mutation, identical frames.

This is the criterion `inprocess` cannot satisfy, and the reason P4 chose
**`redis`** for the acceptance run: an in-process bus reaches the sockets of
the process that owns it and no others, so a test of it would prove only that
one process can talk to itself.

**Two real servers.** Both are `uvicorn` processes started from this module
against the same test database and the same Redis, with `EVENT_BUS=redis`.
Nothing is faked and nothing is shared in memory: the only path from the
mutation to the two sockets is a committed `events` row, a publisher that read
it, and a Redis channel.

**The publisher is the test's own**, for the same reason a deployment's is the
worker's: with `EVENT_BUS=redis` neither API process publishes - the one
process that must not be the publisher is the one serving requests - so
something else has to, and here that is a `RealtimePublisher` built on the
same bus. `publishes_in_api` is what the servers consult, and it says no.

**A skip here would be a hole in CI**, so the Redis gate fails rather than
skips under `AGENTWORLD_REQUIRE_INTEGRATION=1`, exactly as the package's
PostgreSQL gate does. The `api` job starts Redis for this.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from realtime_helpers import mint_ticket, redis_reachable, require_redis

from agentworld.api.ws.publisher import RealtimePublisher, publishes_in_api
from agentworld.infra.event_bus import RedisEventBus
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.settings import Settings
from helpers import Actor

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT_SECONDS = 45.0


@pytest.fixture(scope="module")
def redis_url() -> str:
    return Settings().redis_url


@pytest.fixture
async def redis_gate(redis_url: str) -> None:
    require_redis(await redis_reachable(redis_url))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


class Gateway:
    """One `uvicorn` process serving the real app on the `redis` bus."""

    def __init__(self, settings: Settings, redis_url: str) -> None:
        self.port = _free_port()
        environment = {
            **os.environ,
            "ENVIRONMENT": "test",
            "LOG_FORMAT": "json",
            "DATABASE_URL": settings.database_url,
            "EVENT_BUS": "redis",
            "REDIS_URL": redis_url,
            "API_KEY_PEPPER": settings.api_key_pepper,
        }
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agentworld.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=API_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def wait_until_ready(self) -> None:
        deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"gateway exited early:\n{self._output()}")
            try:
                if httpx.get(f"{self.base}/healthz", timeout=1.0).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise AssertionError(f"gateway did not become ready:\n{self._output()}")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
            self.process.kill()
            self.process.wait(timeout=10)

    def _output(self) -> str:
        if self.process.stdout is None:  # pragma: no cover - always a pipe
            return "<no output captured>"
        self.process.terminate()
        with self.process.stdout as stream:
            return stream.read()[-4000:]


@pytest.fixture
def gateways(settings: Settings, redis_url: str, redis_gate: None) -> Iterator[list[Gateway]]:
    running = [Gateway(settings, redis_url) for _ in range(2)]
    try:
        for gateway in running:
            gateway.wait_until_ready()
        yield running
    finally:
        for gateway in running:
            gateway.stop()


class World:
    def __init__(self, client: TestClient, actor: Actor) -> None:
        self.actor = actor
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Fanout"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()
        self.agent = client.post(
            f"/v1/projects/{self.project['id']}/agents",
            json={
                "agent_key": "fan-1",
                "display_name": "Fanned",
                "interaction_mode": "observed",
            },
            headers=actor.headers,
        ).json()

    @property
    def project_id(self) -> str:
        return str(self.project["id"])

    @property
    def headers(self) -> dict[str, str]:
        return self.actor.headers


def test_the_api_process_does_not_publish_on_the_redis_bus() -> None:
    """The division of labour R13 depends on, stated as code rather than prose.

    If an API process published on the `redis` bus, the two gateways below
    would each publish the same rows and the test could pass without the bus
    carrying anything between them.
    """
    assert publishes_in_api("inprocess") is True
    assert publishes_in_api("redis") is False


async def test_r13_two_gateway_processes_receive_identical_frames(
    client: TestClient,
    actor: Actor,
    factory: UnitOfWorkFactory,
    gateways: list[Gateway],
    redis_url: str,
) -> None:
    import websockets

    world = World(client, actor)
    tickets = [mint_ticket(client, world.project_id, world.headers) for _ in gateways]

    bus = RedisEventBus(redis_url)
    publisher = RealtimePublisher(factory, bus)
    try:
        sockets = []
        for gateway, ticket in zip(gateways, tickets, strict=True):
            url = f"ws://127.0.0.1:{gateway.port}/v1/ws/projects/{world.project_id}?ticket={ticket}"
            sockets.append(await websockets.connect(url, open_timeout=20))
        try:
            snapshots = [json.loads(await _recv(socket)) for socket in sockets]
            assert [frame["type"] for frame in snapshots] == ["snapshot", "snapshot"]
            # Everything already committed, so the mutation below is the only
            # thing either socket can be told about.
            await publisher.publish_once()

            renamed = client.patch(
                f"/v1/projects/{world.project_id}/agents/{world.agent['id']}",
                json={"display_name": "Seen By Both"},
                headers=world.headers,
            )
            assert renamed.status_code == 200, renamed.text
            published = await publisher.publish_once()
            assert published.rows >= 1

            delivered = [await _delta(socket) for socket in sockets]
        finally:
            for socket in sockets:
                await socket.close()
    finally:
        await bus.aclose()

    first, second = delivered
    assert first["type"] == "agent.updated"
    assert first["data"]["display_name"] == "Seen By Both"
    # Identical frames with identical cursors, which is the criterion. Not
    # merely "both got something": the frame is built once, by the publisher,
    # so two gateways cannot disagree about it.
    assert first == second
    assert first["cursor"] == snapshots[0]["cursor"] + 1
    assert snapshots[0]["cursor"] == snapshots[1]["cursor"]


async def test_r13_a_ticket_minted_in_one_process_is_spent_in_another(
    client: TestClient,
    actor: Actor,
    gateways: list[Gateway],
) -> None:
    """Single-use is a row, not process memory (R2 across processes).

    The ticket is minted here, spent on the first gateway, and refused by the
    second - which a per-process registry would allow, and which routing makes
    a matter of luck rather than of design.
    """
    import websockets

    world = World(client, actor)
    ticket = mint_ticket(client, world.project_id, world.headers)

    def url(gateway: Gateway) -> str:
        return f"ws://127.0.0.1:{gateway.port}/v1/ws/projects/{world.project_id}?ticket={ticket}"

    async with websockets.connect(url(gateways[0]), open_timeout=20) as first:
        assert json.loads(await _recv(first))["type"] == "snapshot"

    async with websockets.connect(url(gateways[1]), open_timeout=20) as second:
        assert json.loads(await _recv(second)) == {
            "type": "error",
            "code": "invalid_ticket",
        }


async def _recv(socket: Any, timeout: float = 20.0) -> str:
    """One frame, or a failure - never a wait without end.

    `asyncio.wait_for` rather than a parameter: `recv`'s only positional
    argument in `websockets` is `decode`, so `recv(20.0)` does not time out -
    it asks for text and blocks for ever. A frame that never arrives has to
    fail this test, not hang the job that is running it.
    """
    message: str = await asyncio.wait_for(socket.recv(), timeout=timeout)
    return message


async def _delta(socket: Any) -> dict[str, Any]:
    """The next frame that is not the raw `event` channel.

    A definition change produces no `event` frame (P10.3), so this is simply
    the next frame - but reading it through a filter keeps the assertion
    about the delta rather than about frame order on a channel this test is
    not making a claim about.
    """
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        remaining = max(deadline - time.monotonic(), 0.1)
        frame = json.loads(await _recv(socket, timeout=remaining))
        if frame["type"] != "event":
            return dict(frame)
    raise AssertionError("no delta arrived")  # pragma: no cover - timeout

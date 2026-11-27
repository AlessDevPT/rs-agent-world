"""Driving the WebSocket gateway from a synchronous test.

Three things here, and each exists because the obvious way to write it is
wrong in a way that would make a test lie.

**The pong fence.** `WebSocketTestSession.receive_json()` has no timeout: a
test that reads one frame too many does not fail, it hangs the suite. So
"everything the server has sent, and nothing else" is asserted by sending
`{"type": "ping"}` and reading until `pong`. That is sound because the
gateway sends every frame from one coroutine and drains pending bus frames
before answering a client message - so anything queued before the ping
arrives is read before the pong, and anything after it was never sent. See
`api/ws/gateway.py`.

**Publishing on the app's loop.** The in-process bus's queues belong to the
event loop the app is running on, which under `TestClient` is the portal
thread's, not pytest's. Driving the publisher from pytest's own loop would
put items on a queue nobody is awaiting. `publish(client)` goes through the
portal, so the publisher runs exactly where the gateway can see it.

**The Redis gate.** The package's own gate only covers PostgreSQL. A
Redis-dependent test that skipped silently in CI would be worse than no test
at all, so this one fails under `AGENTWORLD_REQUIRE_INTEGRATION=1` for the
same reason and with the same wording.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

#: How many pong fences a test will wait through for an expected frame when
#: the publisher is a background loop rather than something the test drives.
#: Bounded so a frame that never comes fails the test instead of hanging it.
MAX_FENCES = 200


def mint_ticket(client: TestClient, project_id: str, headers: dict[str, str]) -> str:
    response = client.post(f"/v1/projects/{project_id}/ws-ticket", headers=headers)
    assert response.status_code == 201, response.text
    ticket: str = response.json()["ticket"]
    return ticket


def stream_url(project_id: str, ticket: str, *, cursor: int | None = None) -> str:
    url = f"/v1/ws/projects/{project_id}?ticket={ticket}"
    return url if cursor is None else f"{url}&cursor={cursor}"


def connect(
    client: TestClient, project_id: str, headers: dict[str, str], *, cursor: int | None = None
) -> Any:
    """Mint a ticket and open the stream. Returns the session context manager."""
    ticket = mint_ticket(client, project_id, headers)
    return client.websocket_connect(stream_url(project_id, ticket, cursor=cursor))


def publish(client: TestClient) -> int:
    """Run one publication pass on the app's own loop. Returns rows published."""
    publisher = client.app.state.realtime_publisher  # type: ignore[attr-defined]
    result = client.portal.call(publisher.publish_once)  # type: ignore[union-attr]
    rows: int = result.rows
    return rows


def fence(ws: WebSocketTestSession) -> list[dict[str, Any]]:
    """Everything the server has already sent, and nothing after it.

    The pong is consumed and not returned: it is the fence, not a frame the
    test is asserting about.
    """
    ws.send_json({"type": "ping"})
    frames: list[dict[str, Any]] = []
    while True:
        frame = ws.receive_json()
        if frame.get("type") == "pong":
            return frames
        frames.append(frame)


def fence_until(
    ws: WebSocketTestSession, *, types: Sequence[str], fences: int = MAX_FENCES
) -> list[dict[str, Any]]:
    """Fence repeatedly until every named type has arrived.

    For the one test that lets the API's own publisher loop deliver the frame
    rather than driving a pass itself: there is no way to know when a timer
    has run, so this waits for the outcome and fails rather than hangs.
    """
    collected: list[dict[str, Any]] = []
    wanted = set(types)
    for _ in range(fences):
        collected.extend(fence(ws))
        if wanted <= {frame.get("type") for frame in collected}:
            return collected
    raise AssertionError(
        f"waited {fences} fences for {sorted(wanted)}; "
        f"saw {sorted({f.get('type') for f in collected})}"
    )


def of_type(frames: Sequence[dict[str, Any]], frame_type: str) -> list[dict[str, Any]]:
    return [frame for frame in frames if frame.get("type") == frame_type]


async def redis_reachable(url: str) -> str | None:
    """None when Redis answers, otherwise why it did not."""
    from redis.asyncio import Redis

    client = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        await client.aclose()
    return None


def require_redis(problem: str | None) -> None:
    """Skip, or fail where a skip would be a hole in CI.

    The same rule the package's PostgreSQL gate applies, for the same reason:
    `AGENTWORLD_REQUIRE_INTEGRATION=1` means a skipped integration test is a
    failure, and R13 is the criterion that cannot be satisfied without Redis.
    """
    if problem is None:
        return
    message = (
        f"Redis is not reachable ({problem}). Run `npm run infra:up`, or "
        "`docker compose -f infra/compose/docker-compose.yml up -d --wait redis`."
    )
    if os.environ.get("AGENTWORLD_REQUIRE_INTEGRATION") == "1":
        pytest.fail(f"{message} AGENTWORLD_REQUIRE_INTEGRATION=1 forbids skipping.")
    pytest.skip(message, allow_module_level=True)

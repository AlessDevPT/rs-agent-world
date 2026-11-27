"""R9 to R12: execution deltas, `resume`, the replay window, and a slow consumer.

Split from `test_realtime_gateway.py` because these four need more than a
project and an agent: R9 needs an execution that really ran through the
orchestrator, and R11 needs a project with more events than its own replay
window. The criteria are in `04-ROADMAP-AND-MVP.md`.

**Nothing here hand-writes an event row.** R9's deltas come from
`POST /workflows/{id}/run` and `advance_once`, which is the path the
orchestrator uses, and R10's and R11's come from ordinary PATCHes. A test that
inserted rows itself would assert that the publisher can read a table.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from realtime_helpers import connect, fence, mint_ticket, of_type, publish, stream_url
from sqlalchemy import text
from test_executions_api import Scenario

from agentworld.api.v1.executions import ExecutionStepView, ExecutionView
from agentworld.infra.db import Database
from agentworld.main import create_app
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.executions import ExecutionService
from agentworld.settings import Settings
from helpers import Actor

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(client: TestClient, actor: Actor) -> Scenario:
    return Scenario(client, actor)


def rename(client: TestClient, scenario: Scenario, name: str) -> None:
    response = client.patch(
        f"/v1/projects/{scenario.project_id}/agents/{scenario.agent['id']}",
        json={"display_name": name},
        headers=scenario.actor.headers,
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------- R9 ---


async def test_r9_a_run_produces_execution_and_step_deltas_from_the_log(
    client: TestClient, scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """`execution.updated` and `step.updated`, folded rather than announced.

    Three events reach `events` here: `workflow.execution_started` from
    `start`, then `workflow.step_ready` and `workflow.step_dispatched` from
    `advance_once`. Each is folded into state by the same reducers the ingest
    path uses, and the frames are built from those rows - which is what
    "folded from the log" means, as against a service calling a publisher.
    """
    workflow_id = scenario.workflow()
    assert scenario.publish(workflow_id).status_code == 201

    with connect(client, scenario.project_id, scenario.actor.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        started = scenario.run(workflow_id)
        assert started.status_code == 202, started.text
        execution_id = started.json()["id"]
        # A second pass of the orchestrator, so the step events are produced
        # by the same `advance_once` a worker calls rather than by the route.
        await ExecutionService(factory).advance_once(
            uuid.UUID(scenario.project_id),
            uuid.UUID(execution_id),
            visible={uuid.UUID(scenario.workspace_id)},
        )
        publish(client)
        frames = fence(ws)

    executions = of_type(frames, "execution.updated")
    steps = of_type(frames, "step.updated")
    assert executions, "no execution delta"
    assert steps, "no step delta"
    assert ExecutionView.model_validate(executions[-1]["data"]).id == uuid.UUID(execution_id)
    step = ExecutionStepView.model_validate(steps[-1]["data"])
    assert step.step_key == "research"
    assert steps[-1]["data"]["execution_id"] == execution_id
    # Monotonic per project, which is the other half of the criterion.
    cursors = [frame["cursor"] for frame in frames]
    assert cursors == sorted(cursors)


async def test_r9_a_raw_event_frame_accompanies_an_execution_delta(
    client: TestClient, scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """One row, two frames, one cursor - and the channels keep them apart.

    This is why the gateway deduplicates per *row* rather than per frame: both
    frames carry the same cursor, and a connection that dropped anything at or
    below the last cursor it sent would deliver the first and swallow the
    second.
    """
    workflow_id = scenario.workflow()
    assert scenario.publish(workflow_id).status_code == 201

    with connect(client, scenario.project_id, scenario.actor.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        scenario.run(workflow_id)
        publish(client)
        frames = fence(ws)

    raw = of_type(frames, "event")
    assert raw, "no raw event frame"
    shared = {frame["cursor"] for frame in raw} & {
        frame["cursor"] for frame in of_type(frames, "step.updated")
    }
    assert shared, "a row produced a step delta and no event frame on the same cursor"


def test_r9_a_definition_event_never_reaches_the_event_channel(
    client: TestClient, scenario: Scenario
) -> None:
    """P10.3, asserted on the wire.

    Configuration is sensitive and the raw feed is readable at `viewer`, so a
    definition row reaching this channel would put `instructions`, `labels`,
    `model_overrides` and `dispatch` into the activity history of every
    project. The persisted payload is identity-only, which is the other half -
    this is the half that keeps it off the feed at all.
    """
    with connect(client, scenario.project_id, scenario.actor.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        rename(client, scenario, "Renamed")
        publish(client)
        frames = fence(ws)

    assert of_type(frames, "agent.updated")
    assert of_type(frames, "event") == []


def test_r9_a_channel_can_be_unsubscribed_and_subscribed_again(
    client: TestClient, scenario: Scenario
) -> None:
    """Channels are what let Build Mode take definitions and not the feed.

    `subscribe` is additive and `unsubscribe` removes, which is the reading
    `03-API.md` §6 forces by having both frames: a `subscribe` that replaced
    the set would make `unsubscribe` redundant, and would surprise the log
    viewer the document describes as *adding* `events`.
    """
    with connect(client, scenario.project_id, scenario.actor.headers) as ws:
        ws.receive_json()
        ws.send_json({"type": "unsubscribe", "channels": ["agents"]})
        publish(client)
        fence(ws)

        rename(client, scenario, "Invisible")
        publish(client)
        assert of_type(fence(ws), "agent.updated") == []

        ws.send_json({"type": "subscribe", "channels": ["agents"]})
        rename(client, scenario, "Visible Again")
        publish(client)
        frames = of_type(fence(ws), "agent.updated")

    assert len(frames) == 1
    assert frames[0]["data"]["display_name"] == "Visible Again"


# -------------------------------------------------------------------- R10 ---


async def test_r10_resume_replays_exactly_what_was_missed(
    client: TestClient, scenario: Scenario, owner_database: Database
) -> None:
    """Disconnect, mutate N times, resume: exactly N deltas, in order.

    N is split across a definition change and an execution change, because
    both draw on one cursor domain (P9) and a `resume` that served only one of
    them would still look right to a test that mutated only definitions.
    """
    workflow_id = scenario.workflow()
    scenario.publish(workflow_id)

    with connect(client, scenario.project_id, scenario.actor.headers) as ws:
        cursor = ws.receive_json()["cursor"]

    rename(client, scenario, "First")
    rename(client, scenario, "Second")
    execution_id = scenario.run(workflow_id).json()["id"]
    publish(client)

    expected = await _types_after(owner_database, scenario.project_id, cursor)
    # Both kinds, which is what makes this a test of one cursor domain (P9)
    # rather than of definition replay alone.
    assert any(kind.startswith("definition.") for _, kind in expected)
    assert any(kind.startswith("workflow.") for _, kind in expected)

    ticket = mint_ticket(client, scenario.project_id, scenario.actor.headers)
    with client.websocket_connect(stream_url(scenario.project_id, ticket, cursor=cursor)) as ws:
        frames = fence(ws)

    # Exactly one delta per row that produced one, in cursor order, and a
    # cursor set that is exactly the rows the disconnect missed.
    assert [frame["type"] for frame in frames if frame["type"] != "event"] == [
        "agent.updated",
        "agent.updated",
        "execution.updated",
        "step.updated",
    ]
    assert sorted({frame["cursor"] for frame in frames}) == [seq for seq, _ in expected]
    assert execution_id in str(frames)
    # No snapshot: a client that resumes has one already, and sending it a
    # second would make "exactly N deltas" cost a full state transfer.
    assert of_type(frames, "snapshot") == []


async def test_r10_a_resume_frame_replays_on_a_live_socket(
    client: TestClient, scenario: Scenario
) -> None:
    """The documented client frame, as against the connect-time parameter."""
    with connect(client, scenario.project_id, scenario.actor.headers) as ws:
        cursor = ws.receive_json()["cursor"]
        rename(client, scenario, "Moved On")
        publish(client)
        live = of_type(fence(ws), "agent.updated")

        ws.send_json({"type": "resume", "cursor": cursor})
        replayed = of_type(fence(ws), "agent.updated")

    assert len(live) == 1
    assert len(replayed) == 1
    assert live[0]["cursor"] == replayed[0]["cursor"]


async def test_r10_resume_repeats_nothing_already_delivered(
    client: TestClient, scenario: Scenario
) -> None:
    """A cursor at the head replays nothing, which is the "no repeat" half."""
    rename(client, scenario, "Settled")
    publish(client)

    with connect(client, scenario.project_id, scenario.actor.headers) as ws:
        cursor = ws.receive_json()["cursor"]
    ticket = mint_ticket(client, scenario.project_id, scenario.actor.headers)
    with client.websocket_connect(stream_url(scenario.project_id, ticket, cursor=cursor)) as ws:
        assert fence(ws) == []

        # And the connection is now *at* the head rather than at zero. A
        # resume that replays nothing must still move its position: if it did
        # not, the next live frame would look like a gap of its whole
        # sequence, and the gateway would "close" that gap by replaying the
        # project's entire history to a client that asked for none of it.
        rename(client, scenario, "Next")
        publish(client)
        live = of_type(fence(ws), "agent.updated")

    assert len(live) == 1
    assert live[0]["cursor"] == cursor + 1


async def _types_after(
    owner_database: Database, project_id: str, cursor: int
) -> list[tuple[int, str]]:
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT seq, type FROM events WHERE project_id = :p AND seq > :c ORDER BY seq"
                ),
                {"p": uuid.UUID(project_id), "c": cursor},
            )
        ).all()
    return [(int(row[0]), str(row[1])) for row in rows]


# -------------------------------------------------------------------- R11 ---


def _windowed_app(settings: Settings, auth: Any, window: int) -> Any:
    """An app whose replay window is the setting's floor of 100 events.

    The floor is not relaxed for the test. `WS_REPLAY_WINDOW_EVENTS` has
    `ge=100` because a window small enough to be convenient here would be a
    deployment that re-snapshotted every client on every burst, and loosening
    a production bound to make a test shorter is how the bound stops meaning
    anything. So the test writes 101 events instead.
    """
    from agentworld.api.deps import get_auth_provider

    app = create_app(settings.model_copy(update={"ws_replay_window_events": window}))
    app.dependency_overrides[get_auth_provider] = lambda: auth
    return app


def test_r11_a_cursor_beyond_the_window_gets_resync_required_and_nothing_else(
    settings: Settings, auth: Any, actor: Actor
) -> None:
    window = 100
    with TestClient(_windowed_app(settings, auth, window)) as client:
        scenario = Scenario(client, actor)
        with connect(client, scenario.project_id, scenario.actor.headers) as ws:
            cursor = ws.receive_json()["cursor"]

        for index in range(window + 1):
            rename(client, scenario, f"Edit {index}")
        publish(client)

        ticket = mint_ticket(client, scenario.project_id, scenario.actor.headers)
        with client.websocket_connect(stream_url(scenario.project_id, ticket, cursor=cursor)) as ws:
            first = ws.receive_json()
            assert first == {"type": "resync_required", "reason": "cursor_too_old"}
            # "And nothing else": the socket is closed, so the client's only
            # next move is a fresh snapshot.
            assert ws.receive()["type"] == "websocket.close"


def test_r11_a_cursor_just_inside_the_window_is_served(
    settings: Settings, auth: Any, actor: Actor
) -> None:
    """The boundary from the other side, so the refusal is the window and not
    the mere presence of a cursor."""
    window = 100
    with TestClient(_windowed_app(settings, auth, window)) as client:
        scenario = Scenario(client, actor)
        with connect(client, scenario.project_id, scenario.actor.headers) as ws:
            cursor = ws.receive_json()["cursor"]

        for index in range(window):
            rename(client, scenario, f"Edit {index}")
        publish(client)

        ticket = mint_ticket(client, scenario.project_id, scenario.actor.headers)
        with client.websocket_connect(stream_url(scenario.project_id, ticket, cursor=cursor)) as ws:
            frames = fence(ws)

    assert of_type(frames, "resync_required") == []
    assert len(of_type(frames, "agent.updated")) == window


async def test_r11_a_cursor_whose_rows_were_pruned_gets_resync_required(
    client: TestClient, scenario: Scenario, owner_database: Database
) -> None:
    """The other half of the window: the rows, not the count.

    `events` is partitioned monthly and nothing prunes a partition today, so
    this drops the rows by hand - which is what retention will do when it
    arrives. Asserted now so that arriving is a configuration change rather
    than a protocol one.
    """
    rename(client, scenario, "Before The Prune")
    publish(client)
    with connect(client, scenario.project_id, scenario.actor.headers) as ws:
        cursor = ws.receive_json()["cursor"]
    rename(client, scenario, "After The Prune")
    publish(client)

    async with owner_database.session() as session:
        await session.execute(
            text("DELETE FROM events WHERE project_id = :p AND seq <= :c"),
            {"p": uuid.UUID(scenario.project_id), "c": cursor},
        )

    ticket = mint_ticket(client, scenario.project_id, scenario.actor.headers)
    with client.websocket_connect(stream_url(scenario.project_id, ticket, cursor=cursor - 1)) as ws:
        assert ws.receive_json() == {"type": "resync_required", "reason": "cursor_too_old"}


def test_r11_a_cursor_ahead_of_the_project_gets_resync_required(
    client: TestClient, scenario: Scenario
) -> None:
    """A client claiming a future position is not old; it is wrong. Same answer."""
    ticket = mint_ticket(client, scenario.project_id, scenario.actor.headers)
    with client.websocket_connect(stream_url(scenario.project_id, ticket, cursor=10_000_000)) as ws:
        assert ws.receive_json() == {"type": "resync_required", "reason": "cursor_too_old"}


# -------------------------------------------------------------------- R12 ---


def test_r12_a_consumer_that_cannot_keep_up_is_dropped_with_resync_required(
    settings: Settings, auth: Any, actor: Actor
) -> None:
    """The bound is reached, the socket is closed, and memory stays bounded.

    Provoked deterministically rather than by racing a reader. The queue bound
    is one frame, and one publication pass hands the bus several messages
    with no await between them - `InProcessEventBus.publish` only ever calls
    `put_nowait` - so the gateway's coroutine cannot be scheduled in the
    middle. The subscription therefore really does overflow, which is the
    condition R12 is about and which a test that merely stopped calling
    `receive_json()` could never reach: the test transport's own send buffer
    is unbounded, so the gateway would drain the queue as fast as it filled.
    """
    from agentworld.api.deps import get_auth_provider

    app = create_app(settings.model_copy(update={"ws_outbound_queue_frames": 1}))
    app.dependency_overrides[get_auth_provider] = lambda: auth
    with TestClient(app) as client:
        scenario = Scenario(client, actor)
        with connect(client, scenario.project_id, scenario.actor.headers) as ws:
            ws.receive_json()
            publish(client)
            fence(ws)

            for index in range(6):
                rename(client, scenario, f"Burst {index}")
            publish(client)

            assert ws.receive_json() == {
                "type": "resync_required",
                "reason": "consumer_too_slow",
            }
            assert ws.receive()["type"] == "websocket.close"


def test_r12_the_subscription_never_holds_more_than_its_bound() -> None:
    """Server memory stays bounded while it happens, asserted on the queue.

    The gateway's half of R12 is above; this is the bus's half, and it is the
    one that bounds memory. A subscription offered many more messages than it
    can hold keeps exactly its bound and records that it dropped the rest -
    it does not block the publisher, because one slow socket must not stall a
    project's whole fan-out.
    """
    import asyncio

    from agentworld.infra.event_bus import InProcessEventBus, channel_for

    async def run() -> tuple[int, bool]:
        bus = InProcessEventBus()
        channel = channel_for(uuid.uuid4())
        async with bus.subscribe(channel, maxsize=4) as subscription:
            for index in range(1_000):
                await bus.publish(channel, f"frame-{index}")
            return subscription.qsize(), subscription.overflowed

    size, overflowed = asyncio.run(run())
    assert size == 4
    assert overflowed is True

"""The realtime spine's acceptance criteria: R1-R12 and R14.

One test or group per criterion in `04-ROADMAP-AND-MVP.md`, named so the
ledger can be read against this file. R13 - fan-out across two processes on
the `redis` bus - needs two real servers and lives in
`test_realtime_fanout.py`; R3's tenant half is asserted inside
`test_tenant_isolation.py`'s own sweep, where a new route cannot miss it; R4's
role-matrix row is in `test_role_matrix.py`, which fails on an action with no
row.

**Publication is driven, not awaited.** `realtime_publish_in_api` is off for
this package (see `conftest.py`), so each test runs the publisher pass itself
through the app's portal and can then say what *that* pass produced. One test
- `test_the_api_process_publishes_for_itself_on_the_inprocess_bus` - builds an
app with the loop on instead, so the wiring that a deployment depends on is
covered by something other than inspection.

**"And nothing else" is a pong, not a timeout.** `receive_json()` never times
out, so a test that read one frame too many would hang the suite rather than
fail. `realtime_helpers.fence` sends `ping` and reads until `pong`, which is
exact because the gateway sends every frame from one coroutine and drains
pending bus frames before answering a client message.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from realtime_helpers import (
    connect,
    fence,
    fence_until,
    mint_ticket,
    of_type,
    publish,
    stream_url,
)
from sqlalchemy import text

from agentworld.api.v1.agents import AgentView
from agentworld.api.v1.workflows import ConnectionView, WorkflowView
from agentworld.domain.protocol import PROTOCOL_VERSION
from agentworld.infra.db import Database
from agentworld.main import create_app
from agentworld.settings import Settings
from helpers import Actor

pytestmark = pytest.mark.integration


class World:
    """An owner, a workspace, a project and one agent."""

    def __init__(self, client: TestClient, actor: Actor, label: str = "Realtime") -> None:
        self.actor = actor
        # The label is in the name because workspace slugs are unique across
        # the deployment: a test that builds two of these needs them to differ.
        self.workspace = client.post(
            "/v1/workspaces", json={"name": label}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()
        created = client.post(
            f"/v1/projects/{self.project['id']}/agents",
            json={"agent_key": "etl-1", "display_name": "ETL", "interaction_mode": "observed"},
            headers=actor.headers,
        )
        assert created.status_code == 201, created.text
        self.agent = created.json()

    @property
    def workspace_id(self) -> str:
        return str(self.workspace["id"])

    @property
    def project_id(self) -> str:
        return str(self.project["id"])

    @property
    def agent_id(self) -> str:
        return str(self.agent["id"])

    @property
    def headers(self) -> dict[str, str]:
        return self.actor.headers


@pytest.fixture
def world(client: TestClient, actor: Actor) -> World:
    return World(client, actor)


def rename(client: TestClient, world: World, name: str) -> dict[str, Any]:
    response = client.patch(
        f"/v1/projects/{world.project_id}/agents/{world.agent_id}",
        json={"display_name": name},
        headers=world.headers,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


# --------------------------------------------------------------------- R1 ---


def test_r1_a_connection_with_no_ticket_is_refused(client: TestClient, world: World) -> None:
    with client.websocket_connect(f"/v1/ws/projects/{world.project_id}") as ws:
        assert ws.receive_json() == {"type": "error", "code": "invalid_ticket"}


@pytest.mark.parametrize(
    "ticket",
    [
        "",
        "nonsense",
        "not.a.ticket",
        f"{uuid.uuid4().hex}.{uuid.uuid4().hex}.secret",
        f"{uuid.uuid4()}.{uuid.uuid4()}.",
    ],
    ids=["empty", "one-part", "unparseable-ids", "unknown-ids", "no-secret"],
)
def test_r1_a_malformed_ticket_is_refused(client: TestClient, world: World, ticket: str) -> None:
    with client.websocket_connect(stream_url(world.project_id, ticket)) as ws:
        assert ws.receive_json() == {"type": "error", "code": "invalid_ticket"}


def test_r1_a_ticket_for_another_project_is_refused(
    client: TestClient, world: World, make_actor: Any
) -> None:
    """Same workspace, different project: the refusal is the scope, not the role.

    Minted legitimately and spent on the wrong stream, which is the case a
    gateway that only checked the signature would let through.
    """
    other = client.post(
        f"/v1/workspaces/{world.workspace_id}/projects",
        json={"name": "Elsewhere"},
        headers=world.headers,
    ).json()
    ticket = mint_ticket(client, str(other["id"]), world.headers)

    with client.websocket_connect(stream_url(world.project_id, ticket)) as ws:
        assert ws.receive_json() == {"type": "error", "code": "invalid_ticket"}


def test_r1_no_frame_of_state_precedes_the_refusal(client: TestClient, world: World) -> None:
    """The refusal is the *first* frame, and the socket then closes.

    "Refused before a single frame is sent" is the criterion; a gateway that
    sent a snapshot and then noticed would have already leaked the project's
    contents.
    """
    with client.websocket_connect(stream_url(world.project_id, "nonsense")) as ws:
        first = ws.receive_json()
        assert first["type"] == "error"
        # Closed, not merely quiet. `receive()` reports the close rather than
        # blocking, which is what lets this be asserted without a timeout.
        assert ws.receive()["type"] == "websocket.close"


# --------------------------------------------------------------------- R2 ---


def test_r2_a_ticket_is_single_use(client: TestClient, world: World) -> None:
    ticket = mint_ticket(client, world.project_id, world.headers)

    with client.websocket_connect(stream_url(world.project_id, ticket)) as first:
        assert first.receive_json()["type"] == "snapshot"

    with client.websocket_connect(stream_url(world.project_id, ticket)) as second:
        assert second.receive_json() == {"type": "error", "code": "invalid_ticket"}


async def test_r2_an_expired_ticket_is_refused(
    client: TestClient, world: World, owner_database: Database
) -> None:
    """Expiry is the row's, not the process's clock.

    Aged by moving `expires_at` into the past rather than by sleeping: the TTL
    is sixty seconds and a test that waited it out would be a minute long.
    """
    ticket = mint_ticket(client, world.project_id, world.headers)
    async with owner_database.session() as session:
        await session.execute(text("UPDATE ws_tickets SET expires_at = now() - interval '1s'"))

    with client.websocket_connect(stream_url(world.project_id, ticket)) as ws:
        assert ws.receive_json() == {"type": "error", "code": "invalid_ticket"}


async def test_r2_an_expired_ticket_is_not_marked_consumed(
    client: TestClient, world: World, owner_database: Database
) -> None:
    """A ticket that was never usable stays unspent, so "spent" means spent."""
    ticket = mint_ticket(client, world.project_id, world.headers)
    async with owner_database.session() as session:
        await session.execute(text("UPDATE ws_tickets SET expires_at = now() - interval '1s'"))

    with client.websocket_connect(stream_url(world.project_id, ticket)) as ws:
        assert ws.receive_json()["type"] == "error"

    async with owner_database.session() as session:
        consumed = (await session.execute(text("SELECT consumed_at FROM ws_tickets"))).scalar_one()
    assert consumed is None


async def test_r2_only_the_digest_of_a_ticket_is_stored(
    client: TestClient, world: World, owner_database: Database
) -> None:
    ticket = mint_ticket(client, world.project_id, world.headers)
    secret = ticket.split(".")[2]

    async with owner_database.session() as session:
        stored = (await session.execute(text("SELECT secret_digest FROM ws_tickets"))).scalar_one()
    assert secret not in stored
    assert len(stored) == 64


def test_r2_a_real_id_with_the_wrong_secret_is_refused(client: TestClient, world: World) -> None:
    """And burns the ticket: presenting a real id is an attack on that ticket."""
    ticket = mint_ticket(client, world.project_id, world.headers)
    workspace, ticket_id, _ = ticket.split(".")

    with client.websocket_connect(
        stream_url(world.project_id, f"{workspace}.{ticket_id}.wrong")
    ) as ws:
        assert ws.receive_json() == {"type": "error", "code": "invalid_ticket"}

    with client.websocket_connect(stream_url(world.project_id, ticket)) as ws:
        assert ws.receive_json() == {"type": "error", "code": "invalid_ticket"}


# --------------------------------------------------------------------- R3 ---


def test_r3_a_ticket_never_yields_another_projects_frames(
    client: TestClient, world: World, make_actor: Any
) -> None:
    """Two tenants, a snapshot each, and no id from one in the other.

    The tenant-isolation sweep covers the route's 404; this covers the
    *stream*, which that sweep cannot reach.
    """
    intruder = make_actor("bob@example.com")
    theirs = World(client, intruder, label="Theirs")

    with connect(client, world.project_id, world.headers) as ws:
        mine = ws.receive_json()
    with connect(client, theirs.project_id, theirs.headers) as ws:
        other = ws.receive_json()

    assert [a["id"] for a in mine["data"]["agents"]] == [world.agent_id]
    assert [a["id"] for a in other["data"]["agents"]] == [theirs.agent_id]
    assert theirs.agent_id not in str(mine)
    assert world.agent_id not in str(other)


def test_r3_a_non_member_cannot_mint_a_ticket(
    client: TestClient, world: World, make_actor: Any
) -> None:
    stranger = make_actor("eve@example.com")
    response = client.post(f"/v1/projects/{world.project_id}/ws-ticket", headers=stranger.headers)
    assert response.status_code == 404


# --------------------------------------------------------------------- R5 ---


def test_r5_the_first_frame_is_a_snapshot_with_a_cursor(client: TestClient, world: World) -> None:
    with connect(client, world.project_id, world.headers) as ws:
        frame = ws.receive_json()

    assert frame["type"] == "snapshot"
    assert isinstance(frame["cursor"], int)
    assert set(frame["data"]) == {
        "agents",
        "workflows",
        "connections",
        "integrations",
        "executions",
        "approvals",
        "world",
    }
    assert set(frame["data"]["world"]) == {"layout", "placements"}
    assert [AgentView.model_validate(a) for a in frame["data"]["agents"]]


def test_r5_the_snapshot_cursor_is_the_projects_latest_seq(
    client: TestClient, world: World
) -> None:
    """Creating the agent appended a definition event, so the cursor is not 0.

    Asserted relatively: `AgentService.create` is a P13 producer, so the
    number moves whenever another producer is wired, and a hard-coded one
    would have to be re-baselined instead of failing honestly.
    """
    with connect(client, world.project_id, world.headers) as ws:
        before = ws.receive_json()["cursor"]
    rename(client, world, "Renamed")
    with connect(client, world.project_id, world.headers) as ws:
        after = ws.receive_json()["cursor"]

    assert before >= 1
    assert after == before + 1


# --------------------------------------------------------------------- R6 ---


async def test_r6_a_snapshot_transaction_cannot_see_a_mutation_made_during_it(
    client: TestClient, world: World, factory: Any
) -> None:
    """The isolation property the handshake rests on, asserted directly.

    Two snapshots inside one `REPEATABLE READ` transaction, with a committed
    rename between them, must agree - in the cursor *and* in the entity. Under
    `READ COMMITTED` the second would carry the new name on the old cursor,
    which is exactly how an entity comes to appear in a snapshot and in a
    delta both.
    """
    from agentworld.api.ws.frames import FrameBuilder
    from agentworld.repositories.base import TenantScope

    scope = TenantScope(uuid.UUID(world.workspace_id), uuid.UUID(world.project_id))
    async with factory.begin(
        visible_workspaces={scope.workspace_id}, isolation_level="REPEATABLE READ"
    ) as work:
        builder = FrameBuilder(work, scope)
        first = await builder.snapshot()
        rename(client, world, "Changed Underneath")
        second = await builder.snapshot()

    assert first.cursor == second.cursor
    assert first.data["agents"] == second.data["agents"]
    assert "Changed Underneath" not in str(first.data)


def test_r6_an_entity_mutated_before_the_handshake_is_in_the_snapshot_only(
    client: TestClient, world: World
) -> None:
    rename(client, world, "Before")

    with connect(client, world.project_id, world.headers) as ws:
        snapshot = ws.receive_json()
        published = publish(client)
        after = fence(ws)

    names = [a["display_name"] for a in snapshot["data"]["agents"]]
    assert names == ["Before"]
    # The row is published - the publisher had never run - but the connection
    # has already sent that cursor in the snapshot, so it emits nothing.
    assert published >= 1
    assert after == []


def test_r6_an_entity_mutated_after_the_handshake_arrives_exactly_once(
    client: TestClient, world: World
) -> None:
    with connect(client, world.project_id, world.headers) as ws:
        snapshot = ws.receive_json()
        publish(client)
        assert fence(ws) == []

        rename(client, world, "After")
        publish(client)
        frames = fence(ws)

    assert [a["display_name"] for a in snapshot["data"]["agents"]] == ["ETL"]
    updates = of_type(frames, "agent.updated")
    assert len(updates) == 1
    assert updates[0]["data"]["display_name"] == "After"
    assert updates[0]["cursor"] == snapshot["cursor"] + 1


async def test_r6_a_duplicate_publication_produces_no_duplicate_frame(
    client: TestClient, world: World, owner_database: Database
) -> None:
    """At-least-once publication, asserted rather than assumed (P10, R10).

    The position is rewound so the same rows are published a second time,
    which is what a crash between the send and the position write produces.
    """
    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        rename(client, world, "Once")
        publish(client)
        first = of_type(fence(ws), "agent.updated")

        await _rewind_publication(owner_database, world.project_id)
        republished = publish(client)
        second = of_type(fence(ws), "agent.updated")

    assert len(first) == 1
    assert republished >= 1
    assert second == []


# --------------------------------------------------------------------- R7 ---


def test_r7_a_definition_delta_validates_against_the_rest_view(
    client: TestClient, world: World
) -> None:
    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        rename(client, world, "Whole")
        publish(client)
        frames = of_type(fence(ws), "agent.updated")

    assert len(frames) == 1
    view = AgentView.model_validate(frames[0]["data"])
    rest = client.get(
        f"/v1/projects/{world.project_id}/agents/{world.agent_id}", headers=world.headers
    ).json()
    assert view.id == uuid.UUID(world.agent_id)
    assert view.display_name == rest["display_name"]
    assert set(frames[0]["data"]) == set(rest)


def test_r7_a_workflow_delta_validates_against_the_rest_view(
    client: TestClient, world: World
) -> None:
    created = client.post(
        f"/v1/projects/{world.project_id}/workflows",
        json={"name": "Nightly"},
        headers=world.headers,
    )
    assert created.status_code == 201, created.text

    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        patched = client.patch(
            f"/v1/projects/{world.project_id}/workflows/{created.json()['workflow']['id']}",
            json={"name": "Nightly ETL"},
            headers=world.headers,
        )
        assert patched.status_code == 200, patched.text
        publish(client)
        frames = of_type(fence(ws), "workflow.updated")

    assert len(frames) == 1
    assert WorkflowView.model_validate(frames[0]["data"]).name == "Nightly ETL"


def test_r7_a_connection_delta_validates_against_the_rest_view(
    client: TestClient, world: World
) -> None:
    second = client.post(
        f"/v1/projects/{world.project_id}/agents",
        json={"agent_key": "etl-2", "display_name": "Second", "interaction_mode": "observed"},
        headers=world.headers,
    ).json()
    created = client.post(
        f"/v1/projects/{world.project_id}/connections",
        json={
            "kind": "delegate",
            "source_kind": "agent",
            "source_id": world.agent_id,
            "target_kind": "agent",
            "target_id": second["id"],
        },
        headers=world.headers,
    )
    assert created.status_code == 201, created.text

    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        # The update first, because R7 is about *every* delta payload and the
        # creation above happened before the socket opened - its frame was
        # deduped away by the snapshot's cursor, so without this the
        # whole-entity half of this criterion would rest on nothing.
        patched = client.patch(
            f"/v1/projects/{world.project_id}/connections/{created.json()['id']}",
            json={"enabled": False},
            headers=world.headers,
        )
        assert patched.status_code == 200, patched.text
        publish(client)
        updates = of_type(fence(ws), "connection.updated")
        # Read before the delete below: the listing is empty afterwards, and
        # its shape is what this half of R7 compares the payload against.
        listed = client.get(
            f"/v1/projects/{world.project_id}/connections", headers=world.headers
        ).json()

        client.delete(
            f"/v1/projects/{world.project_id}/connections/{created.json()['id']}",
            headers=world.headers,
        )
        publish(client)
        frames = fence(ws)

    assert len(updates) == 1
    view = ConnectionView.model_validate(updates[0]["data"])
    assert view.id == uuid.UUID(created.json()["id"])
    assert view.enabled is False
    assert set(updates[0]["data"]) == set(listed[0])

    tombstones = of_type(frames, "connection.deleted")
    assert len(tombstones) == 1
    # R7's one exception, and it is explicit: project, entity type, entity id
    # and the cursor, and nothing else. Not an entity with its fields blanked.
    assert tombstones[0]["data"] == {
        "project_id": world.project_id,
        "entity": "connection",
        "id": created.json()["id"],
    }
    assert of_type(frames, "connection.updated") == []


def test_r7_an_archive_is_an_update_and_not_a_tombstone(client: TestClient, world: World) -> None:
    """P11.4: an archived agent stays addressable, so an archive is an update."""
    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        archived = client.delete(
            f"/v1/projects/{world.project_id}/agents/{world.agent_id}",
            headers=world.headers,
        )
        assert archived.status_code == 204, archived.text
        publish(client)
        frames = fence(ws)

    updates = of_type(frames, "agent.updated")
    assert len(updates) == 1
    assert updates[0]["data"]["archived_at"] is not None
    assert [f for f in frames if f["type"].endswith(".deleted")] == []


# --------------------------------------------------------------------- R8 ---


async def test_r8_a_patch_on_an_agent_produces_agent_updated_with_a_shared_cursor(
    client: TestClient, world: World, owner_database: Database
) -> None:
    """R8's own example, end to end: the PATCH, the row, the frame, the cursor.

    The cursor on the frame is the `events.seq` the mutation's own transaction
    allocated - the shared per-project sequence of P9, not a number the
    gateway made up - which is asserted by reading the row back.
    """
    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        rename(client, world, "Durable")
        publish(client)
        frames = of_type(fence(ws), "agent.updated")

    assert len(frames) == 1
    recorded = await _last_definition_row(owner_database, world.project_id)
    assert frames[0]["cursor"] == recorded["seq"]
    assert recorded["type"] == "definition.agent.updated"
    assert recorded["payload"] == {"entity": "agent", "id": world.agent_id}


def test_r8_nothing_is_published_for_a_mutation_that_rolled_back(
    client: TestClient, world: World
) -> None:
    """The durability half, from the other side.

    A refused PATCH appends no row, so there is nothing to publish and no
    delta - which is the property a fire-and-forget publish inside the
    mutation would break by announcing a change that never happened.
    """
    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        # `restore` on a live agent is the case P13 documents: it has no
        # prior read to refuse against, so it reserves the cursor first and
        # lets the not-found roll the whole transaction back - cursor
        # allocation included. A publish that happened inside the mutation
        # would already have announced a restore that never occurred.
        refused = client.post(
            f"/v1/projects/{world.project_id}/agents/{world.agent_id}/restore",
            headers=world.headers,
        )
        assert refused.status_code == 404, refused.text
        assert publish(client) == 0
        assert fence(ws) == []


async def test_r8_the_publisher_reads_committed_rows_rather_than_being_told(
    client: TestClient, world: World, owner_database: Database
) -> None:
    """Publication is after the fact, so a mutation made with no socket open
    is still delivered to one that opens later and resumes."""
    before = await _latest_seq(owner_database, world.project_id)
    rename(client, world, "While Nobody Watched")
    assert publish(client) >= 1

    with client.websocket_connect(
        stream_url(
            world.project_id,
            mint_ticket(client, world.project_id, world.headers),
            cursor=before,
        )
    ) as ws:
        frames = fence(ws)

    assert [f["type"] for f in of_type(frames, "agent.updated")] == ["agent.updated"]


# --------------------------------------------------------------------- R14 ---


def test_r14_with_no_activity_the_server_sends_nothing(client: TestClient, world: World) -> None:
    """No keepalive, no status, no synthetic delta. Three fences of silence."""
    with connect(client, world.project_id, world.headers) as ws:
        assert ws.receive_json()["type"] == "snapshot"
        for _ in range(3):
            assert fence(ws) == []


async def test_r14_every_delta_traces_to_a_committed_event_row(
    client: TestClient, world: World, owner_database: Database
) -> None:
    """Three mutations, three cursors, and every cursor is an `events` row."""
    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        for name in ("One", "Two", "Three"):
            rename(client, world, name)
        publish(client)
        frames = fence(ws)

    cursors = sorted({f["cursor"] for f in frames})
    rows = await _seqs_of(owner_database, world.project_id)
    assert cursors
    assert set(cursors) <= set(rows)


# ----------------------------------------------------- the wiring, once ---


def test_the_api_process_publishes_for_itself_on_the_inprocess_bus(
    settings: Settings, auth: Any, world_of: Any
) -> None:
    """The lifespan's publisher loop, which every other test turns off.

    `inprocess` cannot cross processes, so the API has to publish for itself
    or a local run shows no deltas at all - the one deployment fact that
    cannot be asserted by driving `publish_once` by hand.
    """
    from agentworld.api.deps import get_auth_provider

    app = create_app(
        settings.model_copy(
            update={"realtime_publish_in_api": True, "realtime_publish_interval_seconds": 0.02}
        )
    )
    app.dependency_overrides[get_auth_provider] = lambda: auth
    with TestClient(app) as own_client:
        world = world_of(own_client)
        with connect(own_client, world.project_id, world.headers) as ws:
            ws.receive_json()
            rename(own_client, world, "Published By The Api")
            frames = fence_until(ws, types=["agent.updated"])

    updates = of_type(frames, "agent.updated")
    assert updates[-1]["data"]["display_name"] == "Published By The Api"


@pytest.fixture
def world_of(actor: Actor) -> Any:
    def build(own_client: TestClient) -> World:
        return World(own_client, actor)

    return build


# ---------------------------------------------- what a bad client gets ---


@pytest.mark.parametrize(
    "raw",
    ["not json at all", "[1, 2]", '{"type": "invented"}', '{"type": "resume"}'],
    ids=["unparseable", "not-an-object", "unknown-type", "resume-with-no-cursor"],
)
def test_a_malformed_client_frame_is_answered_and_the_socket_stays_open(
    client: TestClient, world: World, raw: str
) -> None:
    """A client bug is not a reason to drop a stream.

    `[1, 2]` is the case worth parametrising: it is well-formed JSON with no
    `type` to dispatch on, so a handler that only caught a parse error would
    raise on it - and on a socket there is no error handler above the route to
    turn that into a response.
    """
    with connect(client, world.project_id, world.headers) as ws:
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_text(raw)
        answer = ws.receive_json()
        assert answer["type"] == "error"
        assert answer["code"] in ("malformed_frame", "unknown_frame")
        # Still usable: the fence round-trips afterwards.
        assert fence(ws) == []


# ------------------------------------- the two seams nothing else pins ---


def test_the_handshake_opens_its_snapshot_at_repeatable_read(
    client: TestClient, world: World
) -> None:
    """R6's mechanism, pinned where the gateway chooses it.

    `test_r6_a_snapshot_transaction_cannot_see_a_mutation_made_during_it`
    asserts that `REPEATABLE READ` has the property the handshake needs - but
    it passes the level itself, so dropping the argument from the gateway
    would leave it green. This watches the level the *handshake* asks for, so
    the one line connecting the property to the protocol cannot be deleted
    silently.
    """
    database: Database = client.app.state.database  # type: ignore[attr-defined]
    original = database.session
    levels: list[str | None] = []

    def recording(**kwargs: Any) -> Any:
        levels.append(kwargs.get("isolation_level"))
        return original(**kwargs)

    database.session = recording  # type: ignore[method-assign]
    try:
        with connect(client, world.project_id, world.headers) as ws:
            assert ws.receive_json()["type"] == "snapshot"
    finally:
        database.session = original  # type: ignore[method-assign]

    assert "REPEATABLE READ" in levels, (
        "the snapshot ran at the default isolation level, so a mutation "
        f"committed during it could be reported twice; saw {levels}"
    )


async def test_an_ingested_event_produces_an_agent_delta_and_a_raw_event(
    client: TestClient, world: World
) -> None:
    """R9 through the ingest plane, which is the wording that box uses.

    The execution half of R9 is in `test_realtime_resume.py` and goes through
    `run` and `advance_once`. This is the other producer the frame builder
    has a branch for, and the one the island's "status equal to the REST view
    and arriving as a delta" box depends on: a heartbeat folds into `agents`,
    so the row yields `agent.updated` as well as the raw `event`.
    """
    key = client.post(
        f"/v1/workspaces/{world.workspace_id}/api-keys",
        json={"name": "Ingest", "scope": "ingest", "project_id": world.project_id},
        headers=world.headers,
    )
    assert key.status_code == 201, key.text

    with connect(client, world.project_id, world.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        accepted = client.post(
            "/v1/ingest/events",
            json={
                "events": [
                    {
                        "dedup_key": f"hb-{uuid.uuid4()}",
                        "type": "agent.heartbeat",
                        "protocol_version": PROTOCOL_VERSION,
                        "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
                        "subject": {"kind": "agent", "ref": "etl-1"},
                    }
                ]
            },
            headers={"X-AgentWorld-Key": key.json()["key"]},
        )
        assert accepted.status_code in (200, 202), accepted.text
        publish(client)
        frames = fence(ws)

    raw = of_type(frames, "event")
    deltas = of_type(frames, "agent.updated")
    assert len(raw) == 1, "an ingested event belongs on the activity feed"
    assert len(deltas) == 1, "a folded heartbeat is an agent delta"
    assert raw[0]["cursor"] == deltas[0]["cursor"], "one row, one cursor, two frames"
    rest = client.get(
        f"/v1/projects/{world.project_id}/agents/{world.agent_id}",
        headers=world.headers,
    ).json()
    # The island's box, one layer down: the status on the delta is the status
    # the REST view reports, because both read the row the reducer wrote.
    assert deltas[0]["data"]["status"] == rest["status"]
    assert deltas[0]["data"]["last_heartbeat_at"] is not None


# ------------------------------------------------------------- read helpers ---
#
# All four read as the schema owner. The application role is under forced
# row-level security with no visibility set on a raw session, so the same
# queries through `app.state.database` return nothing - which looks exactly
# like "the publisher wrote nothing" and would make these assertions pass for
# the wrong reason.


async def _latest_seq(owner_database: Database, project_id: str) -> int:
    async with owner_database.session() as session:
        found = (
            await session.execute(
                text("SELECT next_seq - 1 FROM project_event_cursors WHERE project_id = :p"),
                {"p": uuid.UUID(project_id)},
            )
        ).scalar_one_or_none()
    return int(found or 0)


async def _seqs_of(owner_database: Database, project_id: str) -> list[int]:
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text("SELECT seq FROM events WHERE project_id = :p ORDER BY seq"),
                {"p": uuid.UUID(project_id)},
            )
        ).all()
    return [int(row[0]) for row in rows]


async def _last_definition_row(owner_database: Database, project_id: str) -> dict[str, Any]:
    async with owner_database.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT seq, type, payload FROM events "
                    "WHERE project_id = :p AND type LIKE 'definition.%' "
                    "ORDER BY seq DESC LIMIT 1"
                ),
                {"p": uuid.UUID(project_id)},
            )
        ).one()
    return {"seq": int(row[0]), "type": row[1], "payload": row[2]}


async def _rewind_publication(owner_database: Database, project_id: str) -> None:
    """Undo the publisher's recorded position, as a crash would."""
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE event_publication_cursors SET published_seq = 0 WHERE project_id = :p"),
            {"p": uuid.UUID(project_id)},
        )

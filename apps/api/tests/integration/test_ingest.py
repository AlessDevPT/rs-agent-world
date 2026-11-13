"""Ingest, end to end over HTTP, and the promises it has to keep.

The acceptance criteria from the roadmap live here: the same batch ingested
three times yields one event per `dedup_key`; re-folding the stored log
reproduces the state exactly; and folding creates no dispatch rows at all.

The replay test is the one worth reading. It rebuilds envelopes from the
`events` rows rather than reusing the objects that were posted, so it asserts
something stronger than "fold is deterministic": that the stored row carries
everything a replay needs. A column we forgot to persist would pass a
determinism test and fail this one.
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain.protocol import PROTOCOL_VERSION, parse_envelope
from agentworld.domain.reducers import fold
from agentworld.infra.db import Database
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services import ingest as ingest_service
from helpers import Actor

pytestmark = pytest.mark.integration

KEY_HEADER = "X-AgentWorld-Key"

#: The derived tables a replay must rebuild from the log alone.
DERIVED_TABLES = (
    "agents",
    "agent_status_history",
    "tasks",
    "runs",
    "run_steps",
    "workflow_executions",
    "execution_steps",
)


class Fixture:
    """A workspace, a project, an ingest key, and two registered agents."""

    def __init__(self, client: TestClient, actor: Actor, **key_overrides: Any) -> None:
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Ingest"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()
        body: dict[str, Any] = {
            "name": "Worker",
            "scope": "ingest",
            "project_id": self.project["id"],
        }
        body.update(key_overrides)
        minted = client.post(
            f"/v1/workspaces/{self.workspace['id']}/api-keys",
            json=body,
            headers=actor.headers,
        )
        assert minted.status_code == 201, minted.text
        self.key = str(minted.json()["key"])

    @property
    def headers(self) -> dict[str, str]:
        return {KEY_HEADER: self.key}

    @property
    def project_id(self) -> uuid.UUID:
        return uuid.UUID(self.project["id"])

    @property
    def workspace_id(self) -> uuid.UUID:
        return uuid.UUID(self.workspace["id"])


@pytest.fixture
def ingest(client: TestClient, actor: Actor) -> Fixture:
    return Fixture(client, actor)


@pytest.fixture
async def agents(owner_database: Database, ingest: Fixture) -> dict[str, uuid.UUID]:
    """Two agents, because an agent row is configuration and not an event.

    `agent.registered` announces a worker for an agent someone already
    created; nothing in the protocol can create one, which is the property
    that stops a replay re-creating a definition.
    """
    created: dict[str, uuid.UUID] = {}
    async with owner_database.session() as session:
        for agent_key in ("researcher", "writer"):
            agent_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO agents (id, workspace_id, project_id, agent_key, "
                    " interaction_mode, display_name) "
                    "VALUES (:i, :w, :p, :key, 'managed', :name)"
                ),
                {
                    "i": agent_id,
                    "w": ingest.workspace_id,
                    "p": ingest.project_id,
                    "key": agent_key,
                    "name": agent_key.title(),
                },
            )
            created[agent_key] = agent_id
    return created


def event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "dedup_key": f"e-{uuid.uuid4()}",
        "type": "agent.heartbeat",
        "protocol_version": PROTOCOL_VERSION,
        "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
        "subject": {"kind": "agent", "ref": "researcher"},
    }
    base.update(overrides)
    return base


def post(client: TestClient, fixture: Fixture, *events: dict[str, Any]) -> Any:
    return client.post("/v1/ingest/events", json={"events": list(events)}, headers=fixture.headers)


# ------------------------------------------------------------- the happy path ---


async def test_an_event_is_stored_and_folded(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    # A fixed instant in the recent past: fixed so the assertion below can
    # compare it exactly, past because dating it ahead of the server is what
    # the skew check exists to refuse.
    occurred = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).replace(microsecond=0)
    response = post(
        client,
        ingest,
        event(dedup_key="hb-1", occurred_at=occurred.isoformat()),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body == {
        "accepted": 1,
        "duplicates": 0,
        "highest_seq": 1,
        "outcomes": [{"dedup_key": "hb-1", "accepted": True, "seq": 1, "rows_changed": 1}],
    }

    async with owner_database.session() as session:
        stored = (
            await session.execute(
                text(
                    "SELECT seq, type, agent_id, subject_ref, received_at > occurred_at "
                    "FROM events WHERE dedup_key = 'hb-1'"
                )
            )
        ).one()
        heartbeat = (
            await session.execute(
                text("SELECT last_heartbeat_at FROM agents WHERE agent_key = 'researcher'")
            )
        ).scalar_one()

    assert stored[0] == 1
    assert stored[1] == "agent.heartbeat"
    assert stored[2] == agents["researcher"], "the agent_key resolved to a row"
    assert stored[4] is True, "the server's clock is recorded separately"
    assert heartbeat == occurred, "folded from the event's own time, not now()"


async def test_the_sequence_is_per_project_and_gapless(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID]
) -> None:
    """What WebSocket resume and cursor pagination read."""
    first = post(client, ingest, event(), event(), event()).json()
    second = post(client, ingest, event()).json()

    assert [o["seq"] for o in first["outcomes"]] == [1, 2, 3]
    assert second["outcomes"][0]["seq"] == 4
    assert second["highest_seq"] == 4


# ------------------------------------------------------------------- dedup ---


async def test_the_same_batch_three_times_yields_one_event_each(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """The roadmap's acceptance criterion, verbatim."""
    batch = [event(dedup_key="a"), event(dedup_key="b")]

    first = client.post("/v1/ingest/events", json={"events": batch}, headers=ingest.headers).json()
    assert (first["accepted"], first["duplicates"]) == (2, 0)

    for _ in range(2):
        again = client.post(
            "/v1/ingest/events", json={"events": batch}, headers=ingest.headers
        ).json()
        assert (again["accepted"], again["duplicates"]) == (0, 2)
        assert again["highest_seq"] is None, "a duplicate consumes no sequence number"

    async with owner_database.session() as session:
        count = (await session.execute(text("SELECT count(*) FROM events"))).scalar_one()
    assert count == 2


async def test_a_duplicate_within_one_batch_is_refused(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID]
) -> None:
    """The per-event check runs against what is committed, so it cannot see
    the earlier event in its own transaction. Two rows with one key would be
    a dedup guarantee that silently stopped holding."""
    response = post(client, ingest, event(dedup_key="same"), event(dedup_key="same"))
    assert response.status_code == 422
    assert response.json()["detail"] == "Two events in this batch share a dedup_key"


async def test_a_failed_batch_writes_nothing(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """All-or-nothing, so a retry of the whole batch is simply correct."""
    response = post(client, ingest, event(dedup_key="good"), event(severity="nonsense"))
    assert response.status_code == 422

    async with owner_database.session() as session:
        count = (await session.execute(text("SELECT count(*) FROM events"))).scalar_one()
    assert count == 0, "the valid event in the batch was not written either"


# ------------------------------------------------------- promise 7 at runtime ---


async def test_folding_a_workflow_trace_creates_no_dispatches(
    factory: UnitOfWorkFactory,
    ingest: Fixture,
    agents: dict[str, uuid.UUID],
    owner_database: Database,
) -> None:
    """The structural argument is in `test_reducers.py`; this is the live one.

    Every event below is one the orchestrator would have written *after*
    deciding. Folding them must reproduce what was decided and decide nothing
    itself - no dispatch row, which is the only thing that can cause work.

    **Why this no longer goes through the ingest endpoint.** It used to, and
    T0 made that a 403: `workflow.execution_started` and
    `workflow.step_dispatched` are `Producer.ORCHESTRATOR`, so an API key may
    not write them at all. That is the point of D1 and is asserted in
    `test_producer_authority.py`. The property *this* test is about - that
    folding a decision log decides nothing - belongs at the layer the
    orchestrator itself will use, which is where it now runs: append, fold,
    apply, in one transaction, exactly as `services/executions.py` will.
    """
    execution = str(uuid.uuid4())
    trace = [
        event(
            dedup_key="x-1",
            type="workflow.execution_started",
            subject={"kind": "workflow_execution", "ref": execution},
        ),
        event(
            dedup_key="x-2",
            type="workflow.step_dispatched",
            subject={"kind": "execution_step", "ref": f"{execution}:research"},
            correlation={"execution_ref": execution, "agent_key": "researcher"},
        ),
        event(
            dedup_key="x-3",
            type="workflow.step_succeeded",
            subject={"kind": "execution_step", "ref": f"{execution}:research"},
            correlation={"execution_ref": execution},
        ),
    ]

    scope = TenantScope(workspace_id=ingest.workspace_id, project_id=ingest.project_id)
    async with factory.begin(visible_workspaces={ingest.workspace_id}) as work:
        events_repo = work.events(scope)
        derived = work.derived(scope)
        for raw in trace:
            envelope = parse_envelope(raw)
            event_id = await events_repo.append(
                envelope,
                seq=await events_repo.next_seq(),
                refs=await events_repo.resolve(envelope),
            )
            for mutation in fold(envelope):
                await derived.apply(mutation, event_id=event_id)

    async with owner_database.session() as session:
        dispatches = (await session.execute(text("SELECT count(*) FROM dispatches"))).scalar_one()
        stored = (await session.execute(text("SELECT count(*) FROM events"))).scalar_one()
    assert stored == 3
    assert dispatches == 0, "folding the log caused work"


# ------------------------------------------------------- replay equivalence ---


#: The two tables whose rows folding creates with an id it did not choose.
#: `agent_status_history` has a bigserial and `run_steps` a bigserial, so a
#: replay necessarily mints different ones - identity is not derived state,
#: and a test that demanded it match would be asserting that `TRUNCATE`
#: resets a sequence rather than anything about the log.
#:
#: `runs` is created by folding too, since R1, and is deliberately **not**
#: here: its id is `run_id_for(project_id, run_ref)`, a function of what
#: names it, so a rebuilt run gets the id it had. That is what keeps
#: `run_steps.run_id` comparable as well - the steps point at the same run
#: after a replay as before one, which a random id would have made
#: impossible to assert without dropping the column from the comparison.
APPENDED_TABLES = ("agent_status_history", "run_steps")


async def _snapshot(database: Database) -> dict[str, list[str]]:
    """Every derived row, as comparable strings.

    Sorted rather than ordered by a key, so the comparison is about content
    and not about insertion order - which a replay is also entitled to
    differ on.
    """
    snapshot: dict[str, list[str]] = {}
    async with database.session() as session:
        for table in DERIVED_TABLES:
            # A fixed literal list, not user input.
            column = "to_jsonb(t) - 'id'" if table in APPENDED_TABLES else "to_jsonb(t)"
            rows = (await session.execute(text(f"SELECT {column} FROM {table} t"))).scalars()
            snapshot[table] = sorted(str(row) for row in rows)
    return snapshot


async def test_refolding_the_stored_log_reproduces_the_state(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """Replay equivalence, against the stored rows rather than the objects sent.

    This is what makes the event log worth having: the derived tables can be
    thrown away and rebuilt. The envelopes are reconstructed from `events`, so
    a field that was never persisted would break this and nothing else.
    """
    from agentworld.domain.protocol import parse_envelope
    from agentworld.domain.reducers import fold
    from agentworld.repositories.base import TenantScope
    from agentworld.repositories.derived import DerivedStateRepository
    from agentworld.repositories.events import ResolvedRefs

    stream = [
        event(dedup_key="r-1", type="agent.registered", payload={"capabilities": ["search"]}),
        event(
            dedup_key="r-2",
            type="agent.status_changed",
            payload={"status": "working", "reason": "picked up a task"},
        ),
        # The run and its trace, correlated to the agent that owns it. Since
        # R1 these rebuild rather than staying empty, which is what makes
        # `runs` and `run_steps` part of this comparison rather than two
        # tables it trivially agrees about.
        event(
            dedup_key="r-3",
            type="run.started",
            subject={"kind": "run", "ref": "run-1"},
            correlation={"agent_key": "researcher"},
        ),
        event(
            dedup_key="r-3a",
            type="run.step_started",
            subject={"kind": "run", "ref": "run-1"},
            correlation={"agent_key": "researcher"},
            payload={"seq": 0, "name": "turn", "kind": "llm"},
        ),
        event(
            dedup_key="r-3b",
            type="run.step_finished",
            subject={"kind": "run", "ref": "run-1"},
            correlation={"agent_key": "researcher"},
            payload={"seq": 0, "output": {"tokens": 240}},
        ),
        event(
            dedup_key="r-4",
            type="agent.paused",
            payload={"reason": "operator"},
        ),
    ]
    assert post(client, ingest, *stream).status_code == 202
    before = await _snapshot(owner_database)
    assert before["agent_status_history"], "the fixture should have produced history"
    # Named explicitly, because a replay of two empty tables agrees about
    # nothing and would pass. R1 is what made these non-empty.
    assert before["runs"], "the fixture should have produced a run"
    assert before["run_steps"], "the fixture should have produced a trace step"
    # And the activity clock stage A1 added, for the same reason: a replay
    # that agreed about NULL would agree about nothing. The trace in this
    # stream is what moves it.
    # `_snapshot` stringifies the decoded jsonb, so the row is a Python
    # mapping repr rather than JSON text.
    assert ast.literal_eval(before["runs"][0])["last_activity_at"] is not None, (
        "the trace should have moved the run's activity clock"
    )

    # Throw the derived state away - everything except the agent rows, which
    # are configuration and were never derived from the log.
    async with owner_database.session() as session:
        await session.execute(text("TRUNCATE agent_status_history, run_steps, runs CASCADE"))
        # `registered_at` is NOT NULL - it is set when the agent row is
        # created and then moved by `agent.registered` - so it gets a
        # sentinel rather than a null. If the replay fails to overwrite it,
        # the comparison below sees the year 2000 and says so.
        await session.execute(
            text(
                "UPDATE agents SET status = 'provisioning', status_reason = NULL, "
                " last_event_at = NULL, last_heartbeat_at = NULL, "
                " registered_at = '2000-01-01T00:00:00Z', "
                " capabilities = '{}'::text[] "
                # Only the agent the log actually talks about. Replay rebuilds
                # what the events describe; an agent no event mentions keeps
                # whatever it had, and resetting it here would be asking the
                # log to restore something it never recorded.
                "WHERE agent_key = 'researcher'"
            )
        )

    # Replay: rebuild each envelope from its stored row and fold it again.
    scope = TenantScope(workspace_id=ingest.workspace_id, project_id=ingest.project_id)
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, dedup_key, type, protocol_version, occurred_at, "
                    "       subject_kind, subject_ref, correlation, severity, labels, payload, "
                    # Resolved when the event was first ingested and stored on
                    # the row, so a replay needs no second lookup - and could
                    # not do one honestly anyway, since the agent may have
                    # been renamed or removed since.
                    "       agent_id "
                    "  FROM events WHERE project_id = :p ORDER BY seq"
                ),
                {"p": ingest.project_id},
            )
        ).all()
        derived = DerivedStateRepository(session, scope)

        for row in rows:
            envelope = parse_envelope(
                {
                    "dedup_key": row.dedup_key,
                    "type": row.type,
                    "protocol_version": row.protocol_version,
                    "occurred_at": row.occurred_at,
                    "subject": {"kind": row.subject_kind, "ref": row.subject_ref},
                    "correlation": row.correlation,
                    "severity": row.severity,
                    "labels": row.labels,
                    "payload": row.payload,
                }
            )
            refs = ResolvedRefs(agent_id=row.agent_id)
            for mutation in fold(envelope):
                await derived.apply(mutation, event_id=row.id, refs=refs)

    after = await _snapshot(owner_database)
    _assert_same(before, after)


def _assert_same(before: dict[str, list[str]], after: dict[str, list[str]]) -> None:
    """Compare snapshots and say *which column* moved.

    A bare `==` on two dicts of long json strings produces a diff pytest
    truncates, which turns a one-column regression into a bisect.
    """
    import ast

    for table in DERIVED_TABLES:
        old_rows, new_rows = before[table], after[table]
        assert len(old_rows) == len(new_rows), (
            f"{table}: replay produced {len(new_rows)} rows, not {len(old_rows)}"
        )
        for old_row, new_row in zip(old_rows, new_rows, strict=True):
            old_dict = ast.literal_eval(old_row)
            new_dict = ast.literal_eval(new_row)
            differing = {
                key: (old_dict.get(key), new_dict.get(key))
                for key in old_dict.keys() | new_dict.keys()
                if old_dict.get(key) != new_dict.get(key)
            }
            assert not differing, f"{table}: replay changed {differing}"


# ------------------------------------------------------------------- limits ---


async def test_an_empty_batch_is_refused(client: TestClient, ingest: Fixture) -> None:
    response = client.post("/v1/ingest/events", json={"events": []}, headers=ingest.headers)
    assert response.status_code == 422


async def test_a_clock_set_far_in_the_future_is_refused(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID]
) -> None:
    """Otherwise the events sort before everything real for an hour, and a
    cursor that has passed them never returns them."""
    ahead = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    response = post(client, ingest, event(occurred_at=ahead.isoformat()))
    assert response.status_code == 422
    assert "clock" in response.json()["detail"]


async def test_a_clock_running_behind_is_accepted(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID]
) -> None:
    """Being late is normal: an event takes time to arrive."""
    behind = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
    assert post(client, ingest, event(occurred_at=behind.isoformat())).status_code == 202


async def test_a_malformed_event_names_its_position_in_the_batch(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID]
) -> None:
    """An integrator sending 500 events needs to know which one."""
    response = post(client, ingest, event(), event(occurred_at="not a date"))
    assert response.status_code == 422
    assert response.json().get("index") == 1


# -------------------------------------------------------------- the key plane ---


async def test_a_control_token_cannot_ingest(
    client: TestClient, ingest: Fixture, actor: Actor
) -> None:
    assert post_with(client, actor.headers, event()).status_code == 401


def post_with(client: TestClient, headers: dict[str, str], *events: dict[str, Any]) -> Any:
    return client.post("/v1/ingest/events", json={"events": list(events)}, headers=headers)


async def test_a_key_may_not_speak_for_an_agent_outside_its_allowlist(
    client: TestClient, actor: Actor, owner_database: Database
) -> None:
    """A key leaked from one worker must not become any other worker."""
    restricted = Fixture(client, actor, agent_key_allowlist=["researcher"])
    async with owner_database.session() as session:
        for agent_key in ("researcher", "writer"):
            await session.execute(
                text(
                    "INSERT INTO agents (id, workspace_id, project_id, agent_key, "
                    " interaction_mode, display_name) "
                    "VALUES (:i, :w, :p, :key, 'managed', :name)"
                ),
                {
                    "i": uuid.uuid4(),
                    "w": restricted.workspace_id,
                    "p": restricted.project_id,
                    "key": agent_key,
                    "name": agent_key.title(),
                },
            )

    assert (
        post(client, restricted, event(subject={"kind": "agent", "ref": "researcher"})).status_code
        == 202
    )

    denied = post(client, restricted, event(subject={"kind": "agent", "ref": "writer"}))
    assert denied.status_code == 403
    assert "writer" in denied.json()["detail"]


async def test_events_land_in_the_keys_own_project(
    client: TestClient,
    actor: Actor,
    make_actor: Any,
    ingest: Fixture,
    agents: dict[str, uuid.UUID],
    owner_database: Database,
) -> None:
    """The project comes from the key, never from the request body.

    So there is no field for a caller to set to someone else's project -
    which is the shape that makes the whole plane safe to hand out.
    """
    assert post(client, ingest, event(project_id=str(uuid.uuid4()))).status_code == 202

    async with owner_database.session() as session:
        project_id = (await session.execute(text("SELECT project_id FROM events"))).scalar_one()
    assert project_id == ingest.project_id


# ------------------------------------------------------- heartbeat, logs, metrics ---


async def test_heartbeat_moves_the_agents_clock(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    response = client.post(
        "/v1/ingest/heartbeat", json={"agent_key": "researcher"}, headers=ingest.headers
    )
    assert response.status_code == 202
    assert response.json()["accepted"] == 1

    async with owner_database.session() as session:
        beat = (
            await session.execute(
                text("SELECT last_heartbeat_at FROM agents WHERE agent_key = 'researcher'")
            )
        ).scalar_one()
    assert beat is not None


async def test_two_heartbeats_in_one_second_are_one_fact(
    client: TestClient,
    ingest: Fixture,
    agents: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry within the second is a retry, not a second heartbeat.

    The clock is pinned, and that is not a convenience. The dedup key is
    `<agent>:hb:<integer second>`, so two real HTTP calls are one fact only
    when they land in the same second - which two calls about ninety
    milliseconds apart do roughly nine times in ten. This test flaked on the
    tenth, having passed by luck in every run before it.

    A test that passes nine times out of ten is worse than no test: it gets
    believed when it fails for a real reason, and it trains whoever sees it
    to re-run the suite. Pinning the clock asserts exactly the documented
    property - same second, same fact - and asserts it every time.
    """

    class _PinnedClock(dt.datetime):
        """`datetime` with a fixed `now`, and everything else intact.

        A subclass rather than a stub: `services.ingest` also uses
        `datetime` for the future-skew check, and a bare object with one
        method would break that for reasons unrelated to the test.
        """

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # type: ignore[override]
            return dt.datetime(2026, 11, 2, 12, 0, 0, 500_000, tzinfo=tz or dt.UTC)

    monkeypatch.setattr(ingest_service, "datetime", _PinnedClock)

    body = {"agent_key": "researcher"}
    first = client.post("/v1/ingest/heartbeat", json=body, headers=ingest.headers).json()
    second = client.post("/v1/ingest/heartbeat", json=body, headers=ingest.headers).json()
    assert first["accepted"] == 1
    assert second["duplicates"] == 1, "a retry within the second is not a second heartbeat"


async def test_heartbeats_in_different_seconds_are_two_facts(
    client: TestClient,
    ingest: Fixture,
    agents: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half, which the flaky version silently relied on.

    Without this, pinning the clock above could hide a dedup key that
    ignored the second entirely - every heartbeat would be a duplicate and
    the test would still pass.
    """
    seconds = iter([0, 0, 1])

    class _TickingClock(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # type: ignore[override]
            return dt.datetime(2026, 11, 2, 12, 0, next(seconds, 1), tzinfo=tz or dt.UTC)

    monkeypatch.setattr(ingest_service, "datetime", _TickingClock)

    body = {"agent_key": "researcher"}
    client.post("/v1/ingest/heartbeat", json=body, headers=ingest.headers)
    later = client.post("/v1/ingest/heartbeat", json=body, headers=ingest.headers).json()
    assert later["accepted"] == 1, "a heartbeat a second later is a new fact"
    assert later["duplicates"] == 0


async def test_logs_are_stored_and_change_no_state(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    response = client.post(
        "/v1/ingest/logs",
        json={
            "agent_key": "researcher",
            "lines": [
                {
                    "message": "searching for competitors",
                    "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
                    "severity": "info",
                },
                {
                    "message": "rate limited, backing off",
                    "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
                    "severity": "warning",
                },
            ],
        },
        headers=ingest.headers,
    )
    assert response.status_code == 202
    assert response.json() == {"accepted": 2}

    async with owner_database.session() as session:
        stored = (await session.execute(text("SELECT count(*), count(agent_id) FROM logs"))).one()
        events = (await session.execute(text("SELECT count(*) FROM events"))).scalar_one()
    assert stored == (2, 2), "both lines stored, both correlated to the agent"
    assert events == 0, "a log line is output, not an event"


async def test_metrics_are_stored(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    response = client.post(
        "/v1/ingest/metrics",
        json={
            "agent_key": "researcher",
            "samples": [
                {
                    "name": "tokens_used",
                    "value": 8421,
                    "unit": "tokens",
                    "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
                    "labels": {"model": "sonnet"},
                }
            ],
        },
        headers=ingest.headers,
    )
    assert response.status_code == 202

    async with owner_database.session() as session:
        value, unit = (await session.execute(text("SELECT value, unit FROM metric_samples"))).one()
    assert (value, unit) == (8421.0, "tokens")


# ---------------------------------------------------------------------- RLS ---


async def test_one_projects_events_are_invisible_to_another_tenant(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], database: Database
) -> None:
    assert post(client, ingest, event(dedup_key="private")).status_code == 202

    async with database.session(visible_workspaces=[uuid.uuid4()]) as session:
        count = (await session.execute(text("SELECT count(*) FROM events"))).scalar_one()
    assert count == 0

    async with database.session(visible_workspaces=[ingest.workspace_id]) as session:
        count = (await session.execute(text("SELECT count(*) FROM events"))).scalar_one()
    assert count == 1


# -------------------------------------------- stale and out-of-order delivery ---


async def test_a_stale_event_does_not_overwrite_a_newer_one(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """A worker that was offline flushes a backlog, late.

    The events arrive in the order it managed to send them, which is not the
    order things happened. Applied blindly, the oldest event in the batch
    decides what the agent looks like - and the island shows it paused while
    it works.
    """
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    recent = (now - dt.timedelta(minutes=1)).isoformat()
    ancient = (now - dt.timedelta(minutes=30)).isoformat()

    assert (
        post(
            client,
            ingest,
            event(
                dedup_key="s-recent",
                type="agent.status_changed",
                occurred_at=recent,
                payload={"status": "working"},
            ),
        ).status_code
        == 202
    )

    assert (
        post(
            client,
            ingest,
            event(
                dedup_key="s-ancient",
                type="agent.status_changed",
                occurred_at=ancient,
                payload={"status": "paused"},
            ),
        ).status_code
        == 202
    )

    async with owner_database.session() as session:
        status, history = (
            await session.execute(
                text(
                    "SELECT a.status, (SELECT count(*) FROM agent_status_history) "
                    "FROM agents a WHERE a.agent_key = 'researcher'"
                )
            )
        ).one()

    assert status == "working", "a 30-minute-old event overwrote a newer one"
    # **One row, not two, since stage D1.1.** This asserted two, with the
    # reasoning that what the agent *is* is guarded while what happened is
    # not rewritten - a defensible reading of a log. D1.1 took the other
    # one: a status event's history append now carries the same
    # `not_before` as its patch, so a late event records nothing because it
    # changed nothing. A row for it would say the agent moved to `paused`
    # while the agent is `working`, which is a transition that did not
    # happen - and `GET .../status-history` publishes that timeline now,
    # where before this table had no reader at all.
    assert history == 1


async def test_delivery_order_does_not_change_the_final_state(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """The same three events, sent newest-first, converge on the same answer."""
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    moments = [
        (now - dt.timedelta(minutes=5), "idle"),
        (now - dt.timedelta(minutes=3), "working"),
        (now - dt.timedelta(minutes=1), "blocked"),
    ]

    for index, (when, status) in enumerate(reversed(moments)):
        assert (
            post(
                client,
                ingest,
                event(
                    dedup_key=f"order-{index}",
                    type="agent.status_changed",
                    occurred_at=when.isoformat(),
                    payload={"status": status},
                ),
            ).status_code
            == 202
        )

    async with owner_database.session() as session:
        final = (
            await session.execute(text("SELECT status FROM agents WHERE agent_key = 'researcher'"))
        ).scalar_one()
    assert final == "blocked", "the newest event should decide, whenever it arrived"


async def test_a_stale_task_update_is_ignored_too(
    client: TestClient, ingest: Fixture, agents: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """A finished task must not be dragged back to running by a late start."""
    task_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO tasks (id, workspace_id, project_id, task_ref, title) "
                "VALUES (:i, :w, :p, 'task-1', 'Research')"
            ),
            {"i": task_id, "w": ingest.workspace_id, "p": ingest.project_id},
        )

    assert (
        post(
            client,
            ingest,
            event(
                dedup_key="t-done",
                type="task.completed",
                subject={"kind": "task", "ref": "task-1"},
                occurred_at=(now - dt.timedelta(minutes=1)).isoformat(),
            ),
        ).status_code
        == 202
    )

    assert (
        post(
            client,
            ingest,
            event(
                dedup_key="t-late-start",
                type="task.started",
                subject={"kind": "task", "ref": "task-1"},
                occurred_at=(now - dt.timedelta(minutes=10)).isoformat(),
            ),
        ).status_code
        == 202
    )

    async with owner_database.session() as session:
        status = (
            await session.execute(text("SELECT status FROM tasks WHERE task_ref = 'task-1'"))
        ).scalar_one()
    assert status == "completed"

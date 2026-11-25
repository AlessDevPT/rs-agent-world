"""The first transactional definition event: `AgentService.update`. M3.

Every test here is about one claim: **the agent row and the event describing it
are written together or not at all**, and the number the event carries comes
from the shared per-project cursor (P9) allocated before the row is touched.

The properties that are *not* here are deliberate. That the eleven types are
`CONTROL_PLANE`, fold into nothing and cannot be forged with an ingest key is
M2's, pinned by `test_producer_authority.py`, `test_protocol_envelope.py` and
`test_reducers.py`; repeating it would make this file look like the boundary
when it is not.

These tests drive `AgentService` rather than the HTTP route, because three of
them need a repository to fail mid-transaction and one needs to watch the
statements go past. The route has one caller and no logic of its own
(`api/v1/agents.py:745`), and `test_agents_api.py` already covers it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from agentworld.domain.enums import MemberRole
from agentworld.domain.errors import ConflictError
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.audit import AuditLogRepository
from agentworld.repositories.events import EventRepository
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.definitions import AgentService
from helpers import Actor

pytestmark = pytest.mark.integration

DEFINITION_TYPE = "definition.agent.updated"


class World:
    """A workspace, a project and an agent, built over HTTP like a client would.

    Set up through the API rather than by seeding rows, so the starting state is
    one the application actually produces - including the absence of a cursor
    row, which matters: creating an agent appends no event, because `create` is
    M4's and not M3's.
    """

    def __init__(self, client: TestClient, actor: Actor) -> None:
        self.actor = actor
        workspace = client.post("/v1/workspaces", json={"name": "Deltas"}, headers=actor.headers)
        assert workspace.status_code == 201, workspace.text
        self.workspace = workspace.json()

        project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        )
        assert project.status_code == 201, project.text
        self.project = project.json()

        self.agent = self.add_agent(client, "Researcher")

    def add_agent(self, client: TestClient, display_name: str) -> Any:
        created = client.post(
            f"/v1/projects/{self.project['id']}/agents",
            json={"display_name": display_name, "interaction_mode": "managed"},
            headers=self.actor.headers,
        )
        assert created.status_code == 201, created.text
        return created.json()

    @property
    def workspace_id(self) -> uuid.UUID:
        return uuid.UUID(self.workspace["id"])

    @property
    def project_id(self) -> uuid.UUID:
        return uuid.UUID(self.project["id"])

    @property
    def agent_id(self) -> uuid.UUID:
        return uuid.UUID(self.agent["id"])

    @property
    def principal(self) -> UserPrincipal:
        return UserPrincipal(
            user_id=self.actor.user_id,
            email=self.actor.email,
            memberships={self.workspace_id: MemberRole.OWNER},
        )


@pytest.fixture
def world(client: TestClient, actor: Actor) -> World:
    return World(client, actor)


@pytest_asyncio.fixture
async def service(factory: UnitOfWorkFactory) -> AgentService:
    return AgentService(factory)


def _context(request_id: str = "r1") -> CallContext:
    return CallContext(request_id=request_id)


# ------------------------------------------------------------------ readers ---
#
# All three read as the owner role, which is a superuser and so sees past
# row-level security. That is the point: these assertions are about what is in
# the database, not about what the repository under test would show us.


async def _events(database: Database, event_type: str = DEFINITION_TYPE) -> list[dict[str, Any]]:
    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT seq, type, dedup_key, subject_kind, subject_ref, payload, "
                    "protocol_version, agent_id, task_id, run_id, execution_id, "
                    "execution_step_id FROM events WHERE type = :t ORDER BY seq"
                ),
                {"t": event_type},
            )
        ).mappings()
        return [dict(row) for row in rows]


async def _audit_count(database: Database, action: str = "agent.update") -> int:
    async with database.session() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action}
                )
            ).scalar_one()
        )


async def _cursor(database: Database, project_id: uuid.UUID) -> int | None:
    """This project's `next_seq`, or None while no cursor row exists yet.

    None is a meaningful answer, not a missing one: the row is created on first
    use by `_ensure_cursor`, so its absence proves nothing has been allocated.
    """
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT next_seq FROM project_event_cursors WHERE project_id = :p"),
                {"p": project_id},
            )
        ).scalar_one_or_none()


async def _display_name(database: Database, agent_id: uuid.UUID) -> str:
    async with database.session() as session:
        return str(
            (
                await session.execute(
                    text("SELECT display_name FROM agents WHERE id = :i"), {"i": agent_id}
                )
            ).scalar_one()
        )


# --------------------------------------------------------- the happy path ---


async def test_an_update_writes_exactly_one_event_and_one_audit_row(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """One edit, one event, one audit row. The whole of M3 in one assertion.

    `len(...) == 1` rather than `>= 1`: a second event would mean the append ran
    twice, which is exactly what a retry loop or a duplicated call site would
    produce, and a truthy check would not notice.
    """
    await service.update(
        world.principal, _context(), world.project_id, world.agent_id, display_name="Senior"
    )

    assert len(await _events(owner_database)) == 1
    assert await _audit_count(owner_database) == 1
    assert await _display_name(owner_database, world.agent_id) == "Senior"


async def test_the_event_is_shaped_the_way_the_decisions_say(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """Every field of the envelope, against the decision that fixed it.

    Asserted field by field rather than against a whole expected row, so a
    failure names the convention that moved.
    """
    await service.update(
        world.principal, _context(), world.project_id, world.agent_id, display_name="Senior"
    )
    row = (await _events(owner_database))[0]

    # P11.1: the persisted name is `definition.agent.updated`, *not* the
    # `agent.updated` client frame - that namespace has a reducer.
    assert row["type"] == DEFINITION_TYPE
    # P11.3: subject is the project; the entity travels in the payload, because
    # `SubjectKind.AGENT.subject_ref` is the agent_key rather than a uuid.
    assert row["subject_kind"] == "project"
    assert row["subject_ref"] == str(world.project_id)
    assert row["payload"] == {"entity": "agent", "id": str(world.agent_id)}
    # P12.1: the key carries the allocated seq, so a second edit of the same
    # agent in the same month cannot collide with this one.
    assert row["dedup_key"] == f"def:agent:{world.agent_id}:updated:{row['seq']}"
    # P12.3: the resolved reference columns stay NULL, which keeps the row off
    # `events_agent_idx` and so out of the raw activity feed (P10.3).
    assert row["agent_id"] is None
    assert row["task_id"] is None
    assert row["run_id"] is None
    assert row["execution_id"] is None
    assert row["execution_step_id"] is None
    # The first event in a fresh project. Creating the agent appended nothing.
    assert row["seq"] == 1


async def test_the_event_carries_no_configuration_only_identity(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """P10.3's other half, and the reason the payload is as thin as it is.

    `instructions` and `labels` are free-form and may hold an endpoint or a
    token - the agent detail page withholds them for that reason. An
    append-only log cannot be redacted later, so they must never arrive in one.

    The whole row is searched, not just the payload: putting a value in
    `labels` or `correlation` instead would satisfy a payload-only assertion
    while leaking exactly the same string.
    """
    await service.update(
        world.principal,
        _context(),
        world.project_id,
        world.agent_id,
        instructions="SENTINEL-INSTRUCTION",
        labels={"token": "SENTINEL-LABEL"},
    )

    row = (await _events(owner_database))[0]
    assert set(row["payload"]) == {"entity", "id"}
    serialised = json.dumps(row, default=str)
    assert "SENTINEL-INSTRUCTION" not in serialised
    assert "SENTINEL-LABEL" not in serialised


# ------------------------------------------------- nothing changed, nothing logged ---


async def test_an_empty_patch_appends_no_event_and_spends_no_sequence_number(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """P12.2. The event log follows the mutation, not the request.

    `AgentRepository.update` returns the row untouched when every field arrives
    `UNSET`, so there is nothing to describe. An event here would reach every
    subscriber as a change that never happened, and would cost a cursor
    position to do it.

    The audit row is still written, and that asymmetry is the point: the audit
    log answers "who asked", the event log answers "what changed".
    """
    before = await _cursor(owner_database, world.project_id)
    assert before is None  # nothing has allocated in this project yet

    await service.update(world.principal, _context(), world.project_id, world.agent_id)

    assert await _events(owner_database) == []
    assert await _cursor(owner_database, world.project_id) is None
    assert await _audit_count(owner_database) == 1


# ---------------------------------------------------------------- atomicity ---


async def test_a_refused_update_writes_neither_the_event_nor_the_audit_row(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """A refusal after the authorization checks leaves nothing behind.

    An archived agent is refused by `not_archived`, which raises inside the
    transaction - and `begin()` rolls back on a `DomainError` like any other
    exception, which is what makes "or not at all" true for the ordinary
    failure and not only for the exotic ones.
    """
    await service.archive(world.principal, _context("r1"), world.project_id, world.agent_id)

    with pytest.raises(ConflictError):
        await service.update(
            world.principal, _context("r2"), world.project_id, world.agent_id, display_name="No"
        )

    assert await _events(owner_database) == []
    assert await _audit_count(owner_database, "agent.update") == 0
    assert await _cursor(owner_database, world.project_id) is None


async def test_a_failing_append_rolls_back_the_agent_mutation(
    service: AgentService,
    world: World,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that would otherwise be silent: row written, event lost.

    Without one transaction this is the dangerous failure - the agent is
    changed, nothing records it, and `resume` can never replay a delta that was
    never written. Patched on the real repository, so the service's own call
    site is what fails.
    """

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the append failed")

    monkeypatch.setattr(EventRepository, "append", explode)

    with pytest.raises(RuntimeError):
        await service.update(
            world.principal,
            _context(),
            world.project_id,
            world.agent_id,
            display_name="Rolled Back",
        )

    assert await _display_name(owner_database, world.agent_id) == "Researcher"
    assert await _events(owner_database) == []
    assert await _audit_count(owner_database) == 0


async def test_a_failing_audit_write_rolls_back_both_the_mutation_and_the_event(
    service: AgentService,
    world: World,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit row is inside the same transaction, so it can veto the change.

    `audit.record` runs after the append, which makes this the one ordering
    where a later failure has to undo an event that was already inserted.
    """

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the audit write failed")

    monkeypatch.setattr(AuditLogRepository, "append", explode)

    with pytest.raises(RuntimeError):
        await service.update(
            world.principal, _context(), world.project_id, world.agent_id, display_name="Nope"
        )

    assert await _display_name(owner_database, world.agent_id) == "Researcher"
    assert await _events(owner_database) == []


async def test_a_rolled_back_update_leaves_no_gap_in_the_sequence(
    service: AgentService,
    world: World,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed transaction consumes no sequence number.

    This is the property the whole shared-cursor design rests on: `resume`
    means "everything after N", so a gap is indistinguishable from an event a
    client never received, and a subscriber would wait for a number that will
    never arrive.

    It holds because `next_seq` advances a *table row* rather than a PostgreSQL
    sequence object, so the rollback un-advances it. Asserted end to end -
    succeed, fail, succeed - because that is the arrangement a sequence object
    would break, and `1, 2` is the only answer that shows it.
    """
    await service.update(
        world.principal, _context("r1"), world.project_id, world.agent_id, display_name="One"
    )

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the append failed")

    monkeypatch.setattr(EventRepository, "append", explode)
    with pytest.raises(RuntimeError):
        await service.update(
            world.principal, _context("r2"), world.project_id, world.agent_id, display_name="Two"
        )
    monkeypatch.undo()

    await service.update(
        world.principal, _context("r3"), world.project_id, world.agent_id, display_name="Three"
    )

    assert [row["seq"] for row in await _events(owner_database)] == [1, 2]


async def test_concurrent_updates_in_one_project_get_distinct_ordered_numbers(
    service: AgentService, world: World, client: TestClient, owner_database: Database
) -> None:
    """Two transactions, one cursor, no duplicates.

    Two agents rather than one, so the test is about the project's cursor
    rather than about row contention on a single agent. Each call opens its own
    transaction through the factory, so the serialisation being demonstrated is
    the cursor's `FOR UPDATE` and nothing else.
    """
    second = world.add_agent(client, "Analyst")

    await asyncio.gather(
        service.update(
            world.principal, _context("r1"), world.project_id, world.agent_id, display_name="A"
        ),
        service.update(
            world.principal,
            _context("r2"),
            world.project_id,
            uuid.UUID(second["id"]),
            display_name="B",
        ),
    )

    rows = await _events(owner_database)
    assert [row["seq"] for row in rows] == [1, 2]
    assert len({row["dedup_key"] for row in rows}) == 2


# --------------------------------------------------------------- lock order ---


async def test_the_cursor_lock_is_taken_before_the_agent_row_is_written(
    service: AgentService, world: World, database: Database
) -> None:
    """The ordering that keeps M3 out of the lock graph's cycles.

    `services/ingest.py` takes the project cursor and *then* folds events into
    `agents`. A definition write that did the opposite - wrote the agent row
    and then reserved its number - would be `agents → cursor` against ingest's
    `cursor → agents`, and an agent edit arriving while a heartbeat batch was
    in flight would deadlock. PostgreSQL would abort one with `40P01`, which is
    not in `CONFLICT_SQLSTATES`, raises `OperationalError` for which no handler
    exists, and so surfaces as a 500 that nothing retries.

    **Statement capture rather than concurrency.** Two concurrent updates pass
    whichever way round the calls are, so a `gather` test would not fail if the
    reservation moved - it would just stop proving anything. Watching the
    statements leave the connection fails the moment the order changes, which
    is the mutation this test exists to catch.
    """
    statements: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        statements.append(" ".join(statement.split()))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        await service.update(
            world.principal, _context(), world.project_id, world.agent_id, display_name="Ordered"
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    def first(predicate: Any, what: str) -> int:
        for index, statement in enumerate(statements):
            if predicate(statement):
                return index
        raise AssertionError(f"no statement {what} in:\n" + "\n".join(statements))

    cursor_lock = first(
        lambda s: "project_event_cursors" in s and "FOR UPDATE" in s,
        "locked the project cursor",
    )
    agent_write = first(lambda s: s.startswith("UPDATE agents"), "wrote the agent row")
    event_insert = first(lambda s: s.startswith("INSERT INTO events"), "inserted the event")

    assert cursor_lock < agent_write, "the cursor must be locked before the agent row is written"
    assert agent_write < event_insert

"""Transactional definition events for the agent lifecycle. M3 and M4.

Every test here is about one claim: **the agent row and the event describing it
are written together or not at all**, and the number the event carries comes
from the shared per-project cursor (P9) allocated before the row is touched.

Four producers, all of `AgentService`: `create`, `update`, `archive` and
`restore` (P13's inventory). `update` came with M3; the other three with M4.

**No assertion here hard-codes a sequence number.** It did until M4, on the
premise that setting a project up appended nothing - and wiring `create` made
that premise false, which is exactly the trap the `baseline` fixture exists to
close. Every expectation is relative to the cursor position captured *after*
setup, so the next producer wired into this file moves no numbers.

The properties that are *not* here are deliberate. That the eleven types are
`CONTROL_PLANE`, fold into nothing and cannot be forged with an ingest key is
M2's, pinned by `test_producer_authority.py`, `test_protocol_envelope.py` and
`test_reducers.py` - the first of those parametrised over all eleven, so these
three are covered there. Repeating it would make this file look like the
boundary when it is not.

These tests drive `AgentService` rather than the HTTP routes, because several
need a repository to fail mid-transaction and two need to watch the statements
go past. The routes have one caller each and no logic of their own, and
`test_agents_api.py` already covers them.
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

from agentworld.domain.enums import AgentInteractionMode, MemberRole
from agentworld.domain.errors import ConflictError, NotFoundError, PermissionDeniedError
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.audit import AuditLogRepository
from agentworld.repositories.events import EventRepository
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.definitions import AgentService
from helpers import Actor

pytestmark = pytest.mark.integration

CREATED = "definition.agent.created"
UPDATED = "definition.agent.updated"
ARCHIVED = "definition.agent.archived"
RESTORED = "definition.agent.restored"


class World:
    """A workspace, a project and an agent, built over HTTP like a client would.

    Set up through the API rather than by seeding rows, so the starting state is
    one the application actually produces. **Since M4 that includes a
    `definition.agent.created` event and a cursor row**, which is why nothing
    below may assume the project starts empty - see the `baseline` fixture.
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

    def member(self) -> UserPrincipal:
        """The same person at `member`, for the authorization refusals.

        No `workspace_members` row is needed: row-level security is driven by
        the principal's memberships, and `authorize` reads the role from the
        same place. `test_agent_test_service.py` builds principals this way.
        """
        return UserPrincipal(
            user_id=self.actor.user_id,
            email=self.actor.email,
            memberships={self.workspace_id: MemberRole.MEMBER},
        )


@pytest.fixture
def world(client: TestClient, actor: Actor) -> World:
    return World(client, actor)


@pytest_asyncio.fixture
async def service(factory: UnitOfWorkFactory) -> AgentService:
    return AgentService(factory)


@pytest_asyncio.fixture
async def baseline(world: World, owner_database: Database) -> int:
    """The number the next definition event in this project will carry.

    Captured once setup has finished, because setup creates an agent and since
    M4 that is itself a definition change. Returning the *next* number rather
    than the cursor row's raw contents means a test reads
    `assert row["seq"] == baseline` and stays true however many events setting
    a project up comes to involve.
    """
    return await _next_seq(owner_database, world.project_id)


def _context(request_id: str = "r1") -> CallContext:
    return CallContext(request_id=request_id)


# ------------------------------------------------------------------ readers ---
#
# All of these read as the owner role, which is a superuser and so sees past
# row-level security. That is the point: these assertions are about what is in
# the database, not about what the repository under test would show us.


async def _events(database: Database, event_type: str) -> list[dict[str, Any]]:
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


async def _definition_log(database: Database, project_id: uuid.UUID) -> list[tuple[int, str]]:
    """Every definition event in this project, as `(seq, type)`, in order."""
    async with database.session() as session:
        rows = await session.execute(
            text(
                "SELECT seq, type FROM events "
                "WHERE project_id = :p AND type LIKE 'definition.%' ORDER BY seq"
            ),
            {"p": project_id},
        )
        return [(int(seq), str(kind)) for seq, kind in rows.all()]


async def _audit_count(database: Database, action: str) -> int:
    async with database.session() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action}
                )
            ).scalar_one()
        )


async def _cursor(database: Database, project_id: uuid.UUID) -> int | None:
    """This project's `next_seq`, or None while no cursor row exists yet."""
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT next_seq FROM project_event_cursors WHERE project_id = :p"),
                {"p": project_id},
            )
        ).scalar_one_or_none()


async def _next_seq(database: Database, project_id: uuid.UUID) -> int:
    """The number the next definition event will carry.

    The cursor row is created on first use by `_ensure_cursor`, so its absence
    means nothing has been allocated and the next number is 1. Collapsing that
    into an `int` is what lets every assertion be an equality rather than a
    two-case check.
    """
    value = await _cursor(database, project_id)
    return 1 if value is None else int(value)


async def _display_name(database: Database, agent_id: uuid.UUID) -> str:
    async with database.session() as session:
        return str(
            (
                await session.execute(
                    text("SELECT display_name FROM agents WHERE id = :i"), {"i": agent_id}
                )
            ).scalar_one()
        )


async def _agent_exists(database: Database, project_id: uuid.UUID, agent_key: str) -> bool:
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT 1 FROM agents WHERE project_id = :p AND agent_key = :k"),
                {"p": project_id, "k": agent_key},
            )
        ).scalar_one_or_none() is not None


async def _archived(database: Database, agent_id: uuid.UUID) -> bool:
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT archived_at FROM agents WHERE id = :i"), {"i": agent_id}
            )
        ).scalar_one() is not None


def _assert_shape(
    row: dict[str, Any], *, event_type: str, project_id: uuid.UUID, agent_id: uuid.UUID, seq: int
) -> None:
    """Every field of the envelope, against the decision that fixed it.

    One helper rather than four copies, because the shape is identical across
    the lifecycle and a difference between the types would be a bug rather
    than a feature.
    """
    # P11.1: the persisted name, *not* the `agent.updated` client frame - that
    # namespace has a reducer.
    assert row["type"] == event_type
    # P11.3: subject is the project; the entity travels in the payload, because
    # `SubjectKind.AGENT.subject_ref` is the agent_key rather than a uuid.
    assert row["subject_kind"] == "project"
    assert row["subject_ref"] == str(project_id)
    assert row["payload"] == {"entity": "agent", "id": str(agent_id)}
    # P12.1: the key carries the allocated seq, so a second event about the
    # same agent in the same month cannot collide with this one.
    operation = event_type.rsplit(".", 1)[1]
    assert row["dedup_key"] == f"def:agent:{agent_id}:{operation}:{seq}"
    assert row["seq"] == seq
    # P12.3: the resolved reference columns stay NULL, which keeps the row off
    # `events_agent_idx` and so out of the raw activity feed (P10.3).
    assert row["agent_id"] is None
    assert row["task_id"] is None
    assert row["run_id"] is None
    assert row["execution_id"] is None
    assert row["execution_step_id"] is None


# ------------------------------------------------------------------- update ---


async def test_an_update_writes_exactly_one_event_and_one_audit_row(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """One edit, one event, one audit row.

    `len(...) == 1` rather than `>= 1`: a second event would mean the append
    ran twice, which is what a retry loop or a duplicated call site would
    produce, and a truthy check would not notice.
    """
    await service.update(
        world.principal, _context(), world.project_id, world.agent_id, display_name="Senior"
    )

    assert len(await _events(owner_database, UPDATED)) == 1
    assert await _audit_count(owner_database, "agent.update") == 1
    assert await _display_name(owner_database, world.agent_id) == "Senior"


async def test_the_update_event_is_shaped_the_way_the_decisions_say(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    await service.update(
        world.principal, _context(), world.project_id, world.agent_id, display_name="Senior"
    )
    row = (await _events(owner_database, UPDATED))[0]
    _assert_shape(
        row,
        event_type=UPDATED,
        project_id=world.project_id,
        agent_id=world.agent_id,
        seq=baseline,
    )


async def test_the_update_event_carries_no_configuration_only_identity(
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

    row = (await _events(owner_database, UPDATED))[0]
    assert set(row["payload"]) == {"entity", "id"}
    serialised = json.dumps(row, default=str)
    assert "SENTINEL-INSTRUCTION" not in serialised
    assert "SENTINEL-LABEL" not in serialised


async def test_an_empty_patch_appends_no_event_and_spends_no_sequence_number(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    """P12.2. The event log follows the mutation, not the request.

    `AgentRepository.update` returns the row untouched when every field arrives
    `UNSET`, so there is nothing to describe. An event here would reach every
    subscriber as a change that never happened, and would cost a cursor
    position to do it.

    The audit row is still written, and that asymmetry is the point: the audit
    log answers "who asked", the event log answers "what changed".
    """
    await service.update(world.principal, _context(), world.project_id, world.agent_id)

    assert await _events(owner_database, UPDATED) == []
    assert await _next_seq(owner_database, world.project_id) == baseline
    assert await _audit_count(owner_database, "agent.update") == 1


async def test_a_refused_update_writes_neither_the_event_nor_the_audit_row(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """A refusal after the authorization checks leaves nothing behind.

    An archived agent is refused by `not_archived`, which raises inside the
    transaction - and `begin()` rolls back on a `DomainError` like any other
    exception, which is what makes "or not at all" true for the ordinary
    failure and not only for the exotic ones.

    The number to compare against is read **after** the archive, not from the
    `baseline` fixture: since M4 the archive is itself a definition change and
    legitimately spends one. Taking the fixture's value here would assert that
    the archive had no effect, which would be a false test of a true property.
    """
    await service.archive(world.principal, _context("r1"), world.project_id, world.agent_id)
    after_archive = await _next_seq(owner_database, world.project_id)

    with pytest.raises(ConflictError):
        await service.update(
            world.principal, _context("r2"), world.project_id, world.agent_id, display_name="No"
        )

    assert await _events(owner_database, UPDATED) == []
    assert await _audit_count(owner_database, "agent.update") == 0
    assert await _next_seq(owner_database, world.project_id) == after_archive


# -------------------------------------------------------------------- create ---


async def test_a_create_writes_one_created_event(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    """The event whose id does not exist until the row does.

    `create` reserves its number before the INSERT and appends after it, which
    is the only operation in this file where the reservation and the append are
    not adjacent.
    """
    agent = await service.create(
        world.principal,
        _context(),
        world.project_id,
        display_name="Analyst",
        interaction_mode=AgentInteractionMode.MANAGED,
    )

    rows = await _events(owner_database, CREATED)
    # Two: setting the world up created one agent, and this created another.
    assert len(rows) == 2
    _assert_shape(
        rows[-1],
        event_type=CREATED,
        project_id=world.project_id,
        agent_id=agent.id,
        seq=baseline,
    )


async def test_the_create_event_carries_no_configuration_only_identity(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """`create` takes `**fields`, so it is the widest door to the payload.

    An agent can be created with its instructions and labels in the same
    request that brings it into existence, which makes this the operation most
    able to put free-form text into an append-only log.
    """
    agent = await service.create(
        world.principal,
        _context(),
        world.project_id,
        display_name="Analyst",
        interaction_mode=AgentInteractionMode.MANAGED,
        instructions="SENTINEL-INSTRUCTION",
        labels={"token": "SENTINEL-LABEL"},
    )

    row = next(
        r for r in await _events(owner_database, CREATED) if r["payload"]["id"] == str(agent.id)
    )
    assert set(row["payload"]) == {"entity", "id"}
    serialised = json.dumps(row, default=str)
    assert "SENTINEL-INSTRUCTION" not in serialised
    assert "SENTINEL-LABEL" not in serialised


async def test_a_duplicate_key_is_refused_without_spending_a_number(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    """The 409 is resolved before the cursor is touched.

    `key_taken` is a read, so `create` keeps `update`'s property: a refusal
    costs no cursor position and leaves no event.
    """
    with pytest.raises(ConflictError):
        await service.create(
            world.principal,
            _context(),
            world.project_id,
            display_name="Researcher",  # the key the world already took
            interaction_mode=AgentInteractionMode.MANAGED,
        )

    assert len(await _events(owner_database, CREATED)) == 1  # only the world's own
    assert await _next_seq(owner_database, world.project_id) == baseline


async def test_a_member_who_may_not_configure_creates_nothing(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    """Authorization is unchanged, and it still runs before the cursor lock.

    A `member` may create an agent and may not give it a model, so creating one
    *with* a model profile is refused - and refused before `_owned_profile`
    looks anything up, which is why the profile id here need not exist.
    """
    with pytest.raises(PermissionDeniedError):
        await service.create(
            world.member(),
            _context(),
            world.project_id,
            display_name="Analyst",
            interaction_mode=AgentInteractionMode.MANAGED,
            model_profile_id=uuid.uuid4(),
        )

    assert len(await _events(owner_database, CREATED)) == 1
    assert await _next_seq(owner_database, world.project_id) == baseline
    assert not await _agent_exists(owner_database, world.project_id, "analyst")


async def test_a_failing_append_rolls_back_the_created_agent(
    service: AgentService,
    world: World,
    owner_database: Database,
    baseline: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent must not survive its own creation event failing.

    For `update` a lost event means a stale client; for `create` it means an
    agent that exists and that no subscriber will ever hear about, which is
    worse. Patched on the real repository so the service's own call site fails.
    """

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the append failed")

    monkeypatch.setattr(EventRepository, "append", explode)

    with pytest.raises(RuntimeError):
        await service.create(
            world.principal,
            _context(),
            world.project_id,
            display_name="Analyst",
            interaction_mode=AgentInteractionMode.MANAGED,
        )

    assert not await _agent_exists(owner_database, world.project_id, "analyst")
    assert len(await _events(owner_database, CREATED)) == 1
    assert await _next_seq(owner_database, world.project_id) == baseline


# ------------------------------------------------------------------- archive ---


async def test_an_archive_writes_one_archived_event(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    """Archiving is a definition change, not a deletion.

    The row stays and stays readable by id, so the persisted type is
    `.archived` and the client frame is `agent.updated` (P11.4).
    """
    await service.archive(world.principal, _context(), world.project_id, world.agent_id)

    rows = await _events(owner_database, ARCHIVED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=ARCHIVED,
        project_id=world.project_id,
        agent_id=world.agent_id,
        seq=baseline,
    )
    assert await _archived(owner_database, world.agent_id)


async def test_archiving_an_archived_agent_is_refused_without_spending_a_number(
    service: AgentService, world: World, owner_database: Database
) -> None:
    """`already_archived` is a read-based refusal, so it costs no number.

    The baseline is taken *after* the first archive rather than from the
    fixture, because that archive legitimately spent one.
    """
    await service.archive(world.principal, _context("r1"), world.project_id, world.agent_id)
    after_first = await _next_seq(owner_database, world.project_id)

    with pytest.raises(ConflictError):
        await service.archive(world.principal, _context("r2"), world.project_id, world.agent_id)

    assert len(await _events(owner_database, ARCHIVED)) == 1
    assert await _next_seq(owner_database, world.project_id) == after_first


async def test_a_member_may_not_archive_and_leaves_nothing_behind(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    with pytest.raises(PermissionDeniedError):
        await service.archive(world.member(), _context(), world.project_id, world.agent_id)

    assert await _events(owner_database, ARCHIVED) == []
    assert await _next_seq(owner_database, world.project_id) == baseline
    assert not await _archived(owner_database, world.agent_id)


# ------------------------------------------------------------------- restore ---


async def test_a_restore_writes_one_restored_event(
    service: AgentService, world: World, owner_database: Database
) -> None:
    await service.archive(world.principal, _context("r1"), world.project_id, world.agent_id)
    before_restore = await _next_seq(owner_database, world.project_id)

    await service.restore(world.principal, _context("r2"), world.project_id, world.agent_id)

    rows = await _events(owner_database, RESTORED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=RESTORED,
        project_id=world.project_id,
        agent_id=world.agent_id,
        seq=before_restore,
    )
    assert not await _archived(owner_database, world.agent_id)


async def test_restoring_a_live_agent_spends_no_number_though_it_takes_the_lock(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    """The one operation whose refusal is reached *after* the cursor lock.

    `agents.restore` is both the existence check and the mutation, so there is
    no earlier read to refuse against and the reservation necessarily precedes
    the 404. P13 accepts the brief lock; what must still hold is that the
    rollback leaves no event **and no consumed number** - which is the whole
    reason `next_seq` advances a table row rather than a sequence object.
    """
    with pytest.raises(NotFoundError):
        await service.restore(world.principal, _context(), world.project_id, world.agent_id)

    assert await _events(owner_database, RESTORED) == []
    assert await _next_seq(owner_database, world.project_id) == baseline
    assert await _audit_count(owner_database, "agent.restore") == 0


# ------------------------------------------------------- ordering and atomicity ---


async def test_the_lifecycle_writes_one_ordered_gapless_run_of_events(
    service: AgentService, world: World, owner_database: Database, baseline: int
) -> None:
    """Create, edit, archive, restore: four events, consecutive and in order.

    This is the property `resume` depends on - a client replaying from a cursor
    must see the lifecycle in the order it happened, with no gap - and it is
    asserted across all four producers at once because a per-operation test
    cannot see an ordering bug between them.

    The world's own agent contributes the first `created`, which is why the run
    starts at `baseline` rather than at 1.
    """
    agent = await service.create(
        world.principal,
        _context("r1"),
        world.project_id,
        display_name="Analyst",
        interaction_mode=AgentInteractionMode.MANAGED,
    )
    await service.update(
        world.principal, _context("r2"), world.project_id, agent.id, display_name="Senior Analyst"
    )
    await service.archive(world.principal, _context("r3"), world.project_id, agent.id)
    await service.restore(world.principal, _context("r4"), world.project_id, agent.id)

    log = await _definition_log(owner_database, world.project_id)
    # The world's `created` sits at baseline - 1; the four below follow it.
    assert log[-4:] == [
        (baseline, CREATED),
        (baseline + 1, UPDATED),
        (baseline + 2, ARCHIVED),
        (baseline + 3, RESTORED),
    ]


async def test_a_failing_append_rolls_back_the_agent_mutation(
    service: AgentService,
    world: World,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that would otherwise be silent: row written, event lost.

    Without one transaction this is the dangerous failure - the agent is
    changed, nothing records it, and `resume` can never replay a delta that was
    never written.
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
    assert await _events(owner_database, UPDATED) == []
    assert await _audit_count(owner_database, "agent.update") == 0


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
    assert await _events(owner_database, UPDATED) == []


async def test_a_rolled_back_update_leaves_no_gap_in_the_sequence(
    service: AgentService,
    world: World,
    owner_database: Database,
    baseline: int,
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
    would break, and two consecutive numbers are the only answer that shows it.
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

    assert [row["seq"] for row in await _events(owner_database, UPDATED)] == [
        baseline,
        baseline + 1,
    ]


async def test_concurrent_updates_in_one_project_get_distinct_ordered_numbers(
    service: AgentService,
    world: World,
    client: TestClient,
    owner_database: Database,
) -> None:
    """Two transactions, one cursor, no duplicates.

    Two agents rather than one, so the test is about the project's cursor
    rather than about row contention on a single agent. Each call opens its own
    transaction through the factory, so the serialisation being demonstrated is
    the cursor's `FOR UPDATE` and nothing else.

    The second agent is created over HTTP *before* the baseline is read, so its
    own `created` event is already accounted for.
    """
    second = world.add_agent(client, "Analyst")
    start = await _next_seq(owner_database, world.project_id)

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

    rows = await _events(owner_database, UPDATED)
    assert [row["seq"] for row in rows] == [start, start + 1]
    assert len({row["dedup_key"] for row in rows}) == 2


# --------------------------------------------------------------- lock order ---


def _statement_recorder(statements: list[str]) -> Any:
    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        statements.append(" ".join(statement.split()))

    return record


def _first(statements: list[str], predicate: Any, what: str) -> int:
    for index, statement in enumerate(statements):
        if predicate(statement):
            return index
    raise AssertionError(f"no statement {what} in:\n" + "\n".join(statements))


async def test_the_cursor_lock_is_taken_before_the_agent_row_is_written(
    service: AgentService, world: World, database: Database
) -> None:
    """The ordering that keeps these writes out of the lock graph's cycles.

    `services/ingest.py` takes the project cursor and *then* folds events into
    `agents`. A definition write that did the opposite - wrote the agent row
    and then reserved its number - would be `agents -> cursor` against ingest's
    `cursor -> agents`, and an agent edit arriving while a heartbeat batch was
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
    record = _statement_recorder(statements)
    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        await service.update(
            world.principal, _context(), world.project_id, world.agent_id, display_name="Ordered"
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    cursor_lock = _first(
        statements,
        lambda s: "project_event_cursors" in s and "FOR UPDATE" in s,
        "locked the project cursor",
    )
    agent_write = _first(statements, lambda s: s.startswith("UPDATE agents"), "wrote the agent row")
    event_insert = _first(
        statements, lambda s: s.startswith("INSERT INTO events"), "inserted the event"
    )

    assert cursor_lock < agent_write, "the cursor must be locked before the agent row is written"
    assert agent_write < event_insert


async def test_the_cursor_lock_is_taken_before_the_agent_row_is_inserted(
    service: AgentService, world: World, database: Database
) -> None:
    """The same ordering for `create`, which is its own case.

    `create` is the operation whose reservation and append are separated by the
    write, because the id does not exist until the INSERT returns. That makes
    it the easiest one to "simplify" into reserving next to the append - which
    is the deadlock. This fails if anyone does.
    """
    statements: list[str] = []
    record = _statement_recorder(statements)
    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        await service.create(
            world.principal,
            _context(),
            world.project_id,
            display_name="Analyst",
            interaction_mode=AgentInteractionMode.MANAGED,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    cursor_lock = _first(
        statements,
        lambda s: "project_event_cursors" in s and "FOR UPDATE" in s,
        "locked the project cursor",
    )
    agent_insert = _first(
        statements, lambda s: s.startswith("INSERT INTO agents"), "inserted the agent row"
    )

    assert cursor_lock < agent_insert, "the cursor must be locked before the agent row is inserted"

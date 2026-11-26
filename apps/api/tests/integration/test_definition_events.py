"""Transactional definition events for the agent lifecycle. M3 and M4.

Every test here is about one claim: **the agent row and the event describing it
are written together or not at all**, and the number the event carries comes
from the shared per-project cursor (P9) allocated before the row is touched.

**All fourteen producers of P13's inventory.** `AgentService.create`,
`update`, `archive` and `restore`; `ConnectionService.create`, `update` and
`delete`; `WorkflowService.create`, `compile_delegation`, `update`,
`save_draft`, `archive` and `restore`; and `ExecutionService._publish`, the
only one outside `services/definitions.py`. `AgentService.update` came with
M3, the other thirteen with M4.

Thirteen of them emit unconditionally. The publish is the exception: it writes
no event when the workflow is a test harness, for the reason P13 section 5
gives about harnesses not appearing in any listing a client subscribes to.

**No assertion here hard-codes a sequence number.** It did until M4, on the
premise that setting a project up appended nothing - and wiring `create` made
that premise false, which is exactly the trap the `baseline` fixture exists to
close. Every expectation is relative to the cursor position captured *after*
setup, so the next producer wired into this file moves no numbers.

The properties that are *not* here are deliberate. That the eleven types are
`CONTROL_PLANE`, fold into nothing and cannot be forged with an ingest key is
M2's, pinned by `test_producer_authority.py`, `test_protocol_envelope.py` and
`test_reducers.py` - the first of those parametrised over all eleven, so the
seven wired here are covered there. Repeating it would make this file look
like the boundary when it is not.

These tests drive the services rather than the HTTP routes, because several
need a repository to fail mid-transaction and six need to watch the statements
go past. The routes have one caller each and no logic of their own, and
`test_agents_api.py` and `test_workflows_api.py` already cover them.

**No assertion counts every event in the project.** Three entities now write
definition events during ordinary setup, so a count that did would break on
the next producer; `_definition_log` and the per-type readers below are what
the lifecycle assertions use instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from agentworld.domain.enums import AgentInteractionMode, ConnectionKind, MemberRole
from agentworld.domain.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
)
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.audit import AuditLogRepository
from agentworld.repositories.events import EventRepository
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.definitions import (
    AgentService,
    ConnectionService,
    WorkflowService,
)
from agentworld.services.executions import ExecutionService
from helpers import Actor

pytestmark = pytest.mark.integration

CREATED = "definition.agent.created"
UPDATED = "definition.agent.updated"
ARCHIVED = "definition.agent.archived"
RESTORED = "definition.agent.restored"
CONNECTION_CREATED = "definition.connection.created"
CONNECTION_UPDATED = "definition.connection.updated"
CONNECTION_DELETED = "definition.connection.deleted"
WORKFLOW_CREATED = "definition.workflow.created"
WORKFLOW_UPDATED = "definition.workflow.updated"
WORKFLOW_ARCHIVED = "definition.workflow.archived"
WORKFLOW_RESTORED = "definition.workflow.restored"


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

    def viewer(self) -> UserPrincipal:
        """The same person at `viewer`, for the one refusal `member` cannot give.

        The policy grants `connection.delete` at `member` and declares no
        consequential variant of it, unlike create and update - so a member
        may delete any connection, and the refusal this file needs for the
        hard delete is one rank lower.
        """
        return UserPrincipal(
            user_id=self.actor.user_id,
            email=self.actor.email,
            memberships={self.workspace_id: MemberRole.VIEWER},
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
    row: dict[str, Any],
    *,
    event_type: str,
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    seq: int,
    entity: str = "agent",
) -> None:
    """Every field of the envelope, against the decision that fixed it.

    One helper rather than seven copies, because the shape is identical across
    both entities and the whole lifecycle - a difference between the types
    would be a bug rather than a feature. `entity` defaults to `agent` so the
    agent group's calls read as they did before connections arrived; the
    parameter named `agent_id` is the entity's id whichever entity it is.
    """
    # P11.1: the persisted name, *not* the `agent.updated` client frame - that
    # namespace has a reducer.
    assert row["type"] == event_type
    # P11.3: subject is the project; the entity travels in the payload, because
    # `SubjectKind.AGENT.subject_ref` is the agent_key rather than a uuid, and
    # `SubjectKind` has no member for a connection at all.
    assert row["subject_kind"] == "project"
    assert row["subject_ref"] == str(project_id)
    assert row["payload"] == {"entity": entity, "id": str(agent_id)}
    # P12.1: the key carries the allocated seq, so a second event about the
    # same entity in the same month cannot collide with this one.
    operation = event_type.rsplit(".", 1)[1]
    assert row["dedup_key"] == f"def:{entity}:{agent_id}:{operation}:{seq}"
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


def _locked_the_cursor(statement: str) -> bool:
    return "project_event_cursors" in statement and "FOR UPDATE" in statement


@contextlib.contextmanager
def _recording(database: Database) -> Iterator[list[str]]:
    """Every statement this engine issues while the block runs.

    The connection group asserts the *absence* of the cursor lock as well as
    its position, and absence is only assertable by watching: `_next_seq`
    cannot see it, because `next_seq` advances a row rather than a sequence
    object, so a rollback un-consumes the number wherever the reservation
    sat. The two agent lock tests predate this helper and stay inline.
    """
    statements: list[str] = []
    record = _statement_recorder(statements)
    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


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


# =============================================================== connections ===
#
# The second entity of P13's inventory. The shape is the agent group's, with
# one operation that has no counterpart there: `delete` is a **hard** delete,
# the only one among the eleven, and its event is the only one that has to mean
# something after the row it describes is gone.


@pytest_asyncio.fixture
async def connections(factory: UnitOfWorkFactory) -> ConnectionService:
    return ConnectionService(factory)


async def _make_connection(
    connections: ConnectionService, world: World, *, config: dict[str, Any] | None = None
) -> Any:
    """A tool grant from the world's agent. Non-consequential by default.

    `side_effect: read` keeps the grant inside `member` authority, so the
    elevation rule is not what any of these tests happens to be exercising.
    """
    return await connections.create(
        world.principal,
        _context(),
        world.project_id,
        kind=ConnectionKind.GRANT_TOOL,
        source_kind="agent",
        source_id=world.agent_id,
        target_kind="tool",
        target_id=uuid.uuid4(),
        config=config if config is not None else {"tool_name": "web_search", "side_effect": "read"},
    )


async def _connection_exists(database: Database, connection_id: uuid.UUID) -> bool:
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT 1 FROM connections WHERE id = :i"), {"i": connection_id}
            )
        ).scalar_one_or_none() is not None


async def test_a_connection_create_writes_one_created_event(
    connections: ConnectionService, world: World, owner_database: Database, baseline: int
) -> None:
    connection = await _make_connection(connections, world)

    rows = await _events(owner_database, CONNECTION_CREATED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=CONNECTION_CREATED,
        project_id=world.project_id,
        agent_id=connection.id,
        seq=baseline,
        entity="connection",
    )


async def test_the_connection_event_carries_no_config_only_identity(
    connections: ConnectionService, world: World, owner_database: Database
) -> None:
    """The payload rule matters more here than anywhere else in the inventory.

    A connection's `config` is the whole of what the grant permits, and for a
    `grant_tool` it can name an endpoint. An append-only log cannot be
    redacted, so a config that reached one would be there for good.

    The whole stored row is searched rather than only the payload, because
    `labels` and `correlation` would leak the same string just as well.
    """
    connection = await _make_connection(
        connections,
        world,
        config={
            "tool_name": "web_search",
            "side_effect": "read",
            "endpoint": "https://SENTINEL-HOST.example/hook",
            "token": "SENTINEL-TOKEN",
        },
    )

    row = (await _events(owner_database, CONNECTION_CREATED))[0]
    assert row["payload"] == {"entity": "connection", "id": str(connection.id)}
    serialised = json.dumps(row, default=str)
    assert "SENTINEL-HOST" not in serialised
    assert "SENTINEL-TOKEN" not in serialised


async def test_a_connection_update_writes_one_updated_event(
    connections: ConnectionService, world: World, owner_database: Database
) -> None:
    connection = await _make_connection(connections, world)
    after_create = await _next_seq(owner_database, world.project_id)

    await connections.update(
        world.principal, _context("r2"), world.project_id, connection.id, enabled=False
    )

    rows = await _events(owner_database, CONNECTION_UPDATED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=CONNECTION_UPDATED,
        project_id=world.project_id,
        agent_id=connection.id,
        seq=after_create,
        entity="connection",
    )


async def test_an_empty_connection_update_appends_no_event_and_spends_no_number(
    connections: ConnectionService, world: World, owner_database: Database
) -> None:
    """P12.2 again, on the second entity.

    `ConnectionRepository.update` shares the agent repository's empty-values
    early return, so the guard is the same one and this pins that it was
    actually applied here rather than assumed to carry over.
    """
    connection = await _make_connection(connections, world)
    after_create = await _next_seq(owner_database, world.project_id)

    await connections.update(world.principal, _context("r2"), world.project_id, connection.id)

    assert await _events(owner_database, CONNECTION_UPDATED) == []
    assert await _next_seq(owner_database, world.project_id) == after_create


async def test_updating_an_unknown_connection_spends_no_number(
    connections: ConnectionService,
    world: World,
    owner_database: Database,
    database: Database,
    baseline: int,
) -> None:
    """The 404 comes from a read, so it is resolved before the cursor lock.

    Watched rather than inferred. The unchanged cursor below would hold even
    if the reservation came first, since the rollback would give the number
    back - so it is the absent `FOR UPDATE` that distinguishes this from the
    delete two tests down, which does reach the lock before it fails.
    """
    with _recording(database) as statements, pytest.raises(NotFoundError):
        await connections.update(
            world.principal, _context(), world.project_id, uuid.uuid4(), enabled=False
        )

    assert not any(_locked_the_cursor(s) for s in statements), (
        "a caller refused by the read must not have taken this project's cursor lock"
    )
    assert await _events(owner_database, CONNECTION_UPDATED) == []
    assert await _next_seq(owner_database, world.project_id) == baseline


async def test_a_connection_delete_writes_one_deleted_event(
    connections: ConnectionService, world: World, owner_database: Database
) -> None:
    connection = await _make_connection(connections, world)
    after_create = await _next_seq(owner_database, world.project_id)

    await connections.delete(world.principal, _context("r2"), world.project_id, connection.id)

    rows = await _events(owner_database, CONNECTION_DELETED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=CONNECTION_DELETED,
        project_id=world.project_id,
        agent_id=connection.id,
        seq=after_create,
        entity="connection",
    )


async def test_the_delete_event_outlives_the_connection_row(
    connections: ConnectionService, world: World, owner_database: Database
) -> None:
    """The one event that must stand on its own, asserted as such.

    Every other definition event describes a row a publisher can read back at
    publish time. This one does not: the connection is gone. So the event has
    to be replayable by itself - which the identity makes it - and that is also
    why it must carry no more than the identity, since there would be nowhere
    to redact anything from.

    What makes that work is that the id travels in the payload: the resolved
    reference columns are NULL for every definition event (P12.3), so this
    row would survive the delete even if `events` did declare the foreign
    keys it deliberately omits. The absent keys are what let an `agent_id`
    outlive its agent; they are not what this test exercises.
    """
    connection = await _make_connection(connections, world)
    await connections.delete(world.principal, _context("r2"), world.project_id, connection.id)

    assert not await _connection_exists(owner_database, connection.id)

    row = (await _events(owner_database, CONNECTION_DELETED))[0]
    assert row["payload"] == {"entity": "connection", "id": str(connection.id)}
    assert row["type"] == CONNECTION_DELETED
    assert row["subject_ref"] == str(world.project_id)
    # Replayable: the id in the payload still names the connection that went,
    # and nothing about the row depends on that connection still existing.
    assert uuid.UUID(row["payload"]["id"]) == connection.id


async def test_deleting_an_unknown_connection_spends_no_number(
    connections: ConnectionService,
    world: World,
    owner_database: Database,
    database: Database,
    baseline: int,
) -> None:
    """`connections.delete` is both the check and the mutation, like
    `AgentService.restore` - so this 404 reaches the cursor lock before it
    fails, and what must hold is that the rollback leaves no trace.

    The cost is asserted rather than left in a comment: the lock *is* taken
    for a connection that does not exist. That is the trade-off P13 accepted
    instead of a read-then-write precheck, and a test that says so is what
    stops it being re-discovered later as a surprise.
    """
    with _recording(database) as statements, pytest.raises(NotFoundError):
        await connections.delete(world.principal, _context(), world.project_id, uuid.uuid4())

    assert any(_locked_the_cursor(s) for s in statements), (
        "the 404 is the DELETE itself, so the cursor lock is taken before it"
    )
    assert await _events(owner_database, CONNECTION_DELETED) == []
    assert await _next_seq(owner_database, world.project_id) == baseline
    assert await _audit_count(owner_database, "connection.delete") == 0


async def test_a_member_may_not_edit_a_consequential_grant_and_takes_no_lock(
    connections: ConnectionService, world: World, owner_database: Database, database: Database
) -> None:
    """The refusal the second authorization exists for, and its lock order.

    An owner grants a consequential tool; a `member` then tries to switch it
    off. `_elevation_for` is applied to the *stored* config as well as the
    sent one, so the bar is `admin` even though this edit sends nothing
    consequential - and `ConnectionService.update` claims such a caller never
    takes this project's cursor lock. Watching the statements is what makes
    that an assertion.
    """
    connection = await _make_connection(
        connections,
        world,
        config={"tool_name": "send_slack_message", "side_effect": "consequential"},
    )
    after_create = await _next_seq(owner_database, world.project_id)

    with _recording(database) as statements, pytest.raises(PermissionDeniedError):
        await connections.update(
            world.member(), _context("r2"), world.project_id, connection.id, enabled=False
        )

    assert not any(_locked_the_cursor(s) for s in statements), (
        "a caller refused by the stored-grant check must not have taken the cursor lock"
    )
    assert await _events(owner_database, CONNECTION_UPDATED) == []
    assert await _audit_count(owner_database, "connection.update") == 0
    assert await _next_seq(owner_database, world.project_id) == after_create


async def test_a_viewer_may_not_delete_a_connection_and_takes_no_lock(
    connections: ConnectionService, world: World, owner_database: Database, database: Database
) -> None:
    """The delete refusal, which is an authorization one and so comes earlier.

    `authorize` runs before the scope is even built, so unlike the 404 above
    this refusal costs no lock at all - the two together say that *only* the
    not-found case pays for the missing precheck.
    """
    connection = await _make_connection(connections, world)
    after_create = await _next_seq(owner_database, world.project_id)

    with _recording(database) as statements, pytest.raises(PermissionDeniedError):
        await connections.delete(world.viewer(), _context("r2"), world.project_id, connection.id)

    assert not any(_locked_the_cursor(s) for s in statements)
    assert await _connection_exists(owner_database, connection.id)
    assert await _events(owner_database, CONNECTION_DELETED) == []
    assert await _audit_count(owner_database, "connection.delete") == 0
    assert await _next_seq(owner_database, world.project_id) == after_create


async def test_a_failing_append_rolls_back_the_connection_delete(
    connections: ConnectionService,
    world: World,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst available failure, so it gets its own test.

    If the delete committed and its event did not, a connection would be gone
    with nothing anywhere recording that it ever went - no row to read, no
    event to replay, and a grant silently removed from every client that still
    believes it is there. The transaction is what prevents it.
    """
    connection = await _make_connection(connections, world)
    after_create = await _next_seq(owner_database, world.project_id)

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the append failed")

    monkeypatch.setattr(EventRepository, "append", explode)

    with pytest.raises(RuntimeError):
        await connections.delete(world.principal, _context("r2"), world.project_id, connection.id)

    assert await _connection_exists(owner_database, connection.id)
    assert await _events(owner_database, CONNECTION_DELETED) == []
    assert await _next_seq(owner_database, world.project_id) == after_create
    assert await _audit_count(owner_database, "connection.delete") == 0


async def test_the_connection_lifecycle_writes_one_ordered_gapless_run(
    connections: ConnectionService, world: World, owner_database: Database, baseline: int
) -> None:
    """Create, edit, delete: three events, consecutive and in order.

    Asserted across the three producers at once, because a per-operation test
    cannot see an ordering bug between them - and ordering is the whole of what
    `resume` relies on.
    """
    connection = await _make_connection(connections, world)
    await connections.update(
        world.principal, _context("r2"), world.project_id, connection.id, enabled=False
    )
    await connections.delete(world.principal, _context("r3"), world.project_id, connection.id)

    log = await _definition_log(owner_database, world.project_id)
    assert log[-3:] == [
        (baseline, CONNECTION_CREATED),
        (baseline + 1, CONNECTION_UPDATED),
        (baseline + 2, CONNECTION_DELETED),
    ]


async def test_the_cursor_lock_is_taken_before_the_connection_row_is_deleted(
    connections: ConnectionService, world: World, database: Database
) -> None:
    """Lock order for the hard delete.

    A DELETE takes `FOR UPDATE` strength on the row where an UPDATE of
    non-key columns takes only `FOR NO KEY UPDATE`, so this is the strongest
    row lock of the seven producers. Nothing references `connections.id` -
    the edge is polymorphic, by migration 0007 - so there is no child insert
    for it to conflict with *today*, and the reason to reserve the cursor
    first is uniformity with the other six rather than an observed conflict:
    the same side of the lock graph as ingest, whatever later points here.
    """
    connection = await _make_connection(connections, world)

    with _recording(database) as statements:
        await connections.delete(world.principal, _context("r2"), world.project_id, connection.id)

    cursor_lock = _first(statements, _locked_the_cursor, "locked the project cursor")
    deletion = _first(
        statements, lambda s: s.startswith("DELETE FROM connections"), "deleted the connection"
    )
    assert cursor_lock < deletion, "the cursor must be locked before the connection row is deleted"


# ================================================================= workflows ===
#
# The third and last entity of P13's inventory inside `services/definitions.py`.
# Six producers, more than either group before it, and two shapes neither had:
# `create` writes two rows for one event, and `save_draft` writes a row in a
# different table from the one the event names.
#
# `ExecutionService._publish` is P13's fourteenth producer; it emits the same
# `definition.workflow.updated` and has its own section at the end of this file.


@pytest_asyncio.fixture
async def workflows(factory: UnitOfWorkFactory) -> WorkflowService:
    return WorkflowService(factory)


def _graph(step_key: str = "research") -> dict[str, Any]:
    return {"steps": [{"step_key": step_key, "kind": "agent_task", "agent_key": "researcher"}]}


async def _make_workflow(
    workflows: WorkflowService,
    world: World,
    *,
    name: str = "Nightly digest",
    description: str | None = None,
    graph: dict[str, Any] | None = None,
) -> Any:
    workflow, _ = await workflows.create(
        world.principal,
        _context(),
        world.project_id,
        name=name,
        description=description,
        graph=graph if graph is not None else _graph(),
    )
    return workflow


async def _workflow_exists(database: Database, *, slug: str) -> bool:
    async with database.session() as session:
        return (
            await session.execute(text("SELECT 1 FROM workflows WHERE slug = :s"), {"s": slug})
        ).scalar_one_or_none() is not None


async def _draft_count(database: Database, workflow_id: uuid.UUID) -> int:
    async with database.session() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM workflow_versions WHERE workflow_id = :w"),
                    {"w": workflow_id},
                )
            ).scalar_one()
        )


async def test_a_workflow_create_writes_one_created_event(
    workflows: WorkflowService, world: World, owner_database: Database, baseline: int
) -> None:
    workflow = await _make_workflow(workflows, world)

    rows = await _events(owner_database, WORKFLOW_CREATED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=WORKFLOW_CREATED,
        project_id=world.project_id,
        agent_id=workflow.id,
        seq=baseline,
        entity="workflow",
    )


async def test_creating_a_workflow_writes_one_event_for_two_rows(
    workflows: WorkflowService, world: World, owner_database: Database
) -> None:
    """P13's "one logical operation, one event", where it actually bites.

    `WorkflowRepository.create` writes the `workflows` row *and* its first
    `workflow_versions` draft, because a workflow without a draft is not a
    thing this application can produce. A per-row rule would emit two events
    and make a client fetch twice for one change; the rule is per operation,
    so this asserts both rows exist and exactly one event describes them.
    """
    workflow = await _make_workflow(workflows, world)

    assert await _draft_count(owner_database, workflow.id) == 1
    assert len(await _events(owner_database, WORKFLOW_CREATED)) == 1


async def test_the_workflow_event_carries_no_graph_only_identity(
    workflows: WorkflowService, world: World, owner_database: Database
) -> None:
    """A workflow's graph is the largest thing any of the eleven could leak.

    It names steps, agents and expressions, and a description is free text a
    person wrote. An append-only log cannot be redacted, so the payload is the
    identity and the operation and nothing else. The whole stored row is
    searched, not just the payload, because `labels` and `correlation` would
    carry the same string out just as well.
    """
    workflow = await _make_workflow(
        workflows,
        world,
        description="SENTINEL-DESCRIPTION",
        graph=_graph("SENTINEL-STEP"),
    )

    row = (await _events(owner_database, WORKFLOW_CREATED))[0]
    assert row["payload"] == {"entity": "workflow", "id": str(workflow.id)}
    serialised = json.dumps(row, default=str)
    assert "SENTINEL-DESCRIPTION" not in serialised
    assert "SENTINEL-STEP" not in serialised


async def test_a_compiled_delegation_writes_one_created_event(
    workflows: WorkflowService,
    connections: ConnectionService,
    world: World,
    client: TestClient,
    owner_database: Database,
) -> None:
    """P13's one approved producer with no production caller, wired anyway.

    `compile_delegation` ends where `create` ends - a workflow and its first
    draft - so it is a definition write like any other, and a path that wrote
    a workflow without announcing it would be a hole in `resume` the day a
    caller appears. The number is captured after the delegate connection is
    made, because creating that connection is itself a definition change now.
    """
    writer = world.add_agent(client, "Writer")
    connection = await connections.create(
        world.principal,
        _context(),
        world.project_id,
        kind=ConnectionKind.DELEGATE,
        source_kind="agent",
        source_id=world.agent_id,
        target_kind="agent",
        target_id=uuid.UUID(writer["id"]),
        config=None,
    )
    after_setup = await _next_seq(owner_database, world.project_id)

    workflow, draft = await workflows.compile_delegation(
        world.principal, _context("r2"), world.project_id, connection.id
    )

    assert draft.version == 1
    rows = await _events(owner_database, WORKFLOW_CREATED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=WORKFLOW_CREATED,
        project_id=world.project_id,
        agent_id=workflow.id,
        seq=after_setup,
        entity="workflow",
    )


async def test_a_workflow_update_writes_one_updated_event(
    workflows: WorkflowService, world: World, owner_database: Database
) -> None:
    workflow = await _make_workflow(workflows, world)
    after_create = await _next_seq(owner_database, world.project_id)

    await workflows.update(
        world.principal, _context("r2"), world.project_id, workflow.id, name="Renamed"
    )

    rows = await _events(owner_database, WORKFLOW_UPDATED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=WORKFLOW_UPDATED,
        project_id=world.project_id,
        agent_id=workflow.id,
        seq=after_create,
        entity="workflow",
    )


async def test_an_empty_workflow_update_appends_no_event_and_spends_no_number(
    workflows: WorkflowService, world: World, owner_database: Database
) -> None:
    """P12.2 on the third entity, and the only one of these six that needs it.

    `WorkflowRepository.update` has the same empty-values early return as the
    agent's and the connection's, so an empty request writes no row. The other
    five producers always write when they reach their mutation, which is why
    only this one carries the guard.
    """
    workflow = await _make_workflow(workflows, world)
    after_create = await _next_seq(owner_database, world.project_id)

    await workflows.update(world.principal, _context("r2"), world.project_id, workflow.id)

    assert await _events(owner_database, WORKFLOW_UPDATED) == []
    assert await _next_seq(owner_database, world.project_id) == after_create


async def test_a_draft_save_writes_one_updated_event_naming_the_workflow(
    workflows: WorkflowService, world: World, owner_database: Database
) -> None:
    """The event names the workflow, though the row written was the draft's.

    `save_draft` updates `workflow_versions`, not `workflows`. P11.3 fixes the
    payload as the entity a client holds, and a client holds workflows - the
    draft is reached through one. So the payload carries the workflow id, and
    this test exists because the draft's id is the one closest to hand in that
    method and would be the easy mistake.
    """
    workflow = await _make_workflow(workflows, world)
    after_create = await _next_seq(owner_database, world.project_id)

    draft = await workflows.save_draft(
        world.principal, _context("r2"), world.project_id, workflow.id, graph=_graph("rewritten")
    )

    rows = await _events(owner_database, WORKFLOW_UPDATED)
    assert len(rows) == 1
    assert rows[0]["payload"] == {"entity": "workflow", "id": str(workflow.id)}
    assert str(draft.id) not in json.dumps(rows[0], default=str)
    _assert_shape(
        rows[0],
        event_type=WORKFLOW_UPDATED,
        project_id=world.project_id,
        agent_id=workflow.id,
        seq=after_create,
        entity="workflow",
    )


async def test_an_edit_and_a_draft_save_write_two_distinct_updated_events(
    workflows: WorkflowService, world: World, owner_database: Database
) -> None:
    """Two producers share one event type, so their keys must still differ.

    P13 gives `update` and `save_draft` the same `definition.workflow.updated`,
    and P12.1 puts the allocated seq in the `dedup_key` - which is what keeps
    two changes to the same workflow in the same month from colliding on the
    partition's unique index. This is the case that convention exists for.
    """
    workflow = await _make_workflow(workflows, world)
    after_create = await _next_seq(owner_database, world.project_id)

    await workflows.update(
        world.principal, _context("r2"), world.project_id, workflow.id, name="Renamed"
    )
    await workflows.save_draft(
        world.principal, _context("r3"), world.project_id, workflow.id, graph=_graph("rewritten")
    )

    rows = await _events(owner_database, WORKFLOW_UPDATED)
    assert [row["seq"] for row in rows] == [after_create, after_create + 1]
    assert len({row["dedup_key"] for row in rows}) == 2


async def test_a_workflow_archive_writes_one_archived_event(
    workflows: WorkflowService, world: World, owner_database: Database
) -> None:
    workflow = await _make_workflow(workflows, world)
    after_create = await _next_seq(owner_database, world.project_id)

    await workflows.archive(world.principal, _context("r2"), world.project_id, workflow.id)

    rows = await _events(owner_database, WORKFLOW_ARCHIVED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=WORKFLOW_ARCHIVED,
        project_id=world.project_id,
        agent_id=workflow.id,
        seq=after_create,
        entity="workflow",
    )


async def test_a_workflow_restore_writes_one_restored_event(
    workflows: WorkflowService, world: World, owner_database: Database
) -> None:
    workflow = await _make_workflow(workflows, world)
    await workflows.archive(world.principal, _context("r2"), world.project_id, workflow.id)
    after_archive = await _next_seq(owner_database, world.project_id)

    await workflows.restore(world.principal, _context("r3"), world.project_id, workflow.id)

    rows = await _events(owner_database, WORKFLOW_RESTORED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=WORKFLOW_RESTORED,
        project_id=world.project_id,
        agent_id=workflow.id,
        seq=after_archive,
        entity="workflow",
    )


async def test_restoring_a_live_workflow_takes_the_lock_and_spends_no_number(
    workflows: WorkflowService, world: World, owner_database: Database, database: Database
) -> None:
    """The refusal that the statement makes rather than a read.

    `WorkflowRepository.restore` carries `archived_at IS NOT NULL`, so for a
    live workflow the UPDATE *is* the refusal - there is no earlier read to
    turn into a 404. The lock is therefore taken and handed back on the
    rollback, exactly as for `AgentService.restore` and the connection delete.
    Asserted rather than left in a comment, because the unchanged cursor below
    would hold either way.
    """
    workflow = await _make_workflow(workflows, world)
    after_create = await _next_seq(owner_database, world.project_id)

    with _recording(database) as statements, pytest.raises(NotFoundError):
        await workflows.restore(world.principal, _context("r2"), world.project_id, workflow.id)

    assert any(_locked_the_cursor(s) for s in statements), (
        "the refusal is the UPDATE itself, so the cursor lock is taken before it"
    )
    assert await _events(owner_database, WORKFLOW_RESTORED) == []
    assert await _next_seq(owner_database, world.project_id) == after_create
    assert await _audit_count(owner_database, "workflow.restore") == 0


async def test_updating_an_unknown_workflow_takes_no_lock(
    workflows: WorkflowService, world: World, owner_database: Database, database: Database
) -> None:
    """The opposite case to the restore above: this 404 comes from a read."""
    with _recording(database) as statements, pytest.raises(NotFoundError):
        await workflows.update(
            world.principal, _context(), world.project_id, uuid.uuid4(), name="Nothing"
        )

    assert not any(_locked_the_cursor(s) for s in statements)
    assert await _events(owner_database, WORKFLOW_UPDATED) == []


async def test_a_viewer_may_not_rename_a_workflow_and_takes_no_lock(
    workflows: WorkflowService, world: World, owner_database: Database, database: Database
) -> None:
    """`workflow.update` is granted at `member`, so the refusal is a viewer's.

    Read from `policy.py` rather than assumed: the three write verbs here sit
    at two different ranks, and using the wrong principal would make a
    refusal test pass for the wrong reason.
    """
    workflow = await _make_workflow(workflows, world)
    after_create = await _next_seq(owner_database, world.project_id)

    with _recording(database) as statements, pytest.raises(PermissionDeniedError):
        await workflows.update(
            world.viewer(), _context("r2"), world.project_id, workflow.id, name="Renamed"
        )

    assert not any(_locked_the_cursor(s) for s in statements)
    assert await _events(owner_database, WORKFLOW_UPDATED) == []
    assert await _audit_count(owner_database, "workflow.update") == 0
    assert await _next_seq(owner_database, world.project_id) == after_create


async def test_a_member_may_not_archive_a_workflow_and_takes_no_lock(
    workflows: WorkflowService, world: World, owner_database: Database, database: Database
) -> None:
    """Archiving is `admin`, unlike renaming - so here `member` is refused."""
    workflow = await _make_workflow(workflows, world)
    after_create = await _next_seq(owner_database, world.project_id)

    with _recording(database) as statements, pytest.raises(PermissionDeniedError):
        await workflows.archive(world.member(), _context("r2"), world.project_id, workflow.id)

    assert not any(_locked_the_cursor(s) for s in statements)
    assert await _events(owner_database, WORKFLOW_ARCHIVED) == []
    assert await _audit_count(owner_database, "workflow.archive") == 0
    assert await _next_seq(owner_database, world.project_id) == after_create


async def test_a_failing_append_rolls_back_the_workflow_and_its_first_draft(
    workflows: WorkflowService,
    world: World,
    owner_database: Database,
    baseline: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both rows go back, not just the one the event names.

    `create` writes the workflow and its draft, so "together or not at all"
    has to cover the pair. If the draft survived a failed append there would
    be a `workflow_versions` row belonging to no workflow - which the schema
    would refuse anyway, making this the test that says the transaction, not
    the foreign key, is what the guarantee rests on.
    """

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the append failed")

    monkeypatch.setattr(EventRepository, "append", explode)

    with pytest.raises(RuntimeError):
        await _make_workflow(workflows, world, name="Nightly digest")

    assert not await _workflow_exists(owner_database, slug="nightly-digest")
    assert await _events(owner_database, WORKFLOW_CREATED) == []
    assert await _audit_count(owner_database, "workflow.create") == 0
    assert await _next_seq(owner_database, world.project_id) == baseline


async def test_the_workflow_lifecycle_writes_one_ordered_gapless_run(
    workflows: WorkflowService, world: World, owner_database: Database, baseline: int
) -> None:
    """All five verbs on one workflow: consecutive, in order, one each.

    Asserted across the producers at once because a per-operation test cannot
    see an ordering bug between them, and ordering is the whole of what
    `resume` relies on. The draft save sits between the edit and the archive
    deliberately: it is the one that writes to another table, and a seq
    allocated out of step would show up here as a swap.
    """
    workflow = await _make_workflow(workflows, world)
    await workflows.update(
        world.principal, _context("r2"), world.project_id, workflow.id, name="Renamed"
    )
    await workflows.save_draft(
        world.principal, _context("r3"), world.project_id, workflow.id, graph=_graph("rewritten")
    )
    await workflows.archive(world.principal, _context("r4"), world.project_id, workflow.id)
    await workflows.restore(world.principal, _context("r5"), world.project_id, workflow.id)

    log = await _definition_log(owner_database, world.project_id)
    assert log[-5:] == [
        (baseline, WORKFLOW_CREATED),
        (baseline + 1, WORKFLOW_UPDATED),
        (baseline + 2, WORKFLOW_UPDATED),
        (baseline + 3, WORKFLOW_ARCHIVED),
        (baseline + 4, WORKFLOW_RESTORED),
    ]


async def test_the_cursor_lock_is_taken_before_the_draft_row_is_written(
    workflows: WorkflowService, world: World, database: Database
) -> None:
    """Lock order for the producer that writes a different table.

    The other five touch `workflows`; `save_draft` touches
    `workflow_versions`. Nothing in the codebase takes an explicit row lock on
    either table, and every cursor-taking path - ingest, dispatch, the
    orchestrator, review - only reads them, so cursor-first adds no edge here
    any more than it did for agents. This pins the order that keeps it true.
    """
    workflow = await _make_workflow(workflows, world)

    with _recording(database) as statements:
        await workflows.save_draft(
            world.principal,
            _context("r2"),
            world.project_id,
            workflow.id,
            graph=_graph("rewritten"),
        )

    cursor_lock = _first(statements, _locked_the_cursor, "locked the project cursor")
    draft_write = _first(
        statements,
        lambda s: s.startswith("UPDATE workflow_versions"),
        "wrote the draft",
    )
    assert cursor_lock < draft_write, "the cursor must be locked before the draft is written"


# =================================================== the publish, P13's last ===
#
# `ExecutionService._publish` is the fourteenth producer and the only one
# outside `services/definitions.py`. It emits `definition.workflow.updated`
# because publishing *is* moving `workflows.current_version_id`, and it emits
# exactly one event for an act that writes steps, edges, `published_at`, the
# pointer and the next draft (P13 section 2).
#
# It is also the only producer with a condition on it: a harness is in no
# listing a client subscribes to. `agent.test` publishes one through
# `publish_test_harness`, conditionally - normally on an agent's first test
# only, since `current_version_id` being absent is the publish-first predicate,
# and again only if an earlier publish failed and left no pointer.


@pytest_asyncio.fixture
async def executions(factory: UnitOfWorkFactory) -> ExecutionService:
    return ExecutionService(factory)


def _publishable_graph(step_key: str = "research") -> dict[str, Any]:
    """Two steps and an edge, shaped like `test_executions_api`'s.

    The agent step names `researcher`, which is the key the world's own agent
    gets from the display name `Researcher` - a graph naming an agent that is
    not there is refused by validation rather than published.
    """
    return {
        "steps": [
            {"step_key": step_key, "kind": "agent_task", "agent_key": "researcher"},
            {
                "step_key": "shape",
                "kind": "transform",
                "input_mapping": {"found": f"$.steps.{step_key}.output.count"},
            },
        ],
        "edges": [{"from": step_key, "to": "shape"}],
    }


async def _current_version(database: Database, workflow_id: uuid.UUID) -> uuid.UUID | None:
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT current_version_id FROM workflows WHERE id = :w"),
                {"w": workflow_id},
            )
        ).scalar_one_or_none()


async def _step_count(database: Database, workflow_id: uuid.UUID) -> int:
    async with database.session() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM workflow_steps s "
                        "JOIN workflow_versions v ON v.id = s.workflow_version_id "
                        "WHERE v.workflow_id = :w"
                    ),
                    {"w": workflow_id},
                )
            ).scalar_one()
        )


async def _make_harness(database: Database, workflow_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Mark an existing workflow as an agent's test harness.

    Written as the owner rather than through `agent.test`, which would need a
    model profile, a credential and a compiled harness graph to reach the same
    one-column state. `workflows_test_for_agent_key` is partial on
    `test_for_agent_id`, so one harness per agent is all this can make.
    """
    async with database.session() as session:
        await session.execute(
            text("UPDATE workflows SET test_for_agent_id = :a WHERE id = :w"),
            {"a": agent_id, "w": workflow_id},
        )
        await session.commit()


async def test_publishing_a_workflow_writes_one_updated_event(
    workflows: WorkflowService,
    executions: ExecutionService,
    world: World,
    owner_database: Database,
) -> None:
    """One event for an act that writes five things (P13 section 2).

    `publish` inserts the steps and the edges, sets `published_at`, moves
    `workflows.current_version_id` and opens the next draft. A client
    experiences that once, so counting the internal writes would publish
    several deltas and spend several cursor positions on one change.
    """
    workflow = await _make_workflow(workflows, world, graph=_publishable_graph())
    after_create = await _next_seq(owner_database, world.project_id)

    version_id, published_at = await executions.publish(
        world.principal, _context("r2"), world.project_id, workflow.id
    )

    assert published_at is not None
    assert await _current_version(owner_database, workflow.id) == version_id

    rows = await _events(owner_database, WORKFLOW_UPDATED)
    assert len(rows) == 1
    _assert_shape(
        rows[0],
        event_type=WORKFLOW_UPDATED,
        project_id=world.project_id,
        agent_id=workflow.id,
        seq=after_create,
        entity="workflow",
    )


async def test_the_publish_event_carries_no_graph_and_not_the_version(
    workflows: WorkflowService,
    executions: ExecutionService,
    world: World,
    owner_database: Database,
) -> None:
    """P13 section 4: no snapshot, no graph, no step contents.

    A publish is the moment a graph becomes live, so it is the producer most
    tempting to attach one to. The version id is checked too: it is the one
    identifier `publish` returns and would be the natural thing to smuggle in,
    and the payload rule is the entity a client holds - the workflow.
    """
    workflow = await _make_workflow(workflows, world, graph=_publishable_graph("SENTINEL-STEP"))

    version_id, _ = await executions.publish(
        world.principal, _context("r2"), world.project_id, workflow.id
    )

    row = (await _events(owner_database, WORKFLOW_UPDATED))[0]
    assert row["payload"] == {"entity": "workflow", "id": str(workflow.id)}
    serialised = json.dumps(row, default=str)
    assert "SENTINEL-STEP" not in serialised
    assert str(version_id) not in serialised


async def test_an_unpublishable_draft_takes_no_lock(
    workflows: WorkflowService,
    executions: ExecutionService,
    world: World,
    owner_database: Database,
    database: Database,
) -> None:
    """Validation refuses before the cursor, so a bad draft costs no lock.

    The graph names an agent that does not exist, which `_validated` turns
    into a refusal - and every refusal in `_publish` is resolved above the
    reservation, which is what this asserts rather than assumes.
    """
    workflow = await _make_workflow(
        workflows,
        world,
        graph={
            "steps": [{"step_key": "a", "kind": "agent_task", "agent_key": "nobody-at-all"}],
            "edges": [],
        },
    )
    after_create = await _next_seq(owner_database, world.project_id)

    with _recording(database) as statements, pytest.raises(DomainError):
        await executions.publish(world.principal, _context("r2"), world.project_id, workflow.id)

    assert not any(_locked_the_cursor(s) for s in statements), (
        "a draft refused by validation must not have taken this project's lock"
    )
    assert await _events(owner_database, WORKFLOW_UPDATED) == []
    assert await _next_seq(owner_database, world.project_id) == after_create
    assert await _current_version(owner_database, workflow.id) is None


async def test_a_viewer_may_not_publish_and_takes_no_lock(
    workflows: WorkflowService,
    executions: ExecutionService,
    world: World,
    owner_database: Database,
    database: Database,
) -> None:
    """`workflow.publish` is granted at `member`, so the refusal is a viewer's."""
    workflow = await _make_workflow(workflows, world, graph=_publishable_graph())
    after_create = await _next_seq(owner_database, world.project_id)

    with _recording(database) as statements, pytest.raises(PermissionDeniedError):
        await executions.publish(world.viewer(), _context("r2"), world.project_id, workflow.id)

    assert not any(_locked_the_cursor(s) for s in statements)
    assert await _events(owner_database, WORKFLOW_UPDATED) == []
    assert await _next_seq(owner_database, world.project_id) == after_create


async def test_a_failing_append_rolls_back_the_whole_publication(
    workflows: WorkflowService,
    executions: ExecutionService,
    world: World,
    owner_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything the publish wrote goes back, not only the pointer.

    P13 section 3: no publish may commit with its event unwritten. A publish
    that committed its frozen steps and lost its event would leave a version
    live that no client was ever told about - and `resume` would never replay
    it, because nothing recorded it.
    """
    workflow = await _make_workflow(workflows, world, graph=_publishable_graph())
    after_create = await _next_seq(owner_database, world.project_id)

    async def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the append failed")

    monkeypatch.setattr(EventRepository, "append", explode)

    with pytest.raises(RuntimeError):
        await executions.publish(world.principal, _context("r2"), world.project_id, workflow.id)

    assert await _current_version(owner_database, workflow.id) is None
    assert await _step_count(owner_database, workflow.id) == 0
    assert await _draft_count(owner_database, workflow.id) == 1
    assert await _events(owner_database, WORKFLOW_UPDATED) == []
    assert await _audit_count(owner_database, "workflow.publish") == 0
    assert await _next_seq(owner_database, world.project_id) == after_create


async def test_publishing_a_harness_writes_no_event_and_takes_no_lock(
    workflows: WorkflowService,
    executions: ExecutionService,
    world: World,
    owner_database: Database,
    database: Database,
) -> None:
    """The one producer with a condition on it, and the reason.

    P13 section 5 excludes the harness writer because
    `WorkflowRepository.list_all` filters `test_for_agent_id IS NULL`: a
    harness is in no listing a client subscribes to, so a delta for one
    describes a change to a collection nobody holds. P13 argues that about
    creating a harness and is silent on publishing one, which `agent.test`
    does conditionally - on an agent's first test, when the harness has no
    `current_version_id`, and again only if an earlier publish failed. This
    test fixes the reading that the exclusion follows the entity rather than
    the entry point.

    The publication itself must still happen: `agent.test` depends on it.
    """
    workflow = await _make_workflow(workflows, world, graph=_publishable_graph())
    await _make_harness(owner_database, workflow.id, world.agent_id)
    after_create = await _next_seq(owner_database, world.project_id)

    with _recording(database) as statements:
        version_id, _ = await executions.publish_test_harness(
            world.principal, _context("r2"), world.project_id, workflow.id
        )

    # It published.
    assert await _current_version(owner_database, workflow.id) == version_id
    assert await _step_count(owner_database, workflow.id) == 2
    # And announced nothing, having taken no number and no lock.
    assert await _events(owner_database, WORKFLOW_UPDATED) == []
    assert await _next_seq(owner_database, world.project_id) == after_create
    assert not any(_locked_the_cursor(s) for s in statements)
    # The audit row is still written: the exclusion is about the realtime
    # stream, not about whether anybody can tell what happened.
    assert await _audit_count(owner_database, "workflow.publish") == 1


async def test_edit_draft_and_publish_write_three_distinct_updated_events(
    workflows: WorkflowService,
    executions: ExecutionService,
    world: World,
    owner_database: Database,
    baseline: int,
) -> None:
    """Three producers, one event type, one workflow, consecutive numbers.

    `update`, `save_draft` and now the publish all emit
    `definition.workflow.updated`. P12.1 puts the allocated seq in the
    `dedup_key`, which is the whole reason three changes to one workflow in
    one month do not collide on the partition's unique index.
    """
    workflow = await _make_workflow(workflows, world, graph=_publishable_graph())
    await workflows.update(
        world.principal, _context("r2"), world.project_id, workflow.id, name="Renamed"
    )
    await workflows.save_draft(
        world.principal,
        _context("r3"),
        world.project_id,
        workflow.id,
        graph=_publishable_graph("rewritten"),
    )
    await executions.publish(world.principal, _context("r4"), world.project_id, workflow.id)

    log = await _definition_log(owner_database, world.project_id)
    assert log[-4:] == [
        (baseline, WORKFLOW_CREATED),
        (baseline + 1, WORKFLOW_UPDATED),
        (baseline + 2, WORKFLOW_UPDATED),
        (baseline + 3, WORKFLOW_UPDATED),
    ]
    rows = await _events(owner_database, WORKFLOW_UPDATED)
    assert len({row["dedup_key"] for row in rows}) == 3


async def test_the_cursor_lock_is_taken_before_the_first_step_row_is_frozen(
    workflows: WorkflowService,
    executions: ExecutionService,
    world: World,
    database: Database,
) -> None:
    """Lock order for the producer with the most writes behind it.

    The publish inserts steps and edges, updates `workflow_versions` and
    updates `workflows` - four statements the cursor must precede. Neither
    `current_version_id` nor `published_at` is an indexed column of any unique
    index on its table, so both updates take `FOR NO KEY UPDATE`, which does
    not conflict with the `FOR KEY SHARE` that starting a run holds on the
    workflow row before it takes the cursor. That is why cursor-first adds no
    cycle here even though a run reaches these tables in the other order.
    """
    workflow = await _make_workflow(workflows, world, graph=_publishable_graph())

    with _recording(database) as statements:
        await executions.publish(world.principal, _context("r2"), world.project_id, workflow.id)

    cursor_lock = _first(statements, _locked_the_cursor, "locked the project cursor")
    first_freeze = _first(
        statements,
        lambda s: s.startswith("INSERT INTO workflow_steps"),
        "froze the first step",
    )
    assert cursor_lock < first_freeze, "the cursor must be locked before the graph is frozen"

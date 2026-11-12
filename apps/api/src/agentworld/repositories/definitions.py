"""Agents and model profiles: the definition tables a human edits.

Separate from `derived.py`, which applies what folding an event decided, and
the separation is the point. Everything here is control plane - it writes an
`audit_log` row, it is authorized by the permission matrix, and no event can
reach it. Everything there is derived - it is rebuilt by replay and no API
writes it.

A consequence worth knowing: `AgentRepository.update` cannot change `status`,
`last_heartbeat_at` or `capabilities`, because there is no parameter for
them. That is what makes "state is derived" true rather than a convention
somebody could forget while adding a field.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult

from agentworld.domain.entities import (
    Agent,
    AgentStatusChange,
    Connection,
    ModelProfile,
    Run,
    Workflow,
    WorkflowDraft,
    WorkflowVersion,
)
from agentworld.domain.enums import AgentInteractionMode, ConnectionKind, SideEffect
from agentworld.repositories import models as m
from agentworld.repositories.base import ScopedRepository

#: Sentinel for "this field was not sent", so a PATCH can tell "set it to
#: null" apart from "leave it alone". `None` cannot do both jobs.
UNSET: Any = object()


class ModelProfileRepository(ScopedRepository):
    """Workspace-scoped: one choice of provider and model per company."""

    async def create(
        self,
        *,
        name: str,
        provider_slug: str,
        model_id: str,
        parameters: Mapping[str, Any] | None = None,
        credential_id: UUID | None = None,
        is_default: bool = False,
    ) -> ModelProfile:
        row = (
            await self._session.execute(
                sa.insert(m.ModelProfile)
                .values(
                    workspace_id=self.scope.workspace_id,
                    name=name,
                    provider_slug=provider_slug,
                    model_id=model_id,
                    parameters=dict(parameters or {}),
                    credential_id=credential_id,
                    is_default=is_default,
                )
                .returning(m.ModelProfile)
            )
        ).scalar_one()
        return _to_profile(row)

    async def get(self, profile_id: UUID) -> ModelProfile | None:
        row = (
            await self._session.execute(
                sa.select(m.ModelProfile).where(
                    m.ModelProfile.id == profile_id,
                    m.ModelProfile.workspace_id == self.scope.workspace_id,
                )
            )
        ).scalar_one_or_none()
        return _to_profile(row) if row is not None else None

    async def list_all(self) -> Sequence[ModelProfile]:
        rows = await self._session.execute(
            sa.select(m.ModelProfile)
            .where(m.ModelProfile.workspace_id == self.scope.workspace_id)
            .order_by(m.ModelProfile.created_at)
        )
        return [_to_profile(row) for row in rows.scalars().all()]

    async def name_taken(self, name: str, *, excluding: UUID | None = None) -> bool:
        query = sa.select(sa.literal(1)).where(
            m.ModelProfile.workspace_id == self.scope.workspace_id,
            m.ModelProfile.name == name,
        )
        if excluding is not None:
            query = query.where(m.ModelProfile.id != excluding)
        return (await self._session.execute(query.limit(1))).scalar_one_or_none() is not None

    async def update(self, profile_id: UUID, **fields: Any) -> ModelProfile | None:
        values = {name: value for name, value in fields.items() if value is not UNSET}
        if not values:
            return await self.get(profile_id)
        row = (
            await self._session.execute(
                sa.update(m.ModelProfile)
                .where(
                    m.ModelProfile.id == profile_id,
                    m.ModelProfile.workspace_id == self.scope.workspace_id,
                )
                .values(**values)
                .returning(m.ModelProfile)
            )
        ).scalar_one_or_none()
        return _to_profile(row) if row is not None else None

    async def delete(self, profile_id: UUID) -> bool:
        return bool(
            await _rows(
                self._session,
                sa.delete(m.ModelProfile).where(
                    m.ModelProfile.id == profile_id,
                    m.ModelProfile.workspace_id == self.scope.workspace_id,
                ),
            )
        )

    async def clear_default(self) -> None:
        """Stand down the current default, if there is one.

        Called *before* inserting a new default, not after. Migration 0007
        already declares `one_default_model_profile`, a unique index over
        `workspace_id` where `is_default` - so a workspace cannot briefly
        have two even inside a transaction, and inserting first would hit
        the index rather than be tidied up afterwards. The database was
        right and the first draft of this was wrong.
        """
        await self._session.execute(
            sa.update(m.ModelProfile)
            .where(
                m.ModelProfile.workspace_id == self.scope.workspace_id,
                m.ModelProfile.is_default.is_(True),
            )
            .values(is_default=False)
        )


class AgentRepository(ScopedRepository):
    """Project-scoped. Needs `scope.project_id` and says so."""

    async def create(
        self,
        *,
        agent_key: str,
        display_name: str,
        interaction_mode: AgentInteractionMode,
        created_by_user_id: UUID | None = None,
        role: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
        kind: str | None = None,
        labels: Mapping[str, Any] | None = None,
        model_profile_id: UUID | None = None,
        model_overrides: Mapping[str, Any] | None = None,
        dispatch: Mapping[str, Any] | None = None,
        heartbeat_ttl_seconds: int = 60,
    ) -> Agent:
        row = (
            await self._session.execute(
                sa.insert(m.Agent)
                .values(
                    workspace_id=self.scope.workspace_id,
                    project_id=self.scope.require_project(),
                    agent_key=agent_key,
                    display_name=display_name,
                    interaction_mode=interaction_mode,
                    created_by_user_id=created_by_user_id,
                    role=role,
                    description=description,
                    instructions=instructions,
                    kind=kind,
                    labels=dict(labels or {}),
                    model_profile_id=model_profile_id,
                    model_overrides=dict(model_overrides or {}),
                    dispatch=dict(dispatch or {}),
                    heartbeat_ttl_seconds=heartbeat_ttl_seconds,
                )
                .returning(m.Agent)
            )
        ).scalar_one()
        return _to_agent(row)

    async def get(self, agent_id: UUID, *, include_archived: bool = False) -> Agent | None:
        query = sa.select(m.Agent).where(
            m.Agent.id == agent_id,
            m.Agent.project_id == self.scope.require_project(),
        )
        if not include_archived:
            query = query.where(m.Agent.archived_at.is_(None))
        row = (await self._session.execute(query)).scalar_one_or_none()
        return _to_agent(row) if row is not None else None

    async def by_key(self, agent_key: str) -> Agent | None:
        """The agent a dispatch instruction names.

        By key rather than by id, because that is what `advance()` put in the
        instruction - and a key is what survives an agent being recreated,
        which is the case a frozen workflow version has to tolerate.
        Archived agents are excluded: the step runner must not perform work
        for one, and the alternative is a turn that runs for an agent
        somebody retired.
        """
        row = (
            await self._session.execute(
                sa.select(m.Agent).where(
                    m.Agent.agent_key == agent_key,
                    m.Agent.project_id == self.scope.require_project(),
                    m.Agent.archived_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return _to_agent(row) if row is not None else None

    async def list_all(self, *, include_archived: bool = False) -> Sequence[Agent]:
        """Archived agents are excluded by default and available on request.

        They are not deleted: an archived agent keeps its runs, its history
        and its place on the island, where it renders shuttered. Hiding it
        from the default listing is a UI concern; losing it would be a
        rewriting of what happened.
        """
        query = sa.select(m.Agent).where(m.Agent.project_id == self.scope.require_project())
        if not include_archived:
            query = query.where(m.Agent.archived_at.is_(None))
        rows = await self._session.execute(query.order_by(m.Agent.registered_at))
        return [_to_agent(row) for row in rows.scalars().all()]

    async def status_history(
        self, agent_id: UUID, *, limit: int = 50
    ) -> Sequence[AgentStatusChange]:
        """This agent's status transitions, newest first.

        Read-only, and the only reader of a table three years of events
        could fill: `agent_status_history` is written by the reducer, one row
        per transition, and nothing has ever read it back until now.

        `project_id` as well as `agent_id`, like every other read in this
        class: the tenant predicate does not weaken because the caller
        happens to know an id, and RLS sits behind it rather than instead of
        it.

        Ordered by `changed_at` and then by `id`. The timestamp defaults to
        `now()`, which is the transaction's start time, so every transition
        folded from one ingest batch shares it exactly - and an order that
        left those to the planner would not be stable between calls. The
        index on `(agent_id, changed_at DESC)` still serves the leading
        columns.
        """
        rows = await self._session.execute(
            sa.select(m.AgentStatusHistory)
            .where(
                m.AgentStatusHistory.agent_id == agent_id,
                m.AgentStatusHistory.project_id == self.scope.require_project(),
            )
            .order_by(
                m.AgentStatusHistory.changed_at.desc(),
                m.AgentStatusHistory.id.desc(),
            )
            .limit(limit)
        )
        return [_to_status_change(row) for row in rows.scalars().all()]

    async def runs(self, agent_id: UUID, *, limit: int = 50) -> Sequence[Run]:
        """This agent's own runs, newest first.

        The second reader of a derived table, and the same shape as
        `status_history` deliberately: one question, one agent, a bounded
        list, the tenant predicate stated here rather than left to RLS.

        **This returns nothing in production today, and that is honest
        rather than broken.** `runs` is folded by the reducers but created
        by nothing - `DerivedStateRepository` patches a run and appends its
        steps, and no statement anywhere inserts one - so until a producer
        exists the table is empty for every agent. An empty list is then a
        true answer about a real table, which is the only answer available
        without inventing rows.

        Ordered by `started_at` and then by `id`. `runs_agent_idx` is
        `(agent_id, started_at DESC)`, so the leading columns are served by
        it; `id` is the tiebreak, because `started_at` defaults to `now()`
        and every run folded from one ingest batch shares it exactly.
        """
        rows = await self._session.execute(
            sa.select(m.Run)
            .where(
                m.Run.agent_id == agent_id,
                m.Run.project_id == self.scope.require_project(),
            )
            .order_by(m.Run.started_at.desc(), m.Run.id.desc())
            .limit(limit)
        )
        return [_to_run(row) for row in rows.scalars().all()]

    async def key_taken(self, agent_key: str) -> bool:
        found = (
            await self._session.execute(
                sa.select(sa.literal(1))
                .where(
                    m.Agent.project_id == self.scope.require_project(),
                    m.Agent.agent_key == agent_key,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found is not None

    async def update(self, agent_id: UUID, **fields: Any) -> Agent | None:
        """Change definition or configuration. Never derived state.

        There is no parameter for `status`, `last_heartbeat_at`,
        `capabilities` or `current_task_id`, so no caller can write them -
        which is how "state is derived" stays true as this class grows.
        """
        values = {name: value for name, value in fields.items() if value is not UNSET}
        if not values:
            return await self.get(agent_id)
        row = (
            await self._session.execute(
                sa.update(m.Agent)
                .where(
                    m.Agent.id == agent_id,
                    m.Agent.project_id == self.scope.require_project(),
                    m.Agent.archived_at.is_(None),
                )
                .values(**values)
                .returning(m.Agent)
            )
        ).scalar_one_or_none()
        return _to_agent(row) if row is not None else None

    async def archive(self, agent_id: UUID) -> bool:
        return bool(
            await _rows(
                self._session,
                sa.update(m.Agent)
                .where(
                    m.Agent.id == agent_id,
                    m.Agent.project_id == self.scope.require_project(),
                    m.Agent.archived_at.is_(None),
                )
                .values(archived_at=sa.func.now()),
            )
        )

    async def restore(self, agent_id: UUID) -> bool:
        """Bring an archived agent back, with nothing else changed.

        `archived_at IS NOT NULL`, so restoring a live agent is false rather
        than a silent no-op - the service turns that into the same 404 a
        second `archive` gets, because "restore this" about something that
        was never away is a caller who believes something that is not true.

        One column. No `status`, no `last_heartbeat_at`, no `capabilities`,
        no `current_task_id`: those are folded from events and this is not an
        event. An agent archived while `working` comes back saying
        `working`, with the heartbeat it last sent - which is honest, and
        which `AgentView` already shows a reader. Writing a status here
        would be this class inventing one, and `update` refuses to do that
        for the same reason.

        There is no identity to re-check. `key_taken` counts archived rows,
        so the archived row has held its `agent_key` all along and
        `agents_project_key_key` cannot refuse this.
        """
        return bool(
            await _rows(
                self._session,
                sa.update(m.Agent)
                .where(
                    m.Agent.id == agent_id,
                    m.Agent.project_id == self.scope.require_project(),
                    m.Agent.archived_at.is_not(None),
                )
                .values(archived_at=None),
            )
        )

    async def profile_in_use(self, profile_id: UUID) -> bool:
        """Whether any agent in this workspace still names that profile.

        Workspace-wide rather than project-wide: a profile is a workspace
        object, so deleting one has to consider every project that uses it.
        """
        found = (
            await self._session.execute(
                sa.select(sa.literal(1))
                .where(
                    m.Agent.workspace_id == self.scope.workspace_id,
                    m.Agent.model_profile_id == profile_id,
                    m.Agent.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found is not None


async def _rows(session: Any, statement: sa.Executable) -> int:
    """How many rows a DML statement touched.

    `session.execute` is typed as returning a `Result`; a DML statement
    really returns a `CursorResult`, which is the one carrying `rowcount`.
    """
    result = cast("CursorResult[Any]", await session.execute(statement))
    return result.rowcount or 0


# ------------------------------------------------------------- conversion ---


def _to_profile(row: m.ModelProfile) -> ModelProfile:
    return ModelProfile(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        provider_slug=row.provider_slug,
        model_id=row.model_id,
        parameters=dict(row.parameters),
        credential_id=row.credential_id,
        is_default=row.is_default,
        created_at=row.created_at,
    )


def _to_run(row: m.Run) -> Run:
    return Run(
        id=row.id,
        status=row.status,
        started_at=row.started_at,
        run_ref=row.run_ref,
        task_id=row.task_id,
        execution_step_id=row.execution_step_id,
        step_count=row.step_count,
        tool_call_count=row.tool_call_count,
        error=dict(row.error) if row.error is not None else None,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
    )


def _to_status_change(row: m.AgentStatusHistory) -> AgentStatusChange:
    return AgentStatusChange(
        from_status=row.from_status,
        to_status=row.to_status,
        changed_at=row.changed_at,
        reason=row.reason,
        event_id=row.event_id,
    )


def _to_agent(row: m.Agent) -> Agent:
    return Agent(
        id=row.id,
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        agent_key=row.agent_key,
        interaction_mode=row.interaction_mode,
        display_name=row.display_name,
        role=row.role,
        description=row.description,
        instructions=row.instructions,
        kind=row.kind,
        labels=dict(row.labels),
        model_profile_id=row.model_profile_id,
        model_overrides=dict(row.model_overrides),
        dispatch=dict(row.dispatch),
        heartbeat_ttl_seconds=row.heartbeat_ttl_seconds,
        status=row.status,
        status_reason=row.status_reason,
        capabilities=tuple(row.capabilities or ()),
        runtime=dict(row.runtime),
        current_task_id=row.current_task_id,
        last_heartbeat_at=row.last_heartbeat_at,
        last_event_at=row.last_event_at,
        registered_at=row.registered_at,
        archived_at=row.archived_at,
    )


class ConnectionRepository(ScopedRepository):
    """Standing grants and routes between components.

    Not a workflow edge. An edge is control flow inside one workflow version;
    a connection is a capability that outlives any single workflow. The island
    draws both as paths, which is exactly where the two get conflated - and
    conflating them is how a line someone drew becomes a permission nobody
    granted.
    """

    async def create(
        self,
        *,
        kind: ConnectionKind,
        source_kind: str,
        source_id: UUID,
        target_kind: str,
        target_id: UUID,
        config: Mapping[str, Any] | None = None,
        requires_approval: bool = False,
        enabled: bool = True,
        created_by_user_id: UUID | None = None,
    ) -> Connection:
        row = (
            await self._session.execute(
                sa.insert(m.Connection)
                .values(
                    workspace_id=self.scope.workspace_id,
                    project_id=self.scope.require_project(),
                    kind=kind,
                    source_kind=source_kind,
                    source_id=source_id,
                    target_kind=target_kind,
                    target_id=target_id,
                    config=dict(config or {}),
                    requires_approval=requires_approval,
                    enabled=enabled,
                    created_by_user_id=created_by_user_id,
                )
                .returning(m.Connection)
            )
        ).scalar_one()
        return _to_connection(row)

    async def get(self, connection_id: UUID) -> Connection | None:
        row = (
            await self._session.execute(
                sa.select(m.Connection).where(
                    m.Connection.id == connection_id,
                    m.Connection.project_id == self.scope.require_project(),
                )
            )
        ).scalar_one_or_none()
        return _to_connection(row) if row is not None else None

    async def list_all(self) -> Sequence[Connection]:
        rows = await self._session.execute(
            sa.select(m.Connection)
            .where(m.Connection.project_id == self.scope.require_project())
            .order_by(m.Connection.created_at)
        )
        return [_to_connection(row) for row in rows.scalars().all()]

    async def update(self, connection_id: UUID, **fields: Any) -> Connection | None:
        values = {name: value for name, value in fields.items() if value is not UNSET}
        if not values:
            return await self.get(connection_id)
        row = (
            await self._session.execute(
                sa.update(m.Connection)
                .where(
                    m.Connection.id == connection_id,
                    m.Connection.project_id == self.scope.require_project(),
                )
                .values(**values)
                .returning(m.Connection)
            )
        ).scalar_one_or_none()
        return _to_connection(row) if row is not None else None

    async def delete(self, connection_id: UUID) -> bool:
        return bool(
            await _rows(
                self._session,
                sa.delete(m.Connection).where(
                    m.Connection.id == connection_id,
                    m.Connection.project_id == self.scope.require_project(),
                ),
            )
        )

    async def tool_grants(self) -> Mapping[UUID, frozenset[str]]:
        """Which tools each agent can actually reach, as validation needs them.

        Enabled grants only: a disabled connection is a capability somebody
        deliberately switched off, and treating it as live would make
        `tool_not_granted` miss exactly the case the switch exists for.
        """
        rows = await self._session.execute(
            sa.select(m.Connection.source_id, m.Connection.config).where(
                m.Connection.project_id == self.scope.require_project(),
                m.Connection.kind == ConnectionKind.GRANT_TOOL,
                m.Connection.source_kind == "agent",
                m.Connection.enabled.is_(True),
            )
        )
        grants: dict[UUID, set[str]] = {}
        for source_id, config in rows.all():
            name = config.get("tool_name")
            if isinstance(name, str) and name:
                grants.setdefault(source_id, set()).add(name)
        return {agent_id: frozenset(names) for agent_id, names in grants.items()}

    async def tool_effects(self) -> Mapping[str, SideEffect]:
        """What each granted tool can do, read from the grants themselves.

        There is no `tools` table until Phase 6, so the side effect is
        recorded on the grant. The strictest wins where two grants disagree:
        a tool that is consequential anywhere is consequential.
        """
        rows = await self._session.execute(
            sa.select(m.Connection.config).where(
                m.Connection.project_id == self.scope.require_project(),
                m.Connection.kind == ConnectionKind.GRANT_TOOL,
            )
        )
        ranked = {SideEffect.READ: 0, SideEffect.WRITE: 1, SideEffect.CONSEQUENTIAL: 2}
        effects: dict[str, SideEffect] = {}
        for (config,) in rows.all():
            name, raw = config.get("tool_name"), config.get("side_effect")
            if not isinstance(name, str) or not name:
                continue
            try:
                effect = SideEffect(raw) if raw else SideEffect.READ
            except ValueError:
                effect = SideEffect.READ
            if name not in effects or ranked[effect] > ranked[effects[name]]:
                effects[name] = effect
        return effects


class WorkflowRepository(ScopedRepository):
    """Workflows and their draft versions.

    A *draft* is a `workflow_versions` row with `published_at IS NULL`, and
    its graph lives in one jsonb column rather than in `workflow_steps` and
    `workflow_edges`. That is deliberate: a draft is edited whole, by someone
    dragging things around, and normalising it on every save would be a great
    deal of work to produce rows nothing reads yet. Publishing is what freezes
    it into the normalised tables, and publishing arrives with the execution
    core - nothing can run a frozen version until there is something to run
    it with.
    """

    async def create(
        self,
        *,
        slug: str,
        name: str,
        description: str | None = None,
        created_by_user_id: UUID | None = None,
        graph: Mapping[str, Any] | None = None,
    ) -> tuple[Workflow, WorkflowDraft]:
        """A workflow and its first draft, in one transaction.

        A workflow with no version at all would be a row the UI has to
        special-case the first time it opens one, so there is never one.
        """
        workflow = (
            await self._session.execute(
                sa.insert(m.Workflow)
                .values(
                    workspace_id=self.scope.workspace_id,
                    project_id=self.scope.require_project(),
                    slug=slug,
                    name=name,
                    description=description,
                    created_by_user_id=created_by_user_id,
                )
                .returning(m.Workflow)
            )
        ).scalar_one()
        draft = await self._new_version(
            workflow.id, version=1, graph=graph, created_by_user_id=created_by_user_id
        )
        return _to_workflow(workflow), draft

    async def get(self, workflow_id: UUID, *, include_archived: bool = False) -> Workflow | None:
        """One workflow by id, archived rows excluded by default.

        The default is what every lifecycle verb wants. `save_draft`,
        `publish`, `start`, `validate` and `archive` all read through here,
        and an archived workflow is not something any of them may touch - so
        the filter lives in one place rather than in five, and each of them
        raises the 404 it already raised.

        `include_archived=True` belongs to the read surface, and matches
        `AgentRepository.get`'s own default-off switch: `GET
        /workflows/{id}` answers with `archived_at` set rather than
        pretending the row is gone, so a client can say "archived" instead
        of "not found" about a workflow it can still see in an execution's
        `workflow_id`.
        """
        conditions = [
            m.Workflow.id == workflow_id,
            m.Workflow.project_id == self.scope.require_project(),
        ]
        if not include_archived:
            conditions.append(m.Workflow.archived_at.is_(None))
        row = (
            await self._session.execute(sa.select(m.Workflow).where(*conditions))
        ).scalar_one_or_none()
        return _to_workflow(row) if row is not None else None

    async def update(self, workflow_id: UUID, **fields: Any) -> Workflow | None:
        """Change a workflow's metadata. Never its graph, never its pointer.

        There is no parameter for `current_version_id`, `enabled`,
        `test_for_agent_id` or `archived_at`, so no caller can write them
        from here: the pointer is `publish`'s, the marker is
        `find_or_create_test`'s, and `archived_at` is `archive`'s. What this
        can change is what the workflow is *called*.

        `archived_at IS NULL` in the statement, like `save_draft` and
        `archive`: an archived workflow is not editable, and the service's
        read has already turned that into a 404 rather than a silent no-op.
        """
        values = {name: value for name, value in fields.items() if value is not UNSET}
        if not values:
            return await self.get(workflow_id)
        row = (
            await self._session.execute(
                sa.update(m.Workflow)
                .where(
                    m.Workflow.id == workflow_id,
                    m.Workflow.project_id == self.scope.require_project(),
                    m.Workflow.archived_at.is_(None),
                )
                .values(**values)
                .returning(m.Workflow)
            )
        ).scalar_one_or_none()
        return _to_workflow(row) if row is not None else None

    async def list_all(self, *, include_archived: bool = False) -> Sequence[Workflow]:
        """Every workflow somebody authored, newest last.

        Test harnesses are excluded in both modes. They are real workflows
        and they run on the ordinary orchestrator, but nobody drew them and
        nobody edits them: listing one next to a person's own workflows
        would invite exactly the hand-editing that `save_draft` refuses.
        `get` still finds one by id, because publishing and running it are
        how a test happens - and since Stage A it says that it is one.

        `include_archived` is what makes restoring a workflow possible: a
        caller cannot restore what it cannot find, and before this the only
        way to reach an archived workflow was to have kept its id. The
        parameter, its name and its default are the agent listing's, so a
        client meets one convention rather than two.
        """
        conditions = [
            m.Workflow.project_id == self.scope.require_project(),
            m.Workflow.test_for_agent_id.is_(None),
        ]
        if not include_archived:
            conditions.append(m.Workflow.archived_at.is_(None))
        rows = await self._session.execute(
            sa.select(m.Workflow).where(*conditions).order_by(m.Workflow.created_at)
        )
        return [_to_workflow(row) for row in rows.scalars().all()]

    async def test_for(self, agent_id: UUID) -> Workflow | None:
        """That agent's test harness, if it has one.

        No `archived_at` filter, deliberately. The partial unique index does
        not exclude archived rows either, so a harness hidden from this read
        would be a harness `find_or_create_test` could never insert past -
        every later test of that agent would fail on a constraint instead of
        running. `archive` refuses a harness for the same reason, which is
        what keeps the two views of this row in agreement.
        """
        row = (
            await self._session.execute(
                sa.select(m.Workflow).where(
                    m.Workflow.test_for_agent_id == agent_id,
                    m.Workflow.project_id == self.scope.require_project(),
                )
            )
        ).scalar_one_or_none()
        return _to_workflow(row) if row is not None else None

    async def find_or_create_test(
        self,
        *,
        agent_id: UUID,
        slug: str,
        name: str,
        description: str | None = None,
        graph: Mapping[str, Any],
        created_by_user_id: UUID | None = None,
    ) -> tuple[Workflow | None, bool]:
        """That agent's test harness, creating it if this is the first test.

        Three outcomes, because there are three things that can be true:

        - `(workflow, True)` - this call created it, and the caller owes an
          audit row for a workflow it created;
        - `(workflow, False)` - it was already there, whether from an earlier
          test or from a transaction that beat this one by a moment;
        - `(None, False)` - the insert conflicted with a row that is *not*
          this agent's harness, which can only mean the slug belongs to a
          workflow somebody authored. The caller decides what to say about
          that; this layer does not raise.

        **`ON CONFLICT DO NOTHING` and a re-read, rather than a lock.** Two
        people pressing Test at the same moment both find nothing and both
        insert. Postgres waits for the first transaction to finish before
        deciding the second insert, and under READ COMMITTED the re-read
        takes a fresh snapshot - so the loser sees the committed row rather
        than nothing. Had the winner rolled back instead, the loser's insert
        is the one that lands.

        **The conflict is left uninferred, which was measured rather than
        chosen.** This first named the partial index on `test_for_agent_id`
        as the arbiter, on the reasoning that a slug collision is a different
        problem and should not be swallowed here. The concurrency test then
        failed on `workflows_project_slug_key`: two first tests of one agent
        collide on *both* indexes, and `DO NOTHING` on one arbiter does not
        protect against the other. An unqualified `DO NOTHING` is the only
        form that covers the real race - and it is still not a swallowed
        error, because the re-read tells the two conflicts apart afterwards.
        """
        inserted = (
            await self._session.execute(
                sa.dialects.postgresql.insert(m.Workflow)
                .values(
                    workspace_id=self.scope.workspace_id,
                    project_id=self.scope.require_project(),
                    slug=slug,
                    name=name,
                    description=description,
                    created_by_user_id=created_by_user_id,
                    test_for_agent_id=agent_id,
                )
                .on_conflict_do_nothing()
                .returning(m.Workflow)
            )
        ).scalar_one_or_none()

        if inserted is not None:
            # The same pairing `create` makes: a workflow with no version at
            # all is a row every reader has to special-case.
            await self._new_version(
                inserted.id, version=1, graph=graph, created_by_user_id=created_by_user_id
            )
            return _to_workflow(inserted), True

        return await self.test_for(agent_id), False

    async def slug_taken(self, slug: str) -> bool:
        found = (
            await self._session.execute(
                sa.select(sa.literal(1))
                .where(
                    m.Workflow.project_id == self.scope.require_project(),
                    m.Workflow.slug == slug,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found is not None

    async def draft_of(self, workflow_id: UUID) -> WorkflowDraft | None:
        """The unpublished version, which is the one being edited.

        At most one exists: `workflow_versions_open_draft_key` makes a second
        draft of one workflow unstorable, so the ordering below is a tiebreak
        for a tie the schema no longer permits rather than a choice between
        candidates.
        """
        row = (
            await self._session.execute(
                sa.select(m.WorkflowVersion)
                .where(
                    m.WorkflowVersion.workflow_id == workflow_id,
                    m.WorkflowVersion.project_id == self.scope.require_project(),
                    m.WorkflowVersion.published_at.is_(None),
                )
                .order_by(m.WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _to_draft(row) if row is not None else None

    async def save_draft(
        self, workflow_id: UUID, *, graph: Mapping[str, Any]
    ) -> WorkflowDraft | None:
        """Replace the draft's graph, unless this workflow is a test harness.

        The harness condition is in the statement rather than in a read
        before it, so no caller can write through this method by forgetting
        to check - including the conversational builder, which will one day
        hold `workflow.update_draft` and have no reason to know that some
        workflows are compiled. `WorkflowService.save_draft` checks first and
        says why; what reaches here is the floor under that.
        """
        row = (
            await self._session.execute(
                sa.update(m.WorkflowVersion)
                .where(
                    m.WorkflowVersion.workflow_id == workflow_id,
                    m.WorkflowVersion.project_id == self.scope.require_project(),
                    m.WorkflowVersion.published_at.is_(None),
                    ~sa.select(m.Workflow.id)
                    .where(
                        m.Workflow.id == m.WorkflowVersion.workflow_id,
                        m.Workflow.test_for_agent_id.isnot(None),
                    )
                    .exists(),
                )
                .values(graph=dict(graph))
                .returning(m.WorkflowVersion)
            )
        ).scalar_one_or_none()
        return _to_draft(row) if row is not None else None

    async def publish(
        self,
        workflow_id: UUID,
        *,
        version_id: UUID,
        steps: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
    ) -> dt.datetime:
        """Freeze a draft, and point the workflow at what was frozen.

        **The order is forced by migration 0018 and is not a style choice.**
        The trigger refuses an INSERT into either table when the version it
        belongs to already has `published_at` set, so the graph goes in
        first and `published_at` goes on last. Doing it the obvious way
        round - mark it published, then write its contents - makes the
        trigger refuse the publish's own inserts. T0 found that by breaking
        a fixture that did exactly that.

        `steps` carries `step_key`, `name`, `kind`, `agent_id`,
        `input_mapping` and `config`; `edges` carries `from_step` and
        `to_step` as **step keys**, which this maps to the ids it just
        inserted. The caller works in keys because that is what the author
        wrote; ids exist only once the rows do.

        **`workflows.current_version_id` moves here, in this transaction.**
        Publishing *is* moving that pointer - `01-DOMAIN-AND-PROTOCOL.md`
        says "publishing moves `current_version_id`" as one sentence about
        one act - and a publish that could commit without moving it is a
        publish that can leave the column lying. It lay for every workflow
        in the system until now: the column was declared, foreign-keyed and
        read by the entity, the repository mapper and the API view, and
        written by nothing, so every reader reassembled the answer with
        `ORDER BY version DESC` and two of them carried a comment
        apologising for it.
        """
        ids: dict[str, UUID] = {}
        for step in steps:
            step_id = (
                await self._session.execute(
                    sa.insert(m.WorkflowStep)
                    .values(
                        workflow_version_id=version_id,
                        workspace_id=self.scope.workspace_id,
                        project_id=self.scope.require_project(),
                        step_key=step["step_key"],
                        name=step.get("name") or step["step_key"],
                        kind=step["kind"],
                        agent_id=step.get("agent_id"),
                        tool_id=step.get("tool_id"),
                        config=dict(step.get("config") or {}),
                        input_mapping=dict(step.get("input_mapping") or {}),
                    )
                    .returning(m.WorkflowStep.id)
                )
            ).scalar_one()
            ids[str(step["step_key"])] = step_id

        for edge in edges:
            to_key = str(edge["to_step"])
            from_key = edge.get("from_step")
            await self._session.execute(
                sa.insert(m.WorkflowEdge).values(
                    workflow_version_id=version_id,
                    workspace_id=self.scope.workspace_id,
                    project_id=self.scope.require_project(),
                    from_step_id=ids[str(from_key)] if from_key else None,
                    to_step_id=ids[to_key],
                    condition={"expression": edge["condition"]} if edge.get("condition") else None,
                )
            )

        published = (
            await self._session.execute(
                sa.update(m.WorkflowVersion)
                .where(
                    m.WorkflowVersion.id == version_id,
                    m.WorkflowVersion.project_id == self.scope.require_project(),
                    m.WorkflowVersion.published_at.is_(None),
                )
                .values(published_at=sa.func.now())
                .returning(m.WorkflowVersion.published_at)
            )
        ).scalar_one()
        # `scalar_one` already proved a row came back, and the column was just
        # set to `now()`, so this cannot be None. Narrowed rather than cast,
        # so that a future change making the column nullable fails here.
        assert published is not None

        # `workflow_id` is in this predicate rather than unused - it was
        # unused until now, which is how a publish identified by `version_id`
        # alone kept the "this version belongs to this workflow" guarantee
        # entirely in its caller's head. The composite foreign key
        # `(id, current_version_id) -> workflow_versions(workflow_id, id)`
        # refuses a mismatched pair outright, so this statement cannot point
        # a workflow at another one's graph however it is called.
        #
        # `scalar_one` for the reason the update above uses it: the caller
        # read this workflow under this scope in this transaction, so no row
        # here means something is badly wrong, and a silent no-op is how a
        # column stops being written without anybody noticing.
        (
            await self._session.execute(
                sa.update(m.Workflow)
                .where(
                    m.Workflow.id == workflow_id,
                    m.Workflow.project_id == self.scope.require_project(),
                )
                .values(current_version_id=version_id)
                .returning(m.Workflow.id)
            )
        ).scalar_one()
        return published

    async def published_version(self, workflow_id: UUID) -> WorkflowDraft | None:
        """The newest published version, derived rather than read.

        Returns the same shape as `draft_of` because a frozen version keeps
        its `graph` column: the normalised tables are what the island, the
        DAG canvas and `execution_steps`' foreign keys read, and the jsonb is
        what the orchestrator reads. Both are immutable after publish, so
        they cannot disagree.

        **No longer how anything decides what to run.** `start` reads
        `workflows.current_version_id`, and the harness predicate in
        `AgentTestService` reads the same column; this is the independent
        second opinion they are checked against, which is the one job it is
        now good for. The two answers are the same fact computed two ways and
        a test that asserts they agree is a test that the pointer has not
        drifted - so this method stays, and nothing in `services/` calls it.

        Not the question to ask about a run that has already started either:
        see `published_version_by_id`.
        """
        row = (
            await self._session.execute(
                sa.select(m.WorkflowVersion)
                .where(
                    m.WorkflowVersion.workflow_id == workflow_id,
                    m.WorkflowVersion.project_id == self.scope.require_project(),
                    m.WorkflowVersion.published_at.is_not(None),
                )
                .order_by(m.WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _to_draft(row) if row is not None else None

    async def published_versions(self, workflow_id: UUID) -> Sequence[WorkflowVersion]:
        """Every published version of this workflow, newest first.

        `published_at IS NOT NULL` is the whole of "history", and it is also
        why a draft cannot appear in one: every publish opens the next draft
        behind itself, so a workflow that has been published even once
        always has an unpublished row, and that row is the thing somebody is
        still editing. Migration 0018 makes a published row immutable, so
        what this returns cannot change afterwards - only grow.

        Ordered by `version`, not by `published_at`. The number is unique
        per workflow (`workflow_versions_number_key`), so it is a total
        order and the sequence is the same on every call; two rows can share
        a timestamp, and an order that left the tie to the planner would not
        be stable.

        Unbounded, unlike the execution listings. A version exists because
        somebody pressed Publish, so the count is human-paced, and each row
        here is four scalars rather than a graph - whereas a cap would
        silently drop the *oldest* versions, which is the half of history a
        reader cannot reconstruct from anywhere else.
        """
        rows = await self._session.execute(
            sa.select(m.WorkflowVersion)
            .where(
                m.WorkflowVersion.workflow_id == workflow_id,
                m.WorkflowVersion.project_id == self.scope.require_project(),
                m.WorkflowVersion.published_at.is_not(None),
            )
            .order_by(m.WorkflowVersion.version.desc())
        )
        return [_to_version(row) for row in rows.scalars().all()]

    async def published_version_by_id(
        self, workflow_id: UUID, *, version_id: UUID
    ) -> WorkflowDraft | None:
        """One published version, named rather than inferred.

        What a *running* execution advances against. "The newest published
        version" is the right answer only at the moment a run is created;
        afterwards publishing moves it, while the execution stays bound to
        the version it was created with. Deciding from the newer graph
        changes what a running execution means retroactively, which is the
        one thing `WorkflowExecution`'s docstring and migration 0018 both
        promise cannot happen.

        `workflow_id` as well. Migration 0023 now makes the pair on
        `workflow_executions` a composite foreign key, so a stored execution
        cannot name another workflow's version in the first place - but this
        method is also how `start` checks the pointer it was handed, where
        the pair has not been stored yet and where a mismatch must read as
        "gone" rather than as somebody else's graph. One predicate, holding
        on both sides of the write.

        `published_at IS NOT NULL`, because an execution of a draft is not a
        state this system has: `start` binds the version
        `workflows.current_version_id` names, having asked this same method
        whether it is published, and a published version can never be
        unpublished afterwards.
        """
        row = (
            await self._session.execute(
                sa.select(m.WorkflowVersion).where(
                    m.WorkflowVersion.id == version_id,
                    m.WorkflowVersion.workflow_id == workflow_id,
                    m.WorkflowVersion.project_id == self.scope.require_project(),
                    m.WorkflowVersion.published_at.is_not(None),
                )
            )
        ).scalar_one_or_none()
        return _to_draft(row) if row is not None else None

    async def next_draft(self, workflow_id: UUID, *, after: int) -> WorkflowDraft:
        """A fresh draft above a version that has just been published.

        Publishing freezes what was being edited, so without this the editor
        would have nothing to open. The new draft starts as a copy of what
        was published, which is what someone who clicks "edit" after
        publishing expects.
        """
        previous = (
            await self._session.execute(
                sa.select(m.WorkflowVersion.graph).where(
                    m.WorkflowVersion.workflow_id == workflow_id,
                    m.WorkflowVersion.project_id == self.scope.require_project(),
                    m.WorkflowVersion.version == after,
                )
            )
        ).scalar_one()
        return await self._new_version(
            workflow_id, version=after + 1, graph=previous, created_by_user_id=None
        )

    async def restore(self, workflow_id: UUID) -> bool:
        """Bring an archived workflow back, with nothing else changed.

        A harness is excluded here as it is in `archive`, and the exclusion
        can never fire: a harness cannot be archived, so there is none to
        restore. It is in the statement anyway, because the pair of verbs
        should be refusable by the same predicate rather than by a reader
        remembering which one needed it.

        Nothing about the versions. `archive` never touched them, so a
        restored workflow comes back with its history, its open draft and
        the `current_version_id` it had - immediately runnable at the
        version it was runnable at. `workflow_versions_open_draft_key` does
        not look at `workflows.archived_at`, so the draft was never in
        question either.

        `slug_taken` counts archived rows, so `workflows_project_slug_key`
        cannot refuse this: the row has held its slug since the day it was
        archived.
        """
        return bool(
            await _rows(
                self._session,
                sa.update(m.Workflow)
                .where(
                    m.Workflow.id == workflow_id,
                    m.Workflow.project_id == self.scope.require_project(),
                    m.Workflow.archived_at.is_not(None),
                    m.Workflow.test_for_agent_id.is_(None),
                )
                .values(archived_at=None),
            )
        )

    async def archive(self, workflow_id: UUID) -> bool:
        """Archive a workflow, unless it is a test harness.

        A harness has to stay archivable-by-nobody for the reason `test_for`
        gives: the unique index spans archived rows, so an archived harness
        is one no later test can replace and none can use. Refused in the
        statement for the same reason `save_draft` is.
        """
        return bool(
            await _rows(
                self._session,
                sa.update(m.Workflow)
                .where(
                    m.Workflow.id == workflow_id,
                    m.Workflow.project_id == self.scope.require_project(),
                    m.Workflow.archived_at.is_(None),
                    m.Workflow.test_for_agent_id.is_(None),
                )
                .values(archived_at=sa.func.now()),
            )
        )

    async def _new_version(
        self,
        workflow_id: UUID,
        *,
        version: int,
        graph: Mapping[str, Any] | None,
        created_by_user_id: UUID | None,
    ) -> WorkflowDraft:
        row = (
            await self._session.execute(
                sa.insert(m.WorkflowVersion)
                .values(
                    workflow_id=workflow_id,
                    workspace_id=self.scope.workspace_id,
                    project_id=self.scope.require_project(),
                    version=version,
                    graph=dict(graph or {"steps": [], "edges": []}),
                    created_by_user_id=created_by_user_id,
                )
                .returning(m.WorkflowVersion)
            )
        ).scalar_one()
        return _to_draft(row)


def _to_connection(row: m.Connection) -> Connection:
    return Connection(
        id=row.id,
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        kind=row.kind,
        source_kind=row.source_kind,
        source_id=row.source_id,
        target_kind=row.target_kind,
        target_id=row.target_id,
        config=dict(row.config),
        requires_approval=row.requires_approval,
        enabled=row.enabled,
        created_at=row.created_at,
    )


def _to_workflow(row: m.Workflow) -> Workflow:
    return Workflow(
        id=row.id,
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        current_version_id=row.current_version_id,
        enabled=row.enabled,
        test_for_agent_id=row.test_for_agent_id,
        created_at=row.created_at,
        archived_at=row.archived_at,
    )


def _to_version(row: m.WorkflowVersion) -> WorkflowVersion:
    """A published version row, without its graph.

    The assertion is the predicate `published_versions` selects on, restated
    where the type demands it: a `WorkflowVersion` with no `published_at`
    would be a draft wearing the wrong name.
    """
    assert row.published_at is not None, "only published rows become a WorkflowVersion"
    return WorkflowVersion(
        id=row.id,
        workflow_id=row.workflow_id,
        version=row.version,
        published_at=row.published_at,
        created_at=row.created_at,
    )


def _to_draft(row: m.WorkflowVersion) -> WorkflowDraft:
    return WorkflowDraft(
        id=row.id,
        workflow_id=row.workflow_id,
        version=row.version,
        graph=dict(row.graph),
        published_at=row.published_at,
    )

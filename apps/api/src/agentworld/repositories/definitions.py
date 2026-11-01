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

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult

from agentworld.domain.entities import (
    Agent,
    Connection,
    ModelProfile,
    Workflow,
    WorkflowDraft,
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

    async def get(self, workflow_id: UUID) -> Workflow | None:
        row = (
            await self._session.execute(
                sa.select(m.Workflow).where(
                    m.Workflow.id == workflow_id,
                    m.Workflow.project_id == self.scope.require_project(),
                    m.Workflow.archived_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return _to_workflow(row) if row is not None else None

    async def list_all(self) -> Sequence[Workflow]:
        rows = await self._session.execute(
            sa.select(m.Workflow)
            .where(
                m.Workflow.project_id == self.scope.require_project(),
                m.Workflow.archived_at.is_(None),
            )
            .order_by(m.Workflow.created_at)
        )
        return [_to_workflow(row) for row in rows.scalars().all()]

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
        """The newest unpublished version, which is the one being edited."""
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
        row = (
            await self._session.execute(
                sa.update(m.WorkflowVersion)
                .where(
                    m.WorkflowVersion.workflow_id == workflow_id,
                    m.WorkflowVersion.project_id == self.scope.require_project(),
                    m.WorkflowVersion.published_at.is_(None),
                )
                .values(graph=dict(graph))
                .returning(m.WorkflowVersion)
            )
        ).scalar_one_or_none()
        return _to_draft(row) if row is not None else None

    async def archive(self, workflow_id: UUID) -> bool:
        return bool(
            await _rows(
                self._session,
                sa.update(m.Workflow)
                .where(
                    m.Workflow.id == workflow_id,
                    m.Workflow.project_id == self.scope.require_project(),
                    m.Workflow.archived_at.is_(None),
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
        created_at=row.created_at,
        archived_at=row.archived_at,
    )


def _to_draft(row: m.WorkflowVersion) -> WorkflowDraft:
    return WorkflowDraft(
        id=row.id,
        workflow_id=row.workflow_id,
        version=row.version,
        graph=dict(row.graph),
        published_at=row.published_at,
    )

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

from agentworld.domain.entities import Agent, ModelProfile
from agentworld.domain.enums import AgentInteractionMode
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

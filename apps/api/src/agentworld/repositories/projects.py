"""Projects.

Tenant-scoped, so every statement below carries an explicit
`workspace_id` predicate *and* runs under a row-level security policy that
enforces the same thing. The redundancy is the design: the predicate is what
makes the query correct, and the policy is what catches the day someone
writes a new method and leaves the predicate out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentworld.domain.entities import Project
from agentworld.repositories import models as m
from agentworld.repositories.base import GlobalRepository, ScopedRepository


class ProjectRepository(ScopedRepository):
    async def create(
        self,
        *,
        slug: str,
        name: str,
        description: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> Project:
        row = (
            await self._session.execute(
                sa.insert(m.Project)
                .values(
                    workspace_id=self.scope.workspace_id,
                    slug=slug,
                    name=name,
                    description=description,
                    settings=dict(settings or {}),
                )
                .returning(m.Project)
            )
        ).scalar_one()
        return _to_project(row)

    async def get(self, project_id: UUID) -> Project | None:
        row = (
            await self._session.execute(
                sa.select(m.Project).where(
                    m.Project.id == project_id,
                    m.Project.workspace_id == self.scope.workspace_id,
                    m.Project.archived_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return _to_project(row) if row is not None else None

    async def list_all(self) -> Sequence[Project]:
        rows = await self._session.execute(
            sa.select(m.Project)
            .where(
                m.Project.workspace_id == self.scope.workspace_id,
                m.Project.archived_at.is_(None),
            )
            .order_by(m.Project.created_at)
        )
        return [_to_project(row) for row in rows.scalars().all()]

    async def slug_taken(self, slug: str) -> bool:
        found = (
            await self._session.execute(
                sa.select(sa.literal(1))
                .where(
                    m.Project.workspace_id == self.scope.workspace_id,
                    m.Project.slug == slug,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found is not None

    async def update(
        self,
        project_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> Project | None:
        values: dict[str, Any] = {}
        if name is not None:
            values["name"] = name
        if description is not None:
            values["description"] = description
        if settings is not None:
            # Replace rather than merge. A partial merge would make it
            # impossible to remove a setting, and "PATCH merges one level
            # deep" is a rule every client would have to learn.
            values["settings"] = dict(settings)
        if not values:
            return await self.get(project_id)
        values["updated_at"] = sa.func.now()

        row = (
            await self._session.execute(
                sa.update(m.Project)
                .where(
                    m.Project.id == project_id,
                    m.Project.workspace_id == self.scope.workspace_id,
                    m.Project.archived_at.is_(None),
                )
                .values(**values)
                .returning(m.Project)
            )
        ).scalar_one_or_none()
        return _to_project(row) if row is not None else None

    async def archive(self, project_id: UUID) -> bool:
        archived = (
            await self._session.execute(
                sa.update(m.Project)
                .where(
                    m.Project.id == project_id,
                    m.Project.workspace_id == self.scope.workspace_id,
                    m.Project.archived_at.is_(None),
                )
                .values(archived_at=sa.func.now(), updated_at=sa.func.now())
                .returning(m.Project.id)
            )
        ).scalar_one_or_none()
        return archived is not None


def _to_project(row: m.Project) -> Project:
    return Project(
        id=row.id,
        workspace_id=row.workspace_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        settings=dict(row.settings),
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


class ProjectLookupRepository(GlobalRepository):
    """Resolve which workspace a project belongs to.

    `GET /v1/projects/{id}` is handed a project id and nothing else, so the
    workspace has to be discovered before a `TenantScope` can exist. This is
    the query that does it, and it is the reason the row-level security
    policies are keyed on the caller's whole visibility set rather than on one
    workspace: the policy confines this lookup to workspaces the caller
    already belongs to, so a project id belonging to a stranger simply has no
    row, and the route's 404 is the database's answer rather than a check
    someone had to remember to write.

    So despite subclassing `GlobalRepository`, this is not a tenant-crossing
    read in the way the credential lookups are. It crosses *workspaces the
    caller can already see*, and it cannot do more than that even if this
    method were called with no filter at all.
    """

    async def workspace_of(self, project_id: UUID) -> UUID | None:
        return (
            await self._session.execute(
                sa.select(m.Project.workspace_id).where(
                    m.Project.id == project_id, m.Project.archived_at.is_(None)
                )
            )
        ).scalar_one_or_none()

"""Workspaces.

`workspaces` carries no RLS policy (see migration `0004_rls`), so this
repository is the whole of the isolation for the table: every read is either
by an id the service layer has already authorized through
`domain.policy.authorize`, or filtered by membership. The two reads that are
neither - `create` and `slug_taken` - touch no existing tenant's data.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentworld.domain.entities import Workspace
from agentworld.repositories import models as m
from agentworld.repositories.base import GlobalRepository
from agentworld.repositories.users import to_workspace


class WorkspaceRepository(GlobalRepository):
    """Create and maintain workspaces.

    Global rather than scoped because a workspace *is* the tenant: there is no
    outer scope to confine it to. Authorization happens one layer up, in
    `services.workspaces`, which calls `authorize` before every method here
    that touches an existing row.
    """

    async def create(
        self, *, slug: str, name: str, created_by: UUID, workspace_id: UUID | None = None
    ) -> Workspace:
        """Insert a workspace, returning it.

        The id is generated here rather than by the database default so the
        caller knows it before the statement runs. That matters because the
        very next statement - inserting the creator's `owner` membership, and
        then anything project-shaped - runs under a row-level security
        session variable that has to name this workspace, and a value the
        database has not told us yet cannot be named.
        """
        row = (
            await self._session.execute(
                sa.insert(m.Workspace)
                .values(
                    id=workspace_id or uuid4(),
                    slug=slug,
                    name=name,
                    created_by=created_by,
                )
                .returning(m.Workspace)
            )
        ).scalar_one()
        return to_workspace(row)

    async def live_ids(self, *, limit: int = 1000) -> Sequence[UUID]:
        """Every workspace a background worker should look in.

        Unscoped, and legitimately so: `workspaces` is one of the four tables
        with no row-level security policy, because it is one of the tables
        that answer "which tenant is this?". A worker has no tenant until it
        finds work, and this is how it learns where to look - rather than
        reaching across `dispatches`, which RLS protects and which returns
        nothing to an unscoped session by design.

        Archived workspaces are excluded. Work queued in one is work nobody
        asked to continue.
        """
        rows = await self._session.execute(
            sa.select(m.Workspace.id)
            .where(m.Workspace.archived_at.is_(None))
            .order_by(m.Workspace.created_at)
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def get(self, workspace_id: UUID) -> Workspace | None:
        """Fetch by id. Archived workspaces are not returned.

        A soft-deleted workspace behaves exactly like one that never existed,
        which is what makes the 7-day grace period in `docs/03-API.md` section
        3.2 invisible to clients rather than a second kind of 404.
        """
        row = (
            await self._session.execute(
                sa.select(m.Workspace).where(
                    m.Workspace.id == workspace_id, m.Workspace.archived_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        return to_workspace(row) if row is not None else None

    async def slug_taken(self, slug: str) -> bool:
        """Whether the slug is in use, including by an archived workspace.

        Archived rows still hold their slug. **Workspace restoration is not
        implemented**: there is no endpoint, service or repository method
        for it, and an archived workspace's contents are unreachable anyway,
        because every project-nested route resolves its workspace through a
        lookup that excludes archived projects. Holding the slug is what
        would make a restore possible if one is ever built - task 3 stage C
        built the agent and workflow ones on exactly that property - and
        until then it only means a slug cannot be reused.
        """
        found = (
            await self._session.execute(
                sa.select(sa.literal(1)).where(m.Workspace.slug == slug).limit(1)
            )
        ).scalar_one_or_none()
        return found is not None

    async def update(
        self, workspace_id: UUID, *, name: str | None = None, slug: str | None = None
    ) -> Workspace | None:
        values: dict[str, object] = {}
        if name is not None:
            values["name"] = name
        if slug is not None:
            values["slug"] = slug
        if not values:
            return await self.get(workspace_id)
        values["updated_at"] = sa.func.now()

        row = (
            await self._session.execute(
                sa.update(m.Workspace)
                .where(m.Workspace.id == workspace_id, m.Workspace.archived_at.is_(None))
                .values(**values)
                .returning(m.Workspace)
            )
        ).scalar_one_or_none()
        return to_workspace(row) if row is not None else None

    async def archive(self, workspace_id: UUID) -> bool:
        """Soft delete. Nothing is erased; the row stops being visible.

        A hard delete would cascade through every project, agent and event the
        workspace ever held, which is not a thing to do at the end of an HTTP
        request. The purge after the grace period is a job, not a route.
        """
        archived = (
            await self._session.execute(
                sa.update(m.Workspace)
                .where(m.Workspace.id == workspace_id, m.Workspace.archived_at.is_(None))
                .values(archived_at=sa.func.now(), updated_at=sa.func.now())
                .returning(m.Workspace.id)
            )
        ).scalar_one_or_none()
        return archived is not None

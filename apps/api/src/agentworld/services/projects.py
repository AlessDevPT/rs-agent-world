"""Projects.

The interesting shape here is that `docs/03-API.md` addresses a project by id
alone - `GET /v1/projects/{id}` - with no workspace in the path. So every
single-project method resolves the workspace first, under a session that can
see only the caller's own workspaces, and *then* authorizes. A project id
belonging to a stranger has no row to resolve, so it 404s without any check
having to notice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from agentworld.domain import slugs
from agentworld.domain.entities import Project
from agentworld.domain.errors import ConflictError, NotFoundError
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import UserPrincipal
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services import audit
from agentworld.services.context import CallContext


class ProjectService:
    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    async def create(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        *,
        name: str,
        slug: str | None = None,
        description: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> Project:
        authorize(principal, Action.PROJECT_CREATE, Resource(workspace_id))
        resolved = slugs.validated(slug, fallback=name)

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            scope = TenantScope(workspace_id)
            projects = work.projects(scope)
            if await projects.slug_taken(resolved):
                raise ConflictError(
                    f"This workspace already has a project with the slug '{resolved}'",
                    slug=resolved,
                )
            project = await projects.create(
                slug=resolved, name=name, description=description, settings=settings
            )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="project.create",
                resource_type="project",
                resource_id=project.id,
                metadata={"slug": resolved},
            )
            return project

    async def list_in(self, principal: UserPrincipal, workspace_id: UUID) -> Sequence[Project]:
        authorize(principal, Action.PROJECT_READ, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            return await work.projects(TenantScope(workspace_id)).list_all()

    async def get(self, principal: UserPrincipal, project_id: UUID) -> Project:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await self._resolve(work, project_id)
            authorize(principal, Action.PROJECT_READ, Resource(workspace_id))
            project = await work.projects(TenantScope(workspace_id)).get(project_id)
            if project is None:  # pragma: no cover - resolved a moment ago
                raise NotFoundError("Project not found", project_id=str(project_id))
            return project

    async def update(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> Project:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await self._resolve(work, project_id)
            authorize(principal, Action.PROJECT_UPDATE, Resource(workspace_id))
            project = await work.projects(TenantScope(workspace_id)).update(
                project_id, name=name, description=description, settings=settings
            )
            if project is None:  # pragma: no cover - resolved a moment ago
                raise NotFoundError("Project not found", project_id=str(project_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="project.update",
                resource_type="project",
                resource_id=project_id,
            )
            return project

    async def archive(
        self, principal: UserPrincipal, context: CallContext, project_id: UUID
    ) -> None:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await self._resolve(work, project_id)
            authorize(principal, Action.PROJECT_DELETE, Resource(workspace_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="project.delete",
                resource_type="project",
                resource_id=project_id,
            )
            if not await work.projects(TenantScope(workspace_id)).archive(project_id):
                raise NotFoundError("Project not found", project_id=str(project_id))

    @staticmethod
    async def _resolve(work: UnitOfWork, project_id: UUID) -> UUID:
        """Which of the caller's workspaces holds this project.

        The session was opened over the caller's workspaces only, so RLS has
        already excluded everyone else's projects: a missing row here means
        either no such project or not this caller's, which is exactly the one
        answer `docs/03-API.md` section 2 says a 404 should give.
        """
        workspace_id = await work.project_lookup.workspace_of(project_id)
        if workspace_id is None:
            raise NotFoundError("Project not found", project_id=str(project_id))
        return workspace_id

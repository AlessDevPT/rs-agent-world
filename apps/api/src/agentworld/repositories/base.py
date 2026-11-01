"""The tenant scope, and what every repository has in common.

`docs/02-DATABASE.md` section 7 names the application layer as the *primary*
isolation control and row-level security as defence in depth. This is that
primary control: a tenant-scoped repository cannot be constructed without a
`TenantScope`, so there is no such thing as an unscoped query against a
tenant table. Forgetting the filter is not a mistake you can make quietly -
it is a constructor you cannot call.

Three repositories are deliberately *not* scoped, and each says so in its own
docstring: resolving a user by token subject, resolving which workspaces a
user belongs to, and resolving an API key by its prefix. Those three answer
the question "which tenant is this?", so they cannot presuppose it. They are
the entire cross-tenant surface of the system, and they are listed here so
that surface can be reviewed in one place rather than discovered.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class TenantScope:
    """The tenant a set of queries is confined to.

    `project_id` narrows further, for the ingest plane where a key is pinned
    to one project. A workspace-level caller leaves it None; a repository that
    needs a project asks for it explicitly rather than treating None as "all".
    """

    workspace_id: UUID
    project_id: UUID | None = None

    def with_project(self, project_id: UUID) -> TenantScope:
        return TenantScope(workspace_id=self.workspace_id, project_id=project_id)

    def require_project(self) -> UUID:
        if self.project_id is None:
            raise ValueError("This operation needs a project-scoped TenantScope")
        return self.project_id


class ScopedRepository:
    """Base for repositories that may only ever see one workspace."""

    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope


class GlobalRepository:
    """Base for the few repositories that answer "which tenant is this?".

    Subclassing this is a declaration that the queries inside cross tenant
    boundaries on purpose. There are three subclasses, and there is no reason
    for a fourth that is not also a change to how authentication works.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

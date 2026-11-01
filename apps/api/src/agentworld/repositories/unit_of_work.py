"""The unit of work: one transaction, one visibility set, all repositories.

A service never sees a session. It is handed a factory, opens a unit of work
over the workspaces the call is entitled to, and asks that unit for
repositories. Three things fall out of that:

- **The whole call commits or none of it does.** A project and the audit row
  describing its creation are written in one transaction, so there is no state
  in which the log claims something that did not happen, or something happened
  that the log missed.
- **Services need no SQL.** The import-linter contract "Services orchestrate;
  only repositories speak SQL" is checkable because this type is the only
  thing a service imports to reach the database.
- **The visibility set is chosen once, explicitly, per call.** It is an
  argument to `begin`, so a reader can see what a service is entitled to see
  at the top of the method rather than inferring it from the queries.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agentworld.infra.db import Database
from agentworld.repositories.api_keys import ApiKeyRepository, ApiKeyUnscopedRepository
from agentworld.repositories.audit import AuditLogRepository
from agentworld.repositories.base import TenantScope
from agentworld.repositories.definitions import (
    AgentRepository,
    ModelProfileRepository,
)
from agentworld.repositories.derived import DerivedStateRepository
from agentworld.repositories.events import EventRepository, TelemetryRepository
from agentworld.repositories.projects import ProjectLookupRepository, ProjectRepository
from agentworld.repositories.users import (
    MemberListRepository,
    MembershipRepository,
    UserRepository,
)
from agentworld.repositories.workspaces import WorkspaceRepository


class UnitOfWork:
    """Repository access within one transaction.

    The global repositories are properties - there is only ever one of each.
    The scoped ones are methods taking a `TenantScope`, because that argument
    is not optional and a property could not demand it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """Escape hatch for the integration tests only.

        Nothing in `src/` uses this. It exists so a test can assert what is
        actually in the database - including rows row-level security would
        hide from the repository it is testing - without opening a second
        connection whose transaction would not see uncommitted work.
        """
        return self._session

    # Global: these answer "which tenant is this?", so they cannot be scoped.
    @property
    def users(self) -> UserRepository:
        return UserRepository(self._session)

    @property
    def memberships(self) -> MembershipRepository:
        return MembershipRepository(self._session)

    @property
    def workspaces(self) -> WorkspaceRepository:
        return WorkspaceRepository(self._session)

    @property
    def keys_unscoped(self) -> ApiKeyUnscopedRepository:
        """The unscoped `api_keys` surface - four methods, each justified in
        `repositories.api_keys`."""
        return ApiKeyUnscopedRepository(self._session)

    @property
    def project_lookup(self) -> ProjectLookupRepository:
        """Unscoped by construction, confined by RLS - see its docstring."""
        return ProjectLookupRepository(self._session)

    # Scoped: a TenantScope is required to construct any of these.
    def members(self, scope: TenantScope) -> MemberListRepository:
        return MemberListRepository(self._session, scope)

    def projects(self, scope: TenantScope) -> ProjectRepository:
        return ProjectRepository(self._session, scope)

    def api_keys(self, scope: TenantScope) -> ApiKeyRepository:
        return ApiKeyRepository(self._session, scope)

    def audit(self, scope: TenantScope) -> AuditLogRepository:
        return AuditLogRepository(self._session, scope)

    def agents(self, scope: TenantScope) -> AgentRepository:
        """Definition, not derived state - see `repositories.definitions`."""
        return AgentRepository(self._session, scope)

    def model_profiles(self, scope: TenantScope) -> ModelProfileRepository:
        return ModelProfileRepository(self._session, scope)

    def events(self, scope: TenantScope) -> EventRepository:
        """Appending events. Needs `scope.project_id`, and says so."""
        return EventRepository(self._session, scope)

    def telemetry(self, scope: TenantScope) -> TelemetryRepository:
        """Logs and metric samples, which are not events - see its docstring."""
        return TelemetryRepository(self._session, scope)

    def derived(self, scope: TenantScope) -> DerivedStateRepository:
        """The tables a folded event may change - and no others."""
        return DerivedStateRepository(self._session, scope)


class UnitOfWorkFactory:
    """Opens units of work against one database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def begin(
        self, *, visible_workspaces: Collection[UUID] | None = None
    ) -> AsyncIterator[UnitOfWork]:
        """Open a transaction that can see exactly these workspaces.

        Committed on a clean exit, rolled back on an exception - including a
        `DomainError`, so a request that fails authorization halfway through
        leaves nothing behind.

        Omitting `visible_workspaces` is not a shortcut for "everything": the
        row-level security policies then match nothing. Services that need no
        tenant-scoped table (resolving a principal, verifying a key) use that
        deliberately.
        """
        async with self._database.session(visible_workspaces=visible_workspaces) as session:
            yield UnitOfWork(session)

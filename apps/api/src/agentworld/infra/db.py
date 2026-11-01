"""Database engine, sessions, and the tenant session variable.

Every session this module hands out is already inside a transaction with
`agentworld.workspace_ids` set to the workspaces the caller can see. That
setting is what the row-level security policies in migration `0004_rls` read,
and it is written with PostgreSQL's `set_config(..., is_local => true)`, so it
is scoped to the transaction and cannot survive back into the connection pool
and be read by the next request to borrow that connection.

**Why a set rather than one workspace id.** The obvious design is a single
`agentworld.workspace_id`. It does not survive contact with the API: routes
like `GET /v1/projects/{id}` are given a project id and nothing else, so the
workspace is not known until `projects` has been read - and under RLS that
read returns nothing, because the setting it depends on has not been written
yet. Every way out of that circle except this one involves a bypass: a
`SECURITY DEFINER` resolver, a second role with `BYPASSRLS`, or a policy that
opens up when the setting is empty. Each is a backdoor that one forgotten call
re-opens.

Keying the policies on the caller's *visibility set* - the workspaces their
credential actually grants - dissolves the circle instead. The resolving read
is itself covered by the policy, a foreign project id matches no row and
becomes the documented 404, and no bypass exists to forget. A repository query
that omits its tenant filter can then at worst reach across workspaces the
caller already belongs to, which the application-layer `TenantScope` closes
and which is a far smaller hole than a cross-tenant one.

**Why `set_config` and not `SET LOCAL`.** `SET` takes a literal, not a bind
parameter, so the ids would have to be interpolated into SQL text. A function
call takes a real parameter. That is the difference between a parameterised
query and string concatenation on the one value that decides which tenants'
data a request can see.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

#: The PostgreSQL run-time parameter the RLS policies read. A custom GUC must
#: contain a dot, which is also why it reads as a namespace.
TENANT_SETTING = "agentworld.workspace_ids"

_SET_TENANT = text(f"SELECT set_config('{TENANT_SETTING}', :workspace_ids, true)")
_READ_TENANT = text(f"SELECT current_setting('{TENANT_SETTING}', true)")


class Database:
    """Owns the connection pool for the process.

    Constructed once in the app's lifespan and disposed on shutdown. Tests
    construct their own against a throwaway database.
    """

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 5) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            pool_size=pool_size,
            max_overflow=pool_size * 2,
            # A connection killed by a restart or a network blip otherwise
            # surfaces as a failed request rather than a reconnect.
            pool_pre_ping=True,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> bool:
        """One round trip, for `/readyz`. Never raises."""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    @asynccontextmanager
    async def session(
        self, *, visible_workspaces: Collection[UUID] | None = None
    ) -> AsyncIterator[AsyncSession]:
        """A transactional session that can see exactly these workspaces.

        The default - no workspaces - makes every RLS policy match nothing,
        which is the correct starting point: a request that has not yet
        established whose data it is entitled to should be able to read none
        of it. The two queries that legitimately precede tenant resolution
        (a user's own memberships, a key looked up by its prefix) run against
        tables that carry no policy, and say so where they are written.
        """
        async with self._sessionmaker() as session, session.begin():
            await session.execute(_SET_TENANT, {"workspace_ids": _render(visible_workspaces)})
            yield session


async def current_tenant_setting(session: AsyncSession) -> str:
    """Read the tenant setting back, for the RLS tests and for diagnostics."""
    result = await session.execute(_READ_TENANT)
    return str(result.scalar_one() or "")


def _render(workspace_ids: Collection[UUID] | None) -> str:
    """Comma-separated ids, or "" for none.

    The policies wrap this in `NULLIF(..., '')` before casting, so an empty
    string becomes SQL NULL rather than a cast error, and `= ANY (NULL)` is
    NULL, which RLS treats as "no". Failing closed is the whole point.
    """
    if not workspace_ids:
        return ""
    return ",".join(sorted(str(workspace_id) for workspace_id in workspace_ids))

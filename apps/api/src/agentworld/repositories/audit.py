"""The audit log.

Append-only, and not by convention: migration `0005` grants the application
role SELECT and INSERT on this table and nothing else, so there is no UPDATE
or DELETE for a future code path to reach for. This repository has no method
for either because the database would refuse one.

Tenant-scoped, and covered by row-level security, which has a consequence
worth knowing: writing the audit row for `workspace.create` requires the new
workspace to already be in the session's visibility set. That is why
`WorkspaceRepository.create` accepts the id rather than letting the database
generate it - the service knows the id before the first statement runs, and
opens its session with that id visible.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentworld.repositories import models as m
from agentworld.repositories.base import ScopedRepository


class AuditLogRepository(ScopedRepository):
    async def append(
        self,
        *,
        actor_type: str,
        action: str,
        resource_type: str,
        actor_id: UUID | None = None,
        resource_id: UUID | None = None,
        request_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> int:
        """Record one mutating call, returning the row id.

        `request_id` is the same value as the response's `X-Request-Id` and a
        problem document's `instance`, which is what lets a user's bug report
        be joined to the record of what their request actually did.

        No commit here. The audit row is written inside the same transaction
        as the change it describes, so a rolled-back mutation leaves no claim
        that it happened, and a successful one cannot fail to be recorded.
        """
        result = await self._session.execute(
            sa.insert(m.AuditLogEntry)
            .values(
                workspace_id=self.scope.workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                metadata_=dict(metadata or {}),
                ip=ip,
                user_agent=user_agent,
            )
            .returning(m.AuditLogEntry.id)
        )
        return int(result.scalar_one())

    async def recent(self, *, limit: int = 50) -> list[tuple[int, str, str]]:
        """The latest entries as (id, action, resource_type).

        Deliberately thin: the audit log gets a proper paginated read in Phase
        3 alongside the other list endpoints. This exists so the Phase 1 tests
        can assert that a mutation wrote its row, rather than reaching past
        the repository into raw SQL to find out.
        """
        rows = await self._session.execute(
            sa.select(m.AuditLogEntry.id, m.AuditLogEntry.action, m.AuditLogEntry.resource_type)
            .where(m.AuditLogEntry.workspace_id == self.scope.workspace_id)
            .order_by(m.AuditLogEntry.id.desc())
            .limit(limit)
        )
        return [(int(row[0]), str(row[1]), str(row[2])) for row in rows.all()]

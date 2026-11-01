"""API keys.

Split into two classes on purpose.

`ApiKeyUnscopedRepository` is the entire unscoped surface of this table, and
every method on it is listed here so the surface can be reviewed rather than
discovered:

- `by_prefix` - a key is a *credential*, so looking it up is how a request
  learns which tenant it belongs to. It cannot presuppose the answer. This is
  also why `api_keys` carries no row-level security policy (migration
  `0004_rls`): a policy would need the workspace this query exists to find.
- `replace_hash` and `touch_last_used` - both run immediately after
  `by_prefix`, in the same request, before any tenant scope exists, and both
  take the row id that lookup just returned.
- `workspace_of` - `docs/03-API.md` addresses a key as `/v1/api-keys/{id}`
  with no workspace in the path, so the workspace has to be resolved before
  it can be authorized. It returns an id and nothing else, and the caller's
  very next statement is `authorize`, which 404s when the caller is not a
  member. A key id reveals nothing to someone who cannot guess a uuid4.

`ApiKeyRepository` is tenant-scoped and does everything else - listing,
minting, revoking - with an explicit `workspace_id` predicate on every
statement.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa

from agentworld.domain.entities import ApiKey
from agentworld.domain.enums import ApiKeyScope
from agentworld.repositories import models as m
from agentworld.repositories.base import GlobalRepository, ScopedRepository


@dataclass(frozen=True, slots=True)
class KeyVerificationRow:
    """Everything needed to verify a presented key, and nothing more.

    This is the only type in the codebase with a field for the secret hash.
    It exists for the length of one verification, is never returned from a
    service, and has no route that serialises it - which is what keeps the
    hash out of logs and responses structurally rather than by care.
    """

    id: UUID
    workspace_id: UUID
    project_id: UUID | None
    secret_hash: str
    scope: ApiKeyScope
    agent_key_allowlist: list[str] | None
    rate_limit_rps: int
    #: Evaluated by the database's clock, not this process's. A server whose
    #: clock has drifted must not be able to accept an expired credential.
    is_expired: bool
    is_revoked: bool


class ApiKeyUnscopedRepository(GlobalRepository):
    """The unscoped surface of `api_keys`. See the module docstring."""

    async def by_prefix(self, prefix: str) -> KeyVerificationRow | None:
        """Look up a presented key.

        Revoked and expired keys are returned rather than filtered out. The
        caller still has to verify the secret before deciding anything, and
        rejecting on the secret first means a revoked key and a forged one
        take the same path - no timing or behavioural difference that would
        tell an attacker a prefix was once real.
        """
        row = (
            await self._session.execute(
                sa.select(
                    m.ApiKey.id,
                    m.ApiKey.workspace_id,
                    m.ApiKey.project_id,
                    m.ApiKey.secret_hash,
                    m.ApiKey.scope,
                    m.ApiKey.agent_key_allowlist,
                    m.ApiKey.rate_limit_rps,
                    sa.and_(
                        m.ApiKey.expires_at.is_not(None),
                        m.ApiKey.expires_at <= sa.func.now(),
                    ).label("is_expired"),
                    m.ApiKey.revoked_at.is_not(None).label("is_revoked"),
                ).where(m.ApiKey.prefix == prefix)
            )
        ).one_or_none()
        if row is None:
            return None
        return KeyVerificationRow(
            id=row.id,
            workspace_id=row.workspace_id,
            project_id=row.project_id,
            secret_hash=row.secret_hash,
            scope=row.scope,
            agent_key_allowlist=list(row.agent_key_allowlist)
            if row.agent_key_allowlist is not None
            else None,
            rate_limit_rps=row.rate_limit_rps,
            is_expired=bool(row.is_expired),
            is_revoked=bool(row.is_revoked),
        )

    async def replace_hash(self, api_key_id: UUID, secret_hash: str) -> None:
        """Upgrade a hash produced with weaker Argon2id parameters.

        Also global: it runs immediately after `by_prefix`, in the same
        request, before any tenant scope has been established.
        """
        await self._session.execute(
            sa.update(m.ApiKey).where(m.ApiKey.id == api_key_id).values(secret_hash=secret_hash)
        )

    async def workspace_of(self, api_key_id: UUID) -> UUID | None:
        """Which workspace a key belongs to, for the routes addressed by id.

        Unlike `ProjectLookupRepository.workspace_of`, this one is not
        confined by row-level security - `api_keys` has no policy - so the
        caller is responsible for authorizing the workspace it returns. Every
        call site does that on the following line.
        """
        return (
            await self._session.execute(
                sa.select(m.ApiKey.workspace_id).where(m.ApiKey.id == api_key_id)
            )
        ).scalar_one_or_none()

    async def touch_last_used(self, api_key_id: UUID) -> None:
        """Record that the key was used.

        Not awaited on the hot path by callers that cannot afford it; Phase 2
        batches this behind the ingest write rather than paying a round trip
        per event. For now it is one cheap UPDATE per authenticated request.
        """
        await self._session.execute(
            sa.update(m.ApiKey).where(m.ApiKey.id == api_key_id).values(last_used_at=sa.func.now())
        )


class ApiKeyRepository(ScopedRepository):
    """Management of keys within one workspace."""

    async def create(
        self,
        *,
        name: str,
        prefix: str,
        secret_hash: str,
        scope: ApiKeyScope,
        created_by: UUID,
        project_id: UUID | None = None,
        agent_key_allowlist: Sequence[str] | None = None,
        rate_limit_rps: int = 50,
        expires_at: dt.datetime | None = None,
    ) -> ApiKey:
        row = (
            await self._session.execute(
                sa.insert(m.ApiKey)
                .values(
                    workspace_id=self.scope.workspace_id,
                    project_id=project_id,
                    name=name,
                    prefix=prefix,
                    secret_hash=secret_hash,
                    scope=scope,
                    agent_key_allowlist=list(agent_key_allowlist)
                    if agent_key_allowlist is not None
                    else None,
                    rate_limit_rps=rate_limit_rps,
                    created_by=created_by,
                    expires_at=expires_at,
                )
                .returning(m.ApiKey)
            )
        ).scalar_one()
        return _to_api_key(row)

    async def list_all(self, *, include_revoked: bool = False) -> Sequence[ApiKey]:
        conditions = [m.ApiKey.workspace_id == self.scope.workspace_id]
        if not include_revoked:
            conditions.append(m.ApiKey.revoked_at.is_(None))
        if self.scope.project_id is not None:
            conditions.append(m.ApiKey.project_id == self.scope.project_id)

        rows = await self._session.execute(
            sa.select(m.ApiKey).where(*conditions).order_by(m.ApiKey.created_at.desc())
        )
        return [_to_api_key(row) for row in rows.scalars().all()]

    async def get(self, api_key_id: UUID) -> ApiKey | None:
        row = (
            await self._session.execute(
                sa.select(m.ApiKey).where(
                    m.ApiKey.id == api_key_id,
                    m.ApiKey.workspace_id == self.scope.workspace_id,
                )
            )
        ).scalar_one_or_none()
        return _to_api_key(row) if row is not None else None

    async def revoke(self, api_key_id: UUID) -> ApiKey | None:
        """Mark a key unusable, keeping the row.

        Revocation rather than deletion is the default because the row is
        evidence: `audit_log` references it, and "which key did that" is the
        first question after an incident. `delete` exists for keys minted by
        mistake and never used.
        """
        row = (
            await self._session.execute(
                sa.update(m.ApiKey)
                .where(
                    m.ApiKey.id == api_key_id,
                    m.ApiKey.workspace_id == self.scope.workspace_id,
                    m.ApiKey.revoked_at.is_(None),
                )
                .values(revoked_at=sa.func.now())
                .returning(m.ApiKey)
            )
        ).scalar_one_or_none()
        return _to_api_key(row) if row is not None else None

    async def delete(self, api_key_id: UUID) -> bool:
        deleted = (
            await self._session.execute(
                sa.delete(m.ApiKey)
                .where(
                    m.ApiKey.id == api_key_id,
                    m.ApiKey.workspace_id == self.scope.workspace_id,
                )
                .returning(m.ApiKey.id)
            )
        ).scalar_one_or_none()
        return deleted is not None


def _to_api_key(row: m.ApiKey) -> ApiKey:
    """Convert to the entity, which has no field for the hash."""
    return ApiKey(
        id=row.id,
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        name=row.name,
        prefix=row.prefix,
        scope=row.scope,
        agent_key_allowlist=list(row.agent_key_allowlist)
        if row.agent_key_allowlist is not None
        else None,
        rate_limit_rps=row.rate_limit_rps,
        created_by=row.created_by,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )

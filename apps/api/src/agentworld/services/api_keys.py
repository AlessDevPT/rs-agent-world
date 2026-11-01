"""API keys: minting them, and authenticating with them.

Two halves that must not be confused. `mint`, `list_in`, `revoke` and
`delete` are control-plane management, reached with a user's JWT.
`authenticate` is the ingest plane's front door, reached with the key itself.
They live in one module because they share the key format, and nothing else.

The plaintext key exists in exactly one place in the system's lifetime: the
return value of `mint`. It is not stored, not logged, and not recoverable -
the database holds an Argon2id hash of a peppered secret, and that is the only
copy. Losing it means minting a new key, which is the correct and intended
consequence.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID

from agentworld.domain import keys
from agentworld.domain.entities import ApiKey
from agentworld.domain.enums import ApiKeyScope
from agentworld.domain.errors import (
    AuthenticationFailedError,
    NotFoundError,
    ValidationFailedError,
)
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import KeyPrincipal, UserPrincipal
from agentworld.infra import hashing
from agentworld.infra.logging import get_logger
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services import audit
from agentworld.services.context import CallContext

CHALLENGE = 'AgentWorldKey realm="ingest"'

_GENERIC_FAILURE = "The credential presented could not be verified"

#: Argon2id hash of nothing in particular, used to burn the same CPU on an
#: unknown prefix as on a real one. Without it an unknown prefix answers in
#: microseconds and a real one in milliseconds, which tells an attacker which
#: prefixes exist. Computed once at import.
_DUMMY_HASH = hashing.hash_secret("not-a-real-secret", pepper="timing-equaliser")

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MintedApiKey:
    """A new key and its plaintext, together, once.

    The route serialises `plaintext` into the 201 response and then this
    object is discarded. There is no second chance to read it, which is why
    `docs/03-API.md` section 3.7 marks the field as appearing exactly once.
    """

    api_key: ApiKey
    plaintext: str


class ApiKeyService:
    def __init__(self, factory: UnitOfWorkFactory, *, pepper: str, environment: str) -> None:
        self._factory = factory
        self._pepper = pepper
        # Keys minted against a deployed environment are labelled `live`,
        # everything else `test`, and `authenticate` refuses a label that does
        # not match. A development key pasted into production then fails
        # loudly instead of quietly working.
        self._label: keys.KeyEnvironment = (
            "live" if environment in ("staging", "production") else "test"
        )

    # --------------------------------------------------------- control plane ---

    async def mint(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        *,
        name: str,
        scope: ApiKeyScope,
        project_id: UUID | None = None,
        agent_key_allowlist: Sequence[str] | None = None,
        rate_limit_rps: int = 50,
        expires_at: dt.datetime | None = None,
    ) -> MintedApiKey:
        authorize(principal, Action.API_KEY_CREATE, Resource(workspace_id))

        if scope is ApiKeyScope.INGEST and project_id is None:
            # Also a check constraint in the database. Enforced here too so
            # the caller gets a 422 explaining the rule rather than a 500 from
            # a constraint violation.
            raise ValidationFailedError(
                "An ingest key must be pinned to a project", scope=scope.value
            )
        if expires_at is not None and expires_at <= dt.datetime.now(dt.UTC):
            raise ValidationFailedError(
                "expires_at is in the past", expires_at=expires_at.isoformat()
            )
        allowlist = _validated_allowlist(agent_key_allowlist)

        minted = keys.mint(self._label)
        secret_hash = hashing.hash_secret(minted.secret, pepper=self._pepper)

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            if project_id is not None:
                project = await work.projects(TenantScope(workspace_id)).get(project_id)
                if project is None:
                    raise NotFoundError("Project not found", project_id=str(project_id))

            api_key = await work.api_keys(TenantScope(workspace_id)).create(
                name=name,
                prefix=minted.prefix,
                secret_hash=secret_hash,
                scope=scope,
                created_by=principal.user_id,
                project_id=project_id,
                agent_key_allowlist=allowlist,
                rate_limit_rps=rate_limit_rps,
                expires_at=expires_at,
            )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="api_key.create",
                resource_type="api_key",
                resource_id=api_key.id,
                # The prefix, never the secret. It is the public handle and
                # the thing an operator needs in order to revoke the right key.
                metadata={"prefix": api_key.prefix, "scope": scope.value},
            )
            return MintedApiKey(api_key=api_key, plaintext=minted.plaintext)

    async def list_in(
        self, principal: UserPrincipal, workspace_id: UUID, *, include_revoked: bool = False
    ) -> Sequence[ApiKey]:
        authorize(principal, Action.API_KEY_READ, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            return await work.api_keys(TenantScope(workspace_id)).list_all(
                include_revoked=include_revoked
            )

    async def revoke(
        self, principal: UserPrincipal, context: CallContext, api_key_id: UUID
    ) -> ApiKey:
        """Revoke a key addressed by its own id.

        `docs/03-API.md` puts no workspace in this path, so the workspace is
        resolved first and authorized immediately after. `api_keys` carries no
        row-level security policy, so unlike the project routes the membership
        check here is doing real work rather than confirming what the database
        already enforced - which is why it is the very next line.
        """
        workspace_id = await self._workspace_of(api_key_id)
        authorize(principal, Action.API_KEY_REVOKE, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            api_key = await work.api_keys(TenantScope(workspace_id)).revoke(api_key_id)
            if api_key is None:
                # Either no such key, or it is already revoked. Revoking a
                # revoked key is not worth distinguishing: the caller's intent
                # is satisfied either way, and a 404 says "this key is not
                # going to work", which is true.
                raise NotFoundError("API key not found", api_key_id=str(api_key_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="api_key.revoke",
                resource_type="api_key",
                resource_id=api_key_id,
                metadata={"prefix": api_key.prefix},
            )
            return api_key

    async def delete(
        self, principal: UserPrincipal, context: CallContext, api_key_id: UUID
    ) -> None:
        """Remove the row entirely.

        Prefer `revoke`: the row is evidence, and `audit_log` refers to it.
        This exists for a key minted by mistake, and the audit row recording
        the deletion outlives the key it describes.
        """
        workspace_id = await self._workspace_of(api_key_id)
        authorize(principal, Action.API_KEY_DELETE, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            repository = work.api_keys(TenantScope(workspace_id))
            api_key = await repository.get(api_key_id)
            if api_key is None:
                raise NotFoundError("API key not found", api_key_id=str(api_key_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="api_key.delete",
                resource_type="api_key",
                resource_id=api_key_id,
                metadata={"prefix": api_key.prefix},
            )
            await repository.delete(api_key_id)

    async def _workspace_of(self, api_key_id: UUID) -> UUID:
        """Resolve a key id to its workspace, for the routes addressed by id.

        Raises the same `NotFoundError` for "no such key" as `authorize` does
        for "not your workspace", so the two are indistinguishable from
        outside.
        """
        async with self._factory.begin() as work:
            workspace_id = await work.keys_unscoped.workspace_of(api_key_id)
        if workspace_id is None:
            raise NotFoundError("API key not found", api_key_id=str(api_key_id))
        return workspace_id

    # ---------------------------------------------------------- ingest plane ---

    async def authenticate(self, presented: str) -> KeyPrincipal:
        """Verify a presented key and return what it is allowed to be.

        Every failure raises the same error with the same message. The reason
        goes to the log, never to the caller: a credential endpoint that
        explains itself is an oracle for enumerating valid prefixes, live
        keys and expiry dates.

        The work is done in a fixed order so that the cost does not vary with
        how wrong the key is - in particular an unknown prefix still pays for
        one Argon2id verification against `_DUMMY_HASH`.
        """
        parsed = keys.parse(presented)
        if parsed is None:
            self._reject("malformed")
        if parsed.environment != self._label:
            # A `test` key in production, or the reverse. Refused rather than
            # ignored: the two environments should never share credentials,
            # and silently accepting one hides a misconfiguration until it
            # matters.
            self._reject("wrong_environment", presented_label=parsed.environment)

        async with self._factory.begin() as work:
            # No visibility set: `api_keys` carries no RLS policy, because
            # this lookup is how the ingest plane discovers its tenant. See
            # migration 0004 and `ApiKeyUnscopedRepository`.
            row = await work.keys_unscoped.by_prefix(parsed.prefix)

            if row is None:
                hashing.verify_secret(parsed.secret, hashed=_DUMMY_HASH, pepper="timing-equaliser")
                self._reject("unknown_prefix")

            if not hashing.verify_secret(
                parsed.secret, hashed=row.secret_hash, pepper=self._pepper
            ):
                self._reject("bad_secret", api_key_id=str(row.id))
            # Checked only after the secret, so that holding a valid secret is
            # a precondition for learning anything at all - including whether
            # the key was revoked or has expired.
            if row.is_revoked:
                self._reject("revoked", api_key_id=str(row.id))
            if row.is_expired:
                self._reject("expired", api_key_id=str(row.id))

            if hashing.needs_rehash(row.secret_hash):
                # The only moment the plaintext is in hand, so the only moment
                # an upgrade to stronger parameters is possible.
                await work.keys_unscoped.replace_hash(
                    row.id, hashing.hash_secret(parsed.secret, pepper=self._pepper)
                )
            await work.keys_unscoped.touch_last_used(row.id)

            return KeyPrincipal(
                api_key_id=row.id,
                workspace_id=row.workspace_id,
                project_id=row.project_id,
                scope=row.scope,
                agent_key_allowlist=row.agent_key_allowlist,
                rate_limit_rps=row.rate_limit_rps,
            )

    def _reject(self, reason: str, **context: str) -> NoReturn:
        log.info("api_key_rejected", reason=reason, **context)
        raise AuthenticationFailedError(_GENERIC_FAILURE, challenge=CHALLENGE, reason=reason)


def _validated_allowlist(patterns: Sequence[str] | None) -> list[str] | None:
    """Check allowlist entries before they become a security control.

    A pattern with a `*` anywhere but the end would look like a wildcard and
    behave like a literal, which is the worst possible failure mode for an
    allowlist: it appears to permit a family of agents and in fact permits
    none, or appears to narrow and in fact does not.

    A bare `"*"` is allowed. It reads as "any agent" and behaves as one - a
    prefix match on the empty string - so it is honest, if redundant. The
    rule is about patterns that mislead, not about patterns that are broad.
    """
    if patterns is None:
        return None
    cleaned: list[str] = []
    for pattern in patterns:
        candidate = pattern.strip()
        if not candidate:
            raise ValidationFailedError("An allowlist entry cannot be empty")
        if "*" in candidate.removesuffix("*"):
            raise ValidationFailedError(
                f"'{candidate}' is not a supported pattern: '*' is allowed only "
                "as the last character",
                pattern=candidate,
            )
        cleaned.append(candidate)
    if not cleaned:
        # An empty list would mean "no agent may use this key", which is a
        # key nobody can use. A caller who wants "any agent" omits the field.
        raise ValidationFailedError(
            "An empty allowlist would permit no agent at all; omit the field to permit any"
        )
    return cleaned

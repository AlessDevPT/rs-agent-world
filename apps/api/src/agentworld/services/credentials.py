"""Credentials: storing material nobody can read back, and using it once.

Five management operations reachable over HTTP, and one that is not.

`use` is the only path that decrypts, and it takes no principal - because
there is no principal. It is called by the dispatch worker immediately before
an outbound call, on behalf of an execution whose authority was decided when
the execution was started, not when the credential was read. Giving it a
principal would invite a route to pass one in, and no route should exist: the
permission matrix has `credential.read_metadata` and no
`credential.read_secret`, at any role, for anybody.

What keeps the material out of everything else is structural rather than
careful:

- the entity has no field for it, so no response model or log line can carry
  one;
- `repositories.credentials.material` is the only query that selects the
  ciphertext, and it returns an `EncryptedBlob`, which only `infra/secrets/`
  consumes;
- the plaintext exists as a local in `use` and nowhere else - not in an
  entity, not in a dispatch's `instruction`, not in an event payload, not in
  an audit row's metadata;
- no error raised here puts the material in its context, so no problem
  document can reflect it back.

`display_hint` is the one exception, and it is derived here rather than
accepted from the caller. A caller-supplied hint is a field through which a
client could put the entire secret into a listing endpoint, which is the
opposite of what the column is for.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from agentworld.domain.entities import Credential
from agentworld.domain.enums import CredentialKind
from agentworld.domain.errors import (
    ConflictError,
    CredentialUnreadableError,
    NotFoundError,
    ValidationFailedError,
)
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.logging import get_logger
from agentworld.infra.secrets import SecretStore, SecretUnreadableError
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services import audit
from agentworld.services.context import CallContext

#: Below this, no hint is derived at all. Four characters out of twenty is a
#: fifth of the material, and a fifth of a short secret is a real
#: contribution to guessing it. Every provider whose keys this is for issues
#: far longer than twenty characters, so the floor costs nothing in practice
#: and bounds the disclosure when something unexpected is stored.
_MIN_LENGTH_FOR_A_HINT = 20

#: How many trailing characters a hint shows. The same four that every
#: provider dashboard shows, which is the point: the hint's job is to let an
#: operator match a row here against a key listed there.
_HINT_SUFFIX = 4

#: The maximum material this will store. Not a security boundary - it is
#: there so a mistaken file upload fails as a 422 rather than as a row that
#: works and is 40 MB of ciphertext.
MAX_SECRET_BYTES = 8 * 1024

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RewrapSummary:
    """What one KEK rotation pass over one workspace did.

    `unreadable` is not an error to raise on: a row whose KEK has already
    been retired cannot be re-wrapped, and stopping the pass at the first one
    would mean a single unrecoverable row blocks every other row from ever
    moving off the old key. It is reported instead, loudly, as a count the
    operator has to account for.
    """

    examined: int
    rewrapped: int
    unreadable: int


class CredentialService:
    def __init__(self, factory: UnitOfWorkFactory, *, store: SecretStore) -> None:
        self._factory = factory
        self._store = store

    # ------------------------------------------------------------ management ---

    async def list_in(
        self, principal: UserPrincipal, workspace_id: UUID, *, include_revoked: bool = False
    ) -> Sequence[Credential]:
        authorize(principal, Action.CREDENTIAL_READ_METADATA, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            return await work.credentials(TenantScope(workspace_id)).list_all(
                include_revoked=include_revoked
            )

    async def create(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        *,
        name: str,
        kind: CredentialKind,
        provider_slug: str,
        secret: str,
        project_id: UUID | None = None,
        scopes: Sequence[str] | None = None,
        expires_at: dt.datetime | None = None,
        refresh_after: dt.datetime | None = None,
    ) -> Credential:
        """Store material under a new data key.

        The id is minted here rather than by the database, because the
        additional authenticated data binds the ciphertext to it: the row's
        identity has to exist before its material can be encrypted. That
        ordering is the reason a credential cannot be created by an
        `INSERT ... SELECT` from another workspace's row and then read.
        """
        authorize(principal, Action.CREDENTIAL_CREATE, Resource(workspace_id))
        material = _validated(secret)

        credential_id = uuid4()
        blob = await self._store.encrypt(
            material, workspace_id=workspace_id, credential_id=credential_id
        )

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            scope = TenantScope(workspace_id)
            credentials = work.credentials(scope)
            if project_id is not None and await work.projects(scope).get(project_id) is None:
                raise NotFoundError("Project not found", project_id=str(project_id))
            if await credentials.name_taken(provider_slug=provider_slug, name=name):
                raise ConflictError(
                    f"This workspace already has a '{provider_slug}' credential called '{name}'",
                    name=name,
                    provider_slug=provider_slug,
                )
            credential = await credentials.create(
                credential_id=credential_id,
                name=name,
                kind=kind,
                provider_slug=provider_slug,
                blob=blob,
                created_by_user_id=principal.user_id,
                project_id=project_id,
                display_hint=_display_hint(secret, kind),
                scopes=scopes,
                expires_at=expires_at,
                refresh_after=refresh_after,
            )
            await _record(
                work,
                principal,
                context,
                workspace_id,
                "credential.create",
                credential_id,
                # Which provider and which KEK, so an incident can ask "what
                # was wired to this, and is it on the retired key?". Nothing
                # derived from the material, not even the hint.
                {"provider_slug": provider_slug, "kind": kind.value, "key_id": blob.key_id},
            )
            return credential

    async def rotate(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        credential_id: UUID,
        *,
        secret: str,
        expires_at: dt.datetime | None = None,
        refresh_after: dt.datetime | None = None,
    ) -> Credential:
        """Replace the material, keeping the row.

        The id survives, so every model profile and connection pointing at
        this credential keeps working. That is the whole reason this is not
        "delete and create": rotating a provider key should not require
        re-wiring everything that used it.
        """
        authorize(principal, Action.CREDENTIAL_ROTATE, Resource(workspace_id))
        material = _validated(secret)

        blob = await self._store.encrypt(
            material, workspace_id=workspace_id, credential_id=credential_id
        )

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            credentials = work.credentials(TenantScope(workspace_id))
            existing = await credentials.get(credential_id)
            if existing is None:
                raise NotFoundError("Credential not found", credential_id=str(credential_id))
            rotated = await credentials.replace_material(
                credential_id,
                blob=blob,
                display_hint=_display_hint(secret, existing.kind),
                expires_at=expires_at,
                refresh_after=refresh_after,
            )
            if rotated is None:  # pragma: no cover - the get above just proved it exists
                raise NotFoundError("Credential not found", credential_id=str(credential_id))
            await _record(
                work,
                principal,
                context,
                workspace_id,
                "credential.rotate",
                credential_id,
                {"provider_slug": rotated.provider_slug, "key_id": blob.key_id},
            )
            return rotated

    async def revoke(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        credential_id: UUID,
    ) -> Credential:
        """Stop the credential being usable, keeping its material.

        Reversible by rotation, which is the operational answer to revoking
        the wrong row. `delete` is the irreversible one.
        """
        authorize(principal, Action.CREDENTIAL_REVOKE, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            credentials = work.credentials(TenantScope(workspace_id))
            revoked = await credentials.revoke(credential_id)
            if revoked is None:
                # No such credential, or already revoked. Not worth
                # distinguishing: the caller's intent is satisfied either
                # way, and a 404 correctly says "this is not going to work".
                raise NotFoundError("Credential not found", credential_id=str(credential_id))
            await _record(
                work,
                principal,
                context,
                workspace_id,
                "credential.revoke",
                credential_id,
                {"provider_slug": revoked.provider_slug},
            )
            return revoked

    async def delete(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        credential_id: UUID,
    ) -> None:
        """Destroy the material; keep the row as a tombstone.

        Irreversible. After this the secret cannot be recovered by anyone
        holding the database, the key-encryption key, or both. The row
        survives because `audit_log.resource_id` points at it, and an audit
        trail with dangling references answers nothing.
        """
        authorize(principal, Action.CREDENTIAL_DELETE, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            credentials = work.credentials(TenantScope(workspace_id))
            erased = await credentials.erase(credential_id)
            if erased is None:
                raise NotFoundError("Credential not found", credential_id=str(credential_id))
            await _record(
                work,
                principal,
                context,
                workspace_id,
                "credential.delete",
                credential_id,
                {"provider_slug": erased.provider_slug},
            )

    # ---------------------------------------------------------- key rotation ---

    async def rewrap_workspace(self, principal: UserPrincipal, workspace_id: UUID) -> RewrapSummary:
        """Move every data key in one workspace onto the active KEK.

        No plaintext is involved: a data key is unwrapped and wrapped again,
        and `ciphertext` is not in the statement that writes the result. So
        this is safe to run online, over and over, and against a workspace
        whose rows are already on the active key - where it writes nothing.

        Per workspace rather than across the whole table, deliberately. A
        global pass would need an unscoped repository, and
        `repositories.base` is explicit that the three cross-tenant lookups
        that exist are the entire cross-tenant surface. An operator rotating
        a KEK iterates workspaces; the cost of that is one transaction each,
        and the benefit is that the table worth the most to an attacker has
        no query that can see all of it at once.

        There is no HTTP route for this. Rotation is an operator action with
        no useful latency budget and no UI, and an endpoint that re-wraps
        every key in a workspace is a thing an attacker would like to call.
        """
        authorize(principal, Action.CREDENTIAL_ROTATE, Resource(workspace_id))

        examined = rewrapped = unreadable = 0
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            credentials = work.credentials(TenantScope(workspace_id))
            for credential_id, blob in await credentials.wrapped_keys():
                examined += 1
                try:
                    moved = await self._store.rewrap(
                        blob, workspace_id=workspace_id, credential_id=credential_id
                    )
                except SecretUnreadableError as exc:
                    # Most likely a row wrapped by a KEK that has already
                    # been retired. Counted and logged rather than raised:
                    # one unrecoverable row must not stop every other row
                    # from moving off the old key.
                    unreadable += 1
                    log.error(
                        "credential_rewrap_failed",
                        credential_id=str(credential_id),
                        key_id=blob.key_id,
                        reason=exc.reason,
                    )
                    continue
                if moved.key_id == blob.key_id:
                    continue
                await credentials.rewrap(
                    credential_id, wrapped_key=moved.wrapped_key, key_id=moved.key_id
                )
                rewrapped += 1
        return RewrapSummary(examined=examined, rewrapped=rewrapped, unreadable=unreadable)

    # ------------------------------------------------------- the point of use ---

    async def use(self, *, workspace_id: UUID, credential_id: UUID) -> str:
        """Decrypt one credential, for one outbound call, right now.

        Takes no principal on purpose - see the module docstring. Reachable
        from no route, and the return value belongs in the arguments of a
        request that is about to be made and nowhere else: not in an entity,
        not in a dispatch instruction, not in an event payload, not in a
        prompt.

        A revoked, deleted or missing credential is one `NotFoundError`,
        because an outbound call has no use for the difference and an
        endpoint that explained it would be a probe.
        """
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            credentials = work.credentials(TenantScope(workspace_id))
            blob = await credentials.material(credential_id)
            if blob is None:
                raise NotFoundError("Credential not found", credential_id=str(credential_id))
            try:
                plaintext = await self._store.decrypt(
                    blob, workspace_id=workspace_id, credential_id=credential_id
                )
            except SecretUnreadableError as exc:
                # The row exists and this server cannot read it: a retired
                # KEK, a restore that moved the row, or tampering. A server
                # fault, not a caller fault, and `reason` goes to the log
                # only.
                log.error(
                    "credential_unreadable",
                    credential_id=str(credential_id),
                    key_id=blob.key_id,
                    reason=exc.reason,
                )
                raise CredentialUnreadableError(
                    "The stored credential could not be decrypted on this server",
                    credential_id=str(credential_id),
                ) from None
            await credentials.touch_last_used(credential_id)
            return plaintext.decode("utf-8")


async def _record(
    work: UnitOfWork,
    principal: UserPrincipal,
    context: CallContext,
    workspace_id: UUID,
    action: str,
    credential_id: UUID,
    metadata: Mapping[str, Any],
) -> None:
    """One audit row, in the caller's transaction.

    A thin wrapper only because every call here passes the same
    `resource_type`, and a mistyped one would file a credential event under
    the wrong resource in the trail that exists to find it.
    """
    await audit.record(
        work,
        principal,
        context,
        workspace_id=workspace_id,
        action=action,
        resource_type="credential",
        resource_id=credential_id,
        metadata=metadata,
    )


def _validated(secret: str) -> bytes:
    """Check the material before encrypting it, saying nothing about it.

    Every message here describes the rule, never the value. A validator whose
    message quoted what it rejected would put the secret in a 422 problem
    document, which is the one place it must never be - and would do it only
    on the requests that went wrong, which is exactly when nobody is looking.
    """
    if not secret:
        raise ValidationFailedError("A credential cannot be empty", field="secret")
    encoded = secret.encode("utf-8")
    if len(encoded) > MAX_SECRET_BYTES:
        raise ValidationFailedError(
            f"Credential material is limited to {MAX_SECRET_BYTES} bytes", field="secret"
        )
    return encoded


def _display_hint(secret: str, kind: CredentialKind) -> str | None:
    """The few characters an operator may see, or nothing.

    Derived only for `api_key` material, and that restriction is the point.
    An API key is a single opaque high-entropy token, and showing its last
    four characters is what every provider's own dashboard does - which is
    what makes the hint useful: it is how a row here is matched against a key
    listed there.

    Every other kind is refused a hint rather than given a worse one.
    `basic` material is or contains a human-chosen password, where four
    trailing characters are a real contribution to guessing it. `oauth2`
    material is a token set whose trailing characters belong to whichever
    field happens to be serialised last, so the hint would be arbitrary as
    well as revealing. `custom` is by definition unknown. None of those is
    worth four characters of a secret.
    """
    if kind is not CredentialKind.API_KEY:
        return None
    material = secret.strip()
    if len(material) < _MIN_LENGTH_FOR_A_HINT:
        return None
    return f"…{material[-_HINT_SUFFIX:]}"

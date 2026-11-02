"""Credentials: the only module that reads the four material columns.

Tenant-scoped, with no unscoped counterpart. `ApiKeyRepository` needs one
because looking up a key is how a request discovers its tenant; a credential
is never presented to this server, so nothing about it has to be resolved
before the workspace is known. That is why the routes are nested under the
workspace rather than addressed as `/v1/credentials/{id}`: the id-addressed
shape would need a fourth cross-tenant lookup, and `repositories.base` is
explicit that the three that exist are the whole cross-tenant surface.

`material` is the one method that returns ciphertext, and it returns it as an
`EncryptedBlob` whose only consumer is `infra/secrets/`. Everything else here
returns the `Credential` entity, which has no field material could occupy.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentworld.domain.entities import Credential
from agentworld.domain.enums import CredentialKind
from agentworld.infra.secrets import EncryptedBlob
from agentworld.repositories import models as m
from agentworld.repositories.base import ScopedRepository

#: The columns a tombstone keeps and the columns it destroys. Deleting a
#: credential overwrites the material in place rather than removing the row,
#: because `audit_log.resource_id` points at it and a dangling reference in
#: an audit trail is worse than a tombstone. Zero-length bytes rather than
#: NULL: the columns are NOT NULL, and a zero-length ciphertext cannot be
#: mistaken for one that might still decrypt.
_ERASED: dict[str, Any] = {
    "ciphertext": b"",
    "nonce": b"",
    "wrapped_key": b"",
    "display_hint": None,
}


class CredentialRepository(ScopedRepository):
    """Credentials within one workspace."""

    async def create(
        self,
        *,
        credential_id: UUID,
        name: str,
        kind: CredentialKind,
        provider_slug: str,
        blob: EncryptedBlob,
        created_by_user_id: UUID,
        project_id: UUID | None = None,
        display_hint: str | None = None,
        scopes: Sequence[str] | None = None,
        expires_at: dt.datetime | None = None,
        refresh_after: dt.datetime | None = None,
    ) -> Credential:
        """Insert a row whose id was chosen before the ciphertext existed.

        `credential_id` is an argument rather than a database default, and
        that is forced by the design: the GCM additional authenticated data
        binds the ciphertext to the row's id, so the id has to be known
        before the material can be encrypted. The column keeps its
        `gen_random_uuid()` default for anything that inserts without
        material, and this path always overrides it.
        """
        row = (
            await self._session.execute(
                sa.insert(m.Credential)
                .values(
                    id=credential_id,
                    workspace_id=self.scope.workspace_id,
                    project_id=project_id,
                    name=name,
                    kind=kind,
                    provider_slug=provider_slug,
                    ciphertext=blob.ciphertext,
                    nonce=blob.nonce,
                    wrapped_key=blob.wrapped_key,
                    key_id=blob.key_id,
                    aad_version=blob.aad_version,
                    display_hint=display_hint,
                    scopes=list(scopes) if scopes is not None else None,
                    expires_at=expires_at,
                    refresh_after=refresh_after,
                    created_by_user_id=created_by_user_id,
                )
                .returning(m.Credential)
            )
        ).scalar_one()
        return _to_credential(row)

    async def list_all(self, *, include_revoked: bool = False) -> Sequence[Credential]:
        """Every live credential in the workspace, newest first.

        Deleted rows are never returned: the tombstone exists for the audit
        log's sake, not for an operator's listing, and its material is gone.
        A project-scoped `TenantScope` narrows to credentials for that
        project plus the workspace-wide ones, because a project's outbound
        call may legitimately use either.
        """
        conditions = [
            m.Credential.workspace_id == self.scope.workspace_id,
            m.Credential.deleted_at.is_(None),
        ]
        if not include_revoked:
            conditions.append(m.Credential.revoked_at.is_(None))
        if self.scope.project_id is not None:
            conditions.append(
                sa.or_(
                    m.Credential.project_id == self.scope.project_id,
                    m.Credential.project_id.is_(None),
                )
            )
        rows = await self._session.execute(
            sa.select(m.Credential).where(*conditions).order_by(m.Credential.created_at.desc())
        )
        return [_to_credential(row) for row in rows.scalars().all()]

    async def get(self, credential_id: UUID) -> Credential | None:
        row = await self._row(credential_id)
        return _to_credential(row) if row is not None else None

    async def name_taken(self, *, provider_slug: str, name: str) -> bool:
        """Whether `credentials_name_key` would refuse this pair.

        The unique constraint spans deleted rows too, because a tombstone
        keeps its name. Checked here so the caller gets a 409 that says which
        name, rather than a 500 from a constraint violation.
        """
        found = (
            await self._session.execute(
                sa.select(m.Credential.id).where(
                    m.Credential.workspace_id == self.scope.workspace_id,
                    m.Credential.provider_slug == provider_slug,
                    m.Credential.name == name,
                )
            )
        ).first()
        return found is not None

    async def material(self, credential_id: UUID) -> EncryptedBlob | None:
        """The encrypted material, for `infra/secrets/` and nothing else.

        Returns None for a row that is missing, invisible to this tenant,
        revoked or deleted - all four being cases where there is nothing a
        caller should be able to use. The caller cannot tell them apart,
        which is deliberate: "that credential was revoked last week" is not
        information an outbound call needs and not information an attacker
        should be able to probe for.
        """
        row = (
            await self._session.execute(
                sa.select(
                    m.Credential.ciphertext,
                    m.Credential.nonce,
                    m.Credential.wrapped_key,
                    m.Credential.key_id,
                    m.Credential.aad_version,
                ).where(
                    m.Credential.id == credential_id,
                    m.Credential.workspace_id == self.scope.workspace_id,
                    m.Credential.revoked_at.is_(None),
                    m.Credential.deleted_at.is_(None),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return EncryptedBlob(
            ciphertext=row.ciphertext,
            nonce=row.nonce,
            wrapped_key=row.wrapped_key,
            key_id=row.key_id,
            aad_version=row.aad_version,
        )

    async def wrapped_keys(self) -> Sequence[tuple[UUID, EncryptedBlob]]:
        """Every live row's material, for a KEK rotation pass.

        Includes revoked rows - a revoked credential's row is still readable
        evidence and its DEK still has to move to the new KEK or the old key
        can never be retired - and excludes tombstones, whose material is
        already gone.
        """
        rows = await self._session.execute(
            sa.select(
                m.Credential.id,
                m.Credential.ciphertext,
                m.Credential.nonce,
                m.Credential.wrapped_key,
                m.Credential.key_id,
                m.Credential.aad_version,
            )
            .where(
                m.Credential.workspace_id == self.scope.workspace_id,
                m.Credential.deleted_at.is_(None),
            )
            .order_by(m.Credential.created_at)
        )
        return [
            (
                row.id,
                EncryptedBlob(
                    ciphertext=row.ciphertext,
                    nonce=row.nonce,
                    wrapped_key=row.wrapped_key,
                    key_id=row.key_id,
                    aad_version=row.aad_version,
                ),
            )
            for row in rows.all()
        ]

    async def rewrap(self, credential_id: UUID, *, wrapped_key: bytes, key_id: str) -> bool:
        """Point a row at a new KEK without touching its ciphertext.

        The statement names two columns and no others, which is what makes
        "a rotation cannot corrupt a secret" a property of the SQL rather
        than of the caller's care.
        """
        updated = (
            await self._session.execute(
                sa.update(m.Credential)
                .where(
                    m.Credential.id == credential_id,
                    m.Credential.workspace_id == self.scope.workspace_id,
                    m.Credential.deleted_at.is_(None),
                )
                .values(wrapped_key=wrapped_key, key_id=key_id)
                .returning(m.Credential.id)
            )
        ).scalar_one_or_none()
        return updated is not None

    async def replace_material(
        self,
        credential_id: UUID,
        *,
        blob: EncryptedBlob,
        display_hint: str | None,
        expires_at: dt.datetime | None = None,
        refresh_after: dt.datetime | None = None,
    ) -> Credential | None:
        """New material for an existing credential: a user-supplied rotation.

        A new data key and a new ciphertext replace the old pair. The row id
        is unchanged, so every model profile and connection that names this
        credential keeps working - which is the whole reason rotation is not
        "delete and create".

        `revoked_at` is not in the value list, so rotating a revoked
        credential leaves it revoked. Clearing it here would make rotation
        quietly undo a revocation, which is not what either verb meant; an
        operator who wants it back says so with a separate call.
        """
        row = (
            await self._session.execute(
                sa.update(m.Credential)
                .where(
                    m.Credential.id == credential_id,
                    m.Credential.workspace_id == self.scope.workspace_id,
                    m.Credential.deleted_at.is_(None),
                )
                .values(
                    ciphertext=blob.ciphertext,
                    nonce=blob.nonce,
                    wrapped_key=blob.wrapped_key,
                    key_id=blob.key_id,
                    aad_version=blob.aad_version,
                    display_hint=display_hint,
                    expires_at=expires_at,
                    refresh_after=refresh_after,
                )
                .returning(m.Credential)
            )
        ).scalar_one_or_none()
        return _to_credential(row) if row is not None else None

    async def revoke(self, credential_id: UUID) -> Credential | None:
        """Mark a credential unusable, keeping both the row and its material.

        The material stays because revocation is reversible in the only way
        that matters operationally: an operator who revoked the wrong row
        rotates it rather than losing the integration. What stops it being
        used is `material`, which filters on `revoked_at`.
        """
        row = (
            await self._session.execute(
                sa.update(m.Credential)
                .where(
                    m.Credential.id == credential_id,
                    m.Credential.workspace_id == self.scope.workspace_id,
                    m.Credential.revoked_at.is_(None),
                    m.Credential.deleted_at.is_(None),
                )
                .values(revoked_at=sa.func.now())
                .returning(m.Credential)
            )
        ).scalar_one_or_none()
        return _to_credential(row) if row is not None else None

    async def erase(self, credential_id: UUID) -> Credential | None:
        """Destroy the material and tombstone the row.

        Irreversible, and the stronger of the two removals: after this the
        secret is not recoverable by anyone holding the database, the KEK, or
        both. The row survives so the audit trail still resolves.
        """
        row = (
            await self._session.execute(
                sa.update(m.Credential)
                .where(
                    m.Credential.id == credential_id,
                    m.Credential.workspace_id == self.scope.workspace_id,
                    m.Credential.deleted_at.is_(None),
                )
                .values(deleted_at=sa.func.now(), **_ERASED)
                .returning(m.Credential)
            )
        ).scalar_one_or_none()
        return _to_credential(row) if row is not None else None

    async def touch_last_used(self, credential_id: UUID) -> None:
        """Record that an outbound call used this credential.

        Separate from the decryption it follows so that the read path can be
        made read-only later without changing its caller. "When was this key
        last used" is the first question when deciding whether a credential
        can be retired.
        """
        await self._session.execute(
            sa.update(m.Credential)
            .where(
                m.Credential.id == credential_id,
                m.Credential.workspace_id == self.scope.workspace_id,
            )
            .values(last_used_at=sa.func.now())
        )

    async def _row(self, credential_id: UUID) -> m.Credential | None:
        return (
            await self._session.execute(
                sa.select(m.Credential).where(
                    m.Credential.id == credential_id,
                    m.Credential.workspace_id == self.scope.workspace_id,
                    m.Credential.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


def _to_credential(row: m.Credential) -> Credential:
    """Convert to the entity, which has no field for the material."""
    return Credential(
        id=row.id,
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        name=row.name,
        kind=row.kind,
        provider_slug=row.provider_slug,
        key_id=row.key_id,
        display_hint=row.display_hint,
        scopes=list(row.scopes) if row.scopes is not None else None,
        expires_at=row.expires_at,
        refresh_after=row.refresh_after,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        deleted_at=row.deleted_at,
    )

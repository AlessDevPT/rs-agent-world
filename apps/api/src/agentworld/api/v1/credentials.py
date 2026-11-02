"""Credentials over HTTP: five ways in, no way out.

Every route here is `admin`, and there is no route that returns material -
not restricted to owners, absent. The permission matrix has no
`credential.read_secret` action for one to call, `CredentialView` has no
field for it to put material in, and the entity it is built from has no
attribute for the view to read. Three separate absences, because this is the
table worth the most to an attacker.

**Everything is nested under the workspace**, including the item routes,
where `docs/proposals/.../03-API-AND-FRONTEND.md` originally wrote
`DELETE /v1/credentials/{id}`. The id-addressed shape has to resolve a
credential id to a workspace *before* it can authorize the caller, and that
lookup is by definition cross-tenant - a fourth entry on the list of three
cross-tenant queries that `repositories.base` keeps deliberately short. The
workspace is already in the UI's hands at every point it would call these, so
the nesting costs nothing and removes the only reason for that lookup to
exist.

**`display_hint` is not an input.** `CredentialCreate` forbids unknown fields
so that sending one is a 422 rather than a silently ignored field a client
believes it set. The hint is derived from the material by the service, which
is the only way to be sure the hint is not itself the secret.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from agentworld.api.deps import CallContextDep, CredentialServiceDep, CurrentUser
from agentworld.domain.entities import Credential
from agentworld.domain.enums import CredentialKind
from agentworld.services.credentials import MAX_SECRET_BYTES

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/credentials", tags=["credentials"])

Name = Annotated[str, Field(min_length=1, max_length=200)]
ProviderSlug = Annotated[str, Field(min_length=1, max_length=100)]

#: `repr=False` keeps the material out of a model's `repr`, which is what a
#: traceback, a debugger frame and a careless log line all reach for. The
#: service never logs the model, so this is defence in depth - the cheap
#: kind, since the field is write-only anyway.
Secret = Annotated[str, Field(min_length=1, max_length=MAX_SECRET_BYTES, repr=False)]


class CredentialView(BaseModel):
    """What an operator needs to recognise and audit a credential.

    `display_hint`, `scopes`, `expires_at` and `last_used_at` - enough to
    tell two keys for the same provider apart, to see which is about to
    expire, and to see which is no longer used. Nothing that could be
    replayed against the provider.

    `key_id` is in here because it is operational metadata, not material: it
    names a key-encryption key held in the environment, and an operator
    halfway through a rotation needs to see which rows are still on the old
    one.
    """

    id: UUID
    workspace_id: UUID
    project_id: UUID | None = None
    name: str
    kind: CredentialKind
    provider_slug: str
    key_id: str
    display_hint: str | None = None
    scopes: list[str] | None = None
    expires_at: dt.datetime | None = None
    refresh_after: dt.datetime | None = None
    created_at: dt.datetime | None = None
    last_used_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    is_usable: bool

    @classmethod
    def of(cls, credential: Credential) -> CredentialView:
        return cls(
            id=credential.id,
            workspace_id=credential.workspace_id,
            project_id=credential.project_id,
            name=credential.name,
            kind=credential.kind,
            provider_slug=credential.provider_slug,
            key_id=credential.key_id,
            display_hint=credential.display_hint,
            scopes=list(credential.scopes) if credential.scopes is not None else None,
            expires_at=credential.expires_at,
            refresh_after=credential.refresh_after,
            created_at=credential.created_at,
            last_used_at=credential.last_used_at,
            revoked_at=credential.revoked_at,
            is_usable=credential.is_usable,
        )


class CredentialCreate(BaseModel):
    """New credential material, plus how to recognise it later.

    `extra="forbid"` rather than the default: a client that sends
    `display_hint` has misunderstood something worth being told about, and a
    client that sends `ciphertext` is doing something that must not look
    like it worked.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name
    kind: CredentialKind
    provider_slug: ProviderSlug
    secret: Secret = Field(
        description=(
            "The material to store. Write-only: no endpoint returns it, and there is no "
            "permission at any role that would."
        )
    )
    project_id: UUID | None = Field(
        default=None,
        description="Omit for a workspace-wide credential, which is the usual case.",
    )
    scopes: list[str] | None = None
    expires_at: dt.datetime | None = None
    refresh_after: dt.datetime | None = None


class CredentialRotate(BaseModel):
    """Replacement material for an existing credential."""

    model_config = ConfigDict(extra="forbid")

    secret: Secret
    expires_at: dt.datetime | None = None
    refresh_after: dt.datetime | None = None


@router.get(
    "",
    response_model=list[CredentialView],
    summary="Credentials in a workspace",
    description="Metadata only. There is no endpoint that returns stored material.",
)
async def list_credentials(
    workspace_id: UUID,
    principal: CurrentUser,
    service: CredentialServiceDep,
    include_revoked: bool = Query(
        default=False, description="Include credentials that have been revoked."
    ),
) -> list[CredentialView]:
    credentials = await service.list_in(principal, workspace_id, include_revoked=include_revoked)
    return [CredentialView.of(credential) for credential in credentials]


@router.post(
    "",
    response_model=CredentialView,
    status_code=status.HTTP_201_CREATED,
    summary="Store a credential",
    description=(
        "The material is encrypted under a data key of its own before the row is written. "
        "The 201 carries metadata and a derived hint; it does not echo what was sent."
    ),
)
async def create_credential(
    workspace_id: UUID,
    body: CredentialCreate,
    principal: CurrentUser,
    context: CallContextDep,
    service: CredentialServiceDep,
) -> CredentialView:
    credential = await service.create(
        principal,
        context,
        workspace_id,
        name=body.name,
        kind=body.kind,
        provider_slug=body.provider_slug,
        secret=body.secret,
        project_id=body.project_id,
        scopes=body.scopes,
        expires_at=body.expires_at,
        refresh_after=body.refresh_after,
    )
    return CredentialView.of(credential)


@router.post(
    "/{credential_id}/rotate",
    response_model=CredentialView,
    summary="Replace a credential's material",
    description=(
        "Keeps the row and its id, so everything wired to this credential keeps working. "
        "A revoked credential stays revoked."
    ),
)
async def rotate_credential(
    workspace_id: UUID,
    credential_id: UUID,
    body: CredentialRotate,
    principal: CurrentUser,
    context: CallContextDep,
    service: CredentialServiceDep,
) -> CredentialView:
    credential = await service.rotate(
        principal,
        context,
        workspace_id,
        credential_id,
        secret=body.secret,
        expires_at=body.expires_at,
        refresh_after=body.refresh_after,
    )
    return CredentialView.of(credential)


@router.post(
    "/{credential_id}/revoke",
    response_model=CredentialView,
    summary="Revoke a credential",
    description=(
        "Takes effect immediately: nothing can use it again. The material is kept, so "
        "revoking the wrong row is undone by rotating it rather than by losing it."
    ),
)
async def revoke_credential(
    workspace_id: UUID,
    credential_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: CredentialServiceDep,
) -> CredentialView:
    credential = await service.revoke(principal, context, workspace_id, credential_id)
    return CredentialView.of(credential)


@router.delete(
    "/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Destroy a credential's material",
    description=(
        "Irreversible. The row survives as a tombstone because the audit trail points at "
        "it; the material is overwritten and is not recoverable with the key or the "
        "database or both."
    ),
)
async def delete_credential(
    workspace_id: UUID,
    credential_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: CredentialServiceDep,
) -> Response:
    await service.delete(principal, context, workspace_id, credential_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

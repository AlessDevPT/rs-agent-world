"""API keys.

Minting is nested under the workspace that will own the key; revoking and
deleting address the key by its own id, as `docs/03-API.md` section 3.7
specifies.

The 201 response from `POST` is the only time the plaintext key exists in a
response body, or anywhere outside the minting call. There is no endpoint that
returns it again, because there is nothing left to return it from: the
database holds an Argon2id hash of a peppered secret and the server cannot
reverse it.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from agentworld.api.deps import ApiKeyServiceDep, CallContextDep, CurrentUser
from agentworld.api.schemas.tenancy import ApiKeyCreate, ApiKeyCreated, ApiKeyOut

#: Collection routes, nested under the owning workspace.
workspace_router = APIRouter(prefix="/v1/workspaces", tags=["api-keys"])

#: Item routes, addressed by key id.
router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


@workspace_router.get(
    "/{workspace_id}/api-keys",
    response_model=list[ApiKeyOut],
    summary="API keys in a workspace",
    description="Prefixes only. The secret half is not stored and cannot be listed.",
)
async def list_api_keys(
    workspace_id: UUID,
    principal: CurrentUser,
    service: ApiKeyServiceDep,
    include_revoked: bool = Query(
        default=False, description="Include keys that have already been revoked."
    ),
) -> list[ApiKeyOut]:
    api_keys = await service.list_in(principal, workspace_id, include_revoked=include_revoked)
    return [ApiKeyOut.model_validate(key) for key in api_keys]


@workspace_router.post(
    "/{workspace_id}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key",
    description=(
        "The response carries the full key in its `key` field. This is the only time "
        "it is ever available: capture it here or mint another."
    ),
)
async def create_api_key(
    workspace_id: UUID,
    body: ApiKeyCreate,
    principal: CurrentUser,
    context: CallContextDep,
    service: ApiKeyServiceDep,
) -> ApiKeyCreated:
    minted = await service.mint(
        principal,
        context,
        workspace_id,
        name=body.name,
        scope=body.scope,
        project_id=body.project_id,
        agent_key_allowlist=body.agent_key_allowlist,
        rate_limit_rps=body.rate_limit_rps,
        expires_at=body.expires_at,
    )
    return ApiKeyCreated.model_validate(
        {**ApiKeyOut.model_validate(minted.api_key).model_dump(), "key": minted.plaintext}
    )


@router.post(
    "/{api_key_id}/revoke",
    response_model=ApiKeyOut,
    summary="Revoke an API key",
    description=(
        "Takes effect immediately. The row is kept, because `audit_log` refers to it "
        "and 'which key did that' is the first question after an incident."
    ),
)
async def revoke_api_key(
    api_key_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: ApiKeyServiceDep,
) -> ApiKeyOut:
    return ApiKeyOut.model_validate(await service.revoke(principal, context, api_key_id))


@router.delete(
    "/{api_key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an API key",
    description="Prefer revoking. This removes the row, for a key minted by mistake.",
)
async def delete_api_key(
    api_key_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: ApiKeyServiceDep,
) -> Response:
    await service.delete(principal, context, api_key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

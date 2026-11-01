"""Workspaces and their members.

The routes are thin by design: validate the body, call one service method,
shape the response. Every authorization decision, every conflict rule and
every audit row is in `services.workspaces`, so that reading the service
tells you the whole behaviour and reading the routes tells you the whole
surface.

Path parameters are named `workspace_id` rather than the `{id}` the spec
writes, which is a documentation placeholder and not part of the wire
contract. `id` would also shadow the builtin, which the linter rightly
objects to.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from agentworld.api.deps import CallContextDep, CurrentUser, WorkspaceServiceDep
from agentworld.api.schemas.tenancy import (
    MemberAdd,
    MemberOut,
    MemberRolePatch,
    WorkspaceCreate,
    WorkspaceMembershipOut,
    WorkspaceOut,
    WorkspacePatch,
)
from agentworld.domain.entities import MembershipView

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


@router.get(
    "",
    response_model=list[WorkspaceMembershipOut],
    summary="Workspaces you belong to",
)
async def list_workspaces(
    principal: CurrentUser, service: WorkspaceServiceDep
) -> list[WorkspaceMembershipOut]:
    memberships = await service.list_for(principal)
    return [WorkspaceMembershipOut.model_validate(m) for m in memberships]


@router.post(
    "",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workspace",
    description="The caller becomes its owner. Any authenticated user may create one.",
)
async def create_workspace(
    body: WorkspaceCreate,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkspaceServiceDep,
) -> WorkspaceOut:
    workspace = await service.create(principal, context, name=body.name, slug=body.slug)
    return WorkspaceOut.model_validate(workspace)


@router.get("/{workspace_id}", response_model=WorkspaceOut, summary="One workspace")
async def get_workspace(
    workspace_id: UUID, principal: CurrentUser, service: WorkspaceServiceDep
) -> WorkspaceOut:
    return WorkspaceOut.model_validate(await service.get(principal, workspace_id))


@router.patch("/{workspace_id}", response_model=WorkspaceOut, summary="Rename a workspace")
async def patch_workspace(
    workspace_id: UUID,
    body: WorkspacePatch,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkspaceServiceDep,
) -> WorkspaceOut:
    workspace = await service.update(
        principal, context, workspace_id, name=body.name, slug=body.slug
    )
    return WorkspaceOut.model_validate(workspace)


@router.delete(
    "/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a workspace",
    description=(
        "A soft delete: the workspace stops being visible immediately and is purged "
        "after a grace period. Owner only."
    ),
)
async def delete_workspace(
    workspace_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkspaceServiceDep,
) -> Response:
    await service.archive(principal, context, workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------- members ---


@router.get("/{workspace_id}/members", response_model=list[MemberOut], summary="Workspace members")
async def list_members(
    workspace_id: UUID, principal: CurrentUser, service: WorkspaceServiceDep
) -> list[MemberOut]:
    members = await service.list_members(principal, workspace_id)
    return [_member(view) for view in members]


@router.post(
    "/{workspace_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a member",
)
async def add_member(
    workspace_id: UUID,
    body: MemberAdd,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkspaceServiceDep,
) -> MemberOut:
    view = await service.add_member(
        principal, context, workspace_id, email=str(body.email), role=body.role
    )
    return _member(view)


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=MemberOut,
    summary="Change a member's role",
    description=(
        "Setting the role to 'owner' transfers ownership: the acting owner becomes an "
        "admin in the same transaction, because a workspace has exactly one owner."
    ),
)
async def patch_member(
    workspace_id: UUID,
    user_id: UUID,
    body: MemberRolePatch,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkspaceServiceDep,
) -> MemberOut:
    view = await service.set_member_role(principal, context, workspace_id, user_id, role=body.role)
    return _member(view)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member, or leave",
    description="Admins may remove anyone but the owner; any member may remove themselves.",
)
async def delete_member(
    workspace_id: UUID,
    user_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkspaceServiceDep,
) -> Response:
    await service.remove_member(principal, context, workspace_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _member(view: MembershipView) -> MemberOut:
    return MemberOut.model_validate(
        {
            "user": view.user,
            "role": view.membership.role,
            "joined_at": view.membership.joined_at,
        }
    )

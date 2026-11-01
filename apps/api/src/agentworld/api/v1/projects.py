"""Projects.

Two prefixes, as `docs/03-API.md` section 3.3 specifies: collection routes
hang off the workspace that owns them, and single-project routes are addressed
by project id alone. The second shape is why `ProjectService` resolves the
workspace before authorizing - see its module docstring.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from agentworld.api.deps import CallContextDep, CurrentUser, ProjectServiceDep
from agentworld.api.schemas.tenancy import ProjectCreate, ProjectOut, ProjectPatch

#: Collection routes, nested under their workspace.
workspace_router = APIRouter(prefix="/v1/workspaces", tags=["projects"])

#: Item routes, addressed by project id with no workspace in the path.
router = APIRouter(prefix="/v1/projects", tags=["projects"])


@workspace_router.get(
    "/{workspace_id}/projects",
    response_model=list[ProjectOut],
    summary="Projects in a workspace",
)
async def list_projects(
    workspace_id: UUID, principal: CurrentUser, service: ProjectServiceDep
) -> list[ProjectOut]:
    projects = await service.list_in(principal, workspace_id)
    return [ProjectOut.model_validate(project) for project in projects]


@workspace_router.post(
    "/{workspace_id}/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create_project(
    workspace_id: UUID,
    body: ProjectCreate,
    principal: CurrentUser,
    context: CallContextDep,
    service: ProjectServiceDep,
) -> ProjectOut:
    project = await service.create(
        principal,
        context,
        workspace_id,
        name=body.name,
        slug=body.slug,
        description=body.description,
        settings=body.settings,
    )
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectOut, summary="One project")
async def get_project(
    project_id: UUID, principal: CurrentUser, service: ProjectServiceDep
) -> ProjectOut:
    return ProjectOut.model_validate(await service.get(principal, project_id))


@router.patch("/{project_id}", response_model=ProjectOut, summary="Update a project")
async def patch_project(
    project_id: UUID,
    body: ProjectPatch,
    principal: CurrentUser,
    context: CallContextDep,
    service: ProjectServiceDep,
) -> ProjectOut:
    project = await service.update(
        principal,
        context,
        project_id,
        name=body.name,
        description=body.description,
        settings=body.settings,
    )
    return ProjectOut.model_validate(project)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a project",
)
async def delete_project(
    project_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: ProjectServiceDep,
) -> Response:
    await service.archive(principal, context, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

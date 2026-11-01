"""Agents and model profiles over HTTP.

Two things about the shapes here are deliberate.

**The response has no `status` you can write back.** `AgentView` carries the
derived fields - status, heartbeat, capabilities - and `AgentUpdate` has no
parameter for any of them, so a client that round-trips a `GET` into a
`PATCH` cannot assert that an agent is working. The absence is the
enforcement; there is no `agent.set_status` action for it to call either.

**One PATCH, two permissions.** Touching the model profile, its overrides or
dispatch additionally needs `agent.configure`, which is `admin`. Splitting
the HTTP surface instead would make the UI choose which call to send and get
it wrong on the request that mixes both.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from agentworld.api.deps import (
    AgentServiceDep,
    CallContextDep,
    CurrentUser,
    ModelProfileServiceDep,
)
from agentworld.domain.entities import Agent, ModelProfile
from agentworld.domain.enums import AgentInteractionMode, AgentStatus
from agentworld.repositories.definitions import UNSET

project_router = APIRouter(prefix="/v1/projects/{project_id}/agents", tags=["agents"])
router = APIRouter(prefix="/v1/agents", tags=["agents"])
profiles_router = APIRouter(prefix="/v1/workspaces/{workspace_id}/model-profiles", tags=["agents"])

Name = Annotated[str, Field(min_length=1, max_length=200)]


# ------------------------------------------------------------------- views ---


class AgentView(BaseModel):
    id: UUID
    project_id: UUID
    agent_key: str
    display_name: str
    interaction_mode: AgentInteractionMode

    role: str | None = None
    description: str | None = None
    instructions: str | None = None
    kind: str | None = None
    labels: dict[str, Any] = Field(default_factory=dict)

    model_profile_id: UUID | None = None
    model_overrides: dict[str, Any] = Field(default_factory=dict)
    dispatch: dict[str, Any] = Field(default_factory=dict)
    heartbeat_ttl_seconds: int = 60

    #: Everything below is folded from events. No request body can set it.
    status: AgentStatus
    status_reason: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    current_task_id: UUID | None = None
    last_heartbeat_at: dt.datetime | None = None
    archived_at: dt.datetime | None = None

    @classmethod
    def of(cls, agent: Agent) -> AgentView:
        return cls(
            id=agent.id,
            project_id=agent.project_id,
            agent_key=agent.agent_key,
            display_name=agent.display_name,
            interaction_mode=agent.interaction_mode,
            role=agent.role,
            description=agent.description,
            instructions=agent.instructions,
            kind=agent.kind,
            labels=dict(agent.labels),
            model_profile_id=agent.model_profile_id,
            model_overrides=dict(agent.model_overrides),
            dispatch=dict(agent.dispatch),
            heartbeat_ttl_seconds=agent.heartbeat_ttl_seconds,
            status=agent.status,
            status_reason=agent.status_reason,
            capabilities=list(agent.capabilities),
            current_task_id=agent.current_task_id,
            last_heartbeat_at=agent.last_heartbeat_at,
            archived_at=agent.archived_at,
        )


class ModelProfileView(BaseModel):
    """No credential material, because there is none to have.

    `credential_id` names a row whose secret nothing in the application can
    read - not this endpoint, not an owner, not a platform operator.
    """

    id: UUID
    name: str
    provider_slug: str
    model_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    credential_id: UUID | None = None
    is_default: bool = False

    @classmethod
    def of(cls, profile: ModelProfile) -> ModelProfileView:
        return cls(
            id=profile.id,
            name=profile.name,
            provider_slug=profile.provider_slug,
            model_id=profile.model_id,
            parameters=dict(profile.parameters),
            credential_id=profile.credential_id,
            is_default=profile.is_default,
        )


# ---------------------------------------------------------------- requests ---


class AgentCreate(BaseModel):
    display_name: Name
    interaction_mode: AgentInteractionMode = Field(
        description="`managed` - AgentWorld runs the loop. `dispatchable` - you run it "
        "and we can hand it work. `observed` - it only reports, and no step may be "
        "assigned to it."
    )
    agent_key: str | None = Field(
        default=None,
        description="Stable identifier the worker uses to identify itself. "
        "Derived from the display name when omitted.",
    )
    role: str | None = None
    description: str | None = None
    instructions: str | None = None
    kind: str | None = None
    labels: dict[str, Any] = Field(default_factory=dict)
    #: Needs `agent.configure` as well.
    model_profile_id: UUID | None = None
    model_overrides: dict[str, Any] = Field(default_factory=dict)
    dispatch: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    """Every field optional, and absent means "leave it alone".

    There is deliberately no field here for status, heartbeat, capabilities
    or current task: those are folded from events, and a client that could
    write them could make the island show something that never happened.
    """

    display_name: Name | None = None
    role: str | None = None
    description: str | None = None
    instructions: str | None = None
    kind: str | None = None
    labels: dict[str, Any] | None = None
    interaction_mode: AgentInteractionMode | None = None
    model_profile_id: UUID | None = None
    model_overrides: dict[str, Any] | None = None
    dispatch: dict[str, Any] | None = None
    heartbeat_ttl_seconds: int | None = Field(default=None, ge=5, le=86_400)

    def sent(self) -> dict[str, Any]:
        """Only the fields the caller actually included.

        `exclude_unset` is what makes "null means clear it" and "absent means
        leave it" different requests rather than the same one.
        """
        return {
            name: (UNSET if name not in self.model_fields_set else value)
            for name, value in self.model_dump().items()
        }


class ModelProfileCreate(BaseModel):
    name: Name
    provider_slug: Name
    model_id: Name
    parameters: dict[str, Any] = Field(default_factory=dict)
    credential_id: UUID | None = None
    is_default: bool = False


# ------------------------------------------------------------------ routes ---


@project_router.get("", response_model=list[AgentView], summary="Agents in a project")
async def list_agents(
    project_id: UUID,
    principal: CurrentUser,
    service: AgentServiceDep,
    include_archived: Annotated[bool, Query()] = False,
) -> list[AgentView]:
    agents = await service.list_in(principal, project_id, include_archived=include_archived)
    return [AgentView.of(agent) for agent in agents]


@project_router.post(
    "",
    response_model=AgentView,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent",
)
async def create_agent(
    project_id: UUID,
    body: AgentCreate,
    principal: CurrentUser,
    context: CallContextDep,
    service: AgentServiceDep,
) -> AgentView:
    agent = await service.create(
        principal,
        context,
        project_id,
        display_name=body.display_name,
        interaction_mode=body.interaction_mode,
        agent_key=body.agent_key,
        role=body.role,
        description=body.description,
        instructions=body.instructions,
        kind=body.kind,
        labels=body.labels,
        model_profile_id=body.model_profile_id,
        model_overrides=body.model_overrides,
        dispatch=body.dispatch,
    )
    return AgentView.of(agent)


@router.get("/{agent_id}", response_model=AgentView, summary="One agent")
async def get_agent(
    agent_id: UUID,
    project_id: Annotated[UUID, Query(description="The project the agent belongs to.")],
    principal: CurrentUser,
    service: AgentServiceDep,
) -> AgentView:
    return AgentView.of(await service.get(principal, project_id, agent_id))


@project_router.patch("/{agent_id}", response_model=AgentView, summary="Edit or configure an agent")
async def update_agent(
    project_id: UUID,
    agent_id: UUID,
    body: AgentUpdate,
    principal: CurrentUser,
    context: CallContextDep,
    service: AgentServiceDep,
) -> AgentView:
    """Editing needs `member`; changing the model or dispatch needs `admin`.

    Both are checked before anything is written, so a request that may change
    the name and may not change the model profile changes neither.
    """
    agent = await service.update(principal, context, project_id, agent_id, **body.sent())
    return AgentView.of(agent)


@project_router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive an agent",
)
async def archive_agent(
    project_id: UUID,
    agent_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: AgentServiceDep,
) -> None:
    """Archived, not deleted.

    The agent keeps its runs, its history and its place on the island, where
    it renders shuttered. Deleting it would rewrite what happened.
    """
    await service.archive(principal, context, project_id, agent_id)


@profiles_router.get("", response_model=list[ModelProfileView], summary="Model profiles")
async def list_model_profiles(
    workspace_id: UUID, principal: CurrentUser, service: ModelProfileServiceDep
) -> list[ModelProfileView]:
    return [ModelProfileView.of(p) for p in await service.list_in(principal, workspace_id)]


@profiles_router.post(
    "",
    response_model=ModelProfileView,
    status_code=status.HTTP_201_CREATED,
    summary="Create a model profile",
)
async def create_model_profile(
    workspace_id: UUID,
    body: ModelProfileCreate,
    principal: CurrentUser,
    context: CallContextDep,
    service: ModelProfileServiceDep,
) -> ModelProfileView:
    """`admin`: a profile names a credential and a spend ceiling."""
    profile = await service.create(
        principal,
        context,
        workspace_id,
        name=body.name,
        provider_slug=body.provider_slug,
        model_id=body.model_id,
        parameters=body.parameters,
        credential_id=body.credential_id,
        is_default=body.is_default,
    )
    return ModelProfileView.of(profile)


@profiles_router.delete(
    "/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a model profile",
)
async def delete_model_profile(
    workspace_id: UUID,
    profile_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: ModelProfileServiceDep,
) -> None:
    await service.delete(principal, context, workspace_id, profile_id)

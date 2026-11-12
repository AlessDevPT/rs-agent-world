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
from pydantic import BaseModel, ConfigDict, Field

from agentworld.api.deps import (
    AgentServiceDep,
    AgentTestServiceDep,
    CallContextDep,
    CurrentUser,
    ExecutionServiceDep,
    ModelProfileServiceDep,
)
from agentworld.api.v1.executions import ExecutionView
from agentworld.domain.entities import Agent, AgentStatusChange, ModelProfile
from agentworld.domain.enums import AgentInteractionMode, AgentStatus
from agentworld.repositories.definitions import UNSET

project_router = APIRouter(prefix="/v1/projects/{project_id}/agents", tags=["agents"])
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


class StatusChangeView(BaseModel):
    """One transition, as the island's timeline reads it.

    Derived from the event log and never written by a request - there is no
    field here a client could send back, and no endpoint that would take
    one.
    """

    from_status: AgentStatus | None = Field(
        default=None,
        description=(
            "The status being left. Rows recorded before stage D1.1 repeat `to_status` "
            "instead: the status patch was folded before the history row was written, so "
            "the row read the value it was moving to. Those rows were left as they are - "
            "the status each one came from cannot be recovered for the oldest transition "
            "of an agent, and guessing the rest would be fabricating history."
        ),
    )
    to_status: AgentStatus
    changed_at: dt.datetime
    reason: str | None = None
    event_id: UUID | None = Field(
        default=None,
        description=(
            "The event this was folded from. A correlation handle for a support "
            "question; no endpoint reads an event by id yet."
        ),
    )

    @classmethod
    def of(cls, change: AgentStatusChange) -> StatusChangeView:
        return cls(
            from_status=change.from_status,
            to_status=change.to_status,
            changed_at=change.changed_at,
            reason=change.reason,
            event_id=change.event_id,
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


class AgentTestRequest(BaseModel):
    """The sample input the agent is given, and nothing else.

    `extra="forbid"`: a client that sends a model, a temperature or a system
    prompt here has misunderstood what a test is. It runs *this* agent as
    configured - that is the only question it answers, and overriding its
    configuration for one run would answer a different one.
    """

    model_config = ConfigDict(extra="forbid")

    input: Annotated[str, Field(min_length=1, max_length=20_000)] = Field(
        description="The sample input. Reaches the agent as its one user message."
    )


class ModelProfileUpdate(BaseModel):
    """What a profile points at. Every field optional, absent means leave it.

    `extra="forbid"`: there is nothing else on a profile a caller may set,
    and a client that sends `workspace_id` or `created_at` has misunderstood
    which of the two of us owns the row.

    An explicit `null` is not the same request as an absent field. Only
    `credential_id` is nullable - clearing it detaches the key and leaves a
    profile a managed agent cannot run on, which is a state `create` can
    produce too - and `null` for any of the others is a 422 naming the
    field rather than an integrity error dressed up as a conflict.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    provider_slug: Name | None = None
    model_id: Name | None = None
    parameters: dict[str, Any] | None = None
    credential_id: UUID | None = Field(
        default=None, description="`null` detaches the credential. Omit to leave it alone."
    )
    is_default: bool | None = None

    def sent(self) -> dict[str, Any]:
        """Only the fields the caller actually included."""
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


@project_router.get("/{agent_id}", response_model=AgentView, summary="One agent")
async def get_agent(
    project_id: UUID,
    agent_id: UUID,
    principal: CurrentUser,
    service: AgentServiceDep,
) -> AgentView:
    """Nested under the project, like every other agent route.

    An earlier draft had this at `/v1/agents/{id}` with the project as a
    query parameter, which made one route in the group shaped unlike the
    rest for no reason a caller could see.
    """
    return AgentView.of(await service.get(principal, project_id, agent_id))


@project_router.get(
    "/{agent_id}/status-history",
    response_model=list[StatusChangeView],
    summary="When this agent's status changed",
    description=(
        "Newest first, folded from the events the agent sent. **An agent AgentWorld runs "
        "itself has none**: a managed agent reports nothing, and its work appears under "
        "`GET /v1/projects/{project_id}/executions` instead - so an empty list here is an "
        "answer about where to look rather than a missing record. Readable for an "
        "archived agent, because what it did before it was archived is the question."
    ),
)
async def agent_status_history(
    project_id: UUID,
    agent_id: UUID,
    principal: CurrentUser,
    service: AgentServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[StatusChangeView]:
    """`viewer`, through `agent.read_history`.

    `limit` is the first one in this API, and status history is why: it grows
    with every transition an agent ever reports, where the other listings are
    bounded by something a person did deliberately.
    """
    changes = await service.status_history(principal, project_id, agent_id, limit=limit)
    return [StatusChangeView.of(change) for change in changes]


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


@project_router.post(
    "/{agent_id}/restore",
    response_model=AgentView,
    summary="Restore an archived agent",
    description=(
        "`admin`, like archiving. The agent returns with the status and heartbeat it had "
        "when it was archived - nothing derived is rewritten, so `last_heartbeat_at` is "
        "how a reader judges whether it is really back. Workflows that name it can run "
        "again; none of them is started. An agent that is not archived is a `404`."
    ),
)
async def restore_agent(
    project_id: UUID,
    agent_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: AgentServiceDep,
) -> AgentView:
    """The inverse of `DELETE`, and an action rather than a field.

    `AgentUpdate` has no `archived_at` for the same reason it has no
    `status`: the lifecycle is verbs, not columns a client can assign.
    """
    return AgentView.of(await service.restore(principal, context, project_id, agent_id))


@project_router.post(
    "/{agent_id}/test",
    response_model=ExecutionView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run this agent once against a sample input",
    description=(
        "A real execution, not a preview: it spends money, it produces the ordinary "
        "events, and it appears in `GET /executions`. 202, not 201, for the same "
        "reason running a workflow is - the execution exists when this returns and "
        "the turn has not happened yet. Read the agent's answer from `output` on "
        "`GET /v1/projects/{project_id}/executions/{execution_id}`; there is no "
        "second result endpoint. The first test of an agent also creates and "
        "publishes the one-step workflow it runs, which later tests reuse."
    ),
)
async def test_agent(
    project_id: UUID,
    agent_id: UUID,
    body: AgentTestRequest,
    principal: CurrentUser,
    context: CallContextDep,
    service: AgentTestServiceDep,
    executions: ExecutionServiceDep,
) -> ExecutionView:
    """Returns the execution, read back through the ordinary execution read.

    Two services, deliberately: the test starts it and `ExecutionService`
    reports it, exactly as `POST .../run` does. A view built here from what
    the service happened to know would be a second description of an
    execution, and the two would drift.
    """
    execution_id = await service.run(principal, context, project_id, agent_id, sample=body.input)
    return ExecutionView.of(await executions.get(principal, project_id, execution_id))


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


@profiles_router.patch(
    "/{profile_id}",
    response_model=ModelProfileView,
    summary="Edit a model profile",
    description=(
        "`admin`. A `credential_id` is resolved in this workspace before it is stored, so "
        "an id from somewhere else is a 422 rather than a profile that fails at dispatch. "
        "No endpoint at any role returns credential material."
    ),
)
async def update_model_profile(
    workspace_id: UUID,
    profile_id: UUID,
    body: ModelProfileUpdate,
    principal: CurrentUser,
    context: CallContextDep,
    service: ModelProfileServiceDep,
) -> ModelProfileView:
    """`admin`: a profile names a credential and a spend ceiling.

    The same permission `create` asks for, unchanged - this endpoint adds a
    verb, not a rule.
    """
    profile = await service.update(principal, context, workspace_id, profile_id, **body.sent())
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

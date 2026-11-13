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
from agentworld.domain.entities import (
    Agent,
    AgentStatusChange,
    ModelProfile,
    Run,
    RunActivity,
)
from agentworld.domain.enums import (
    ActivityEvidence,
    AgentInteractionMode,
    AgentLiveness,
    AgentStatus,
    RunStatus,
)
from agentworld.repositories.definitions import UNSET

project_router = APIRouter(prefix="/v1/projects/{project_id}/agents", tags=["agents"])
profiles_router = APIRouter(prefix="/v1/workspaces/{workspace_id}/model-profiles", tags=["agents"])
#: Project-level rather than nested under an agent, because "what in this
#: project has gone quiet" is not a question about one agent.
runs_router = APIRouter(prefix="/v1/projects/{project_id}/runs", tags=["agents"])

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

    #: Derived at read time rather than folded, so these two are not in the
    #: block above: there is no column behind either, and the answer depends
    #: on the clock at the moment of the request.
    liveness: AgentLiveness = Field(
        description=(
            "Whether this agent's heartbeat is current, judged against its own "
            "`heartbeat_ttl_seconds`. **A claim about the heartbeat and nothing else.** "
            "`stale` does not mean dead or stuck: an agent's beats and its work are "
            "different events, so one that reports runs without heartbeating is `stale` "
            "here while working - see `GET /v1/projects/{project_id}/agents/{agent_id}/runs` "
            "for what it is actually doing. `not_applicable` for a `managed` agent, whose "
            "loop AgentWorld runs itself, and `never_reported` for one that has not "
            "heartbeated at all."
        ),
    )
    seconds_since_heartbeat: int | None = Field(
        default=None,
        description=(
            "Whole seconds of silence, or null if this agent has never heartbeated. "
            "Floored and never negative, and measured against the same instant as "
            "`liveness`, so the two cannot disagree."
        ),
    )

    @classmethod
    def of(cls, agent: Agent, *, now: dt.datetime) -> AgentView:
        """`now` is passed in rather than read here.

        Keyword-only and without a default, so a new call site has to say
        which instant it is judging against instead of silently picking the
        moment this line happened to run. A listing judges every row on it
        against one clock, the way the reconciler computes its cut-off once
        for a whole pass.
        """
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
            liveness=agent.liveness(now),
            seconds_since_heartbeat=agent.seconds_since_heartbeat(now),
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


class RunView(BaseModel):
    """One attempt an external agent reported making at something.

    Not an execution. A run is the agent's own account of work it did
    wherever it lives, folded from the events it sent; an execution is
    AgentWorld's record of work it drove itself. They are different tables
    with different producers, and this API keeps them apart rather than
    merging them into a common "activity" record neither one is.

    No `agent_id`: it is in the path that returned this. No counters a
    client could write back, and no endpoint that would take one - every
    field here is derived, like `StatusChangeView` above.
    """

    id: UUID
    run_ref: str | None = Field(
        default=None, description="The agent's own identifier for this attempt."
    )
    status: RunStatus
    task_id: UUID | None = Field(
        default=None, description="The task this run was working on, when it was working on one."
    )
    execution_step_id: UUID | None = Field(
        default=None,
        description=(
            "Set when this run was one step of a workflow execution rather than the agent "
            "acting on its own."
        ),
    )
    step_count: int = 0
    tool_call_count: int = Field(
        default=0,
        description=(
            "Tool calls this run *invoked*. Refused ones are **not** counted: `tool.denied` "
            "is recorded in the event log and folds into nothing, so an agent whose every "
            "call was blocked reports zero here. The event feed is where those refusals are."
        ),
    )
    error: dict[str, Any] | None = None
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    duration_ms: int | None = Field(
        default=None,
        description="Computed by the database from the two timestamps. Null while in flight.",
    )

    @classmethod
    def of(cls, run: Run) -> RunView:
        return cls(
            id=run.id,
            run_ref=run.run_ref,
            status=run.status,
            task_id=run.task_id,
            execution_step_id=run.execution_step_id,
            step_count=run.step_count,
            tool_call_count=run.tool_call_count,
            error=dict(run.error) if run.error is not None else None,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_ms=run.duration_ms,
        )


class RunActivityView(BaseModel):
    """A run that has reported nothing recently, and what is known about it.

    **Not a verdict, and deliberately not shaped like one.** There is no
    field here that says failed, dead, abandoned or stuck, because nothing
    in this system can know any of those about an agent it does not run. A
    row is an absence of evidence with timestamps attached.
    """

    id: UUID
    run_ref: str | None = Field(
        default=None, description="The agent's own identifier for this attempt."
    )
    agent_id: UUID = Field(description="The agent that reported this run.")
    status: RunStatus
    started_at: dt.datetime
    last_activity_at: dt.datetime = Field(
        description=(
            "The newest timestamp known for this run: its own `run.*` clock, or its "
            "newest step's, whichever is later - falling back to `started_at` when "
            "neither is set. `activity_evidence` says which one it was."
        ),
    )
    seconds_since_activity: int = Field(
        description=(
            "Whole seconds since `last_activity_at`, floored and never negative, measured "
            "against one instant shared by every row in this response."
        ),
    )
    step_count: int = Field(
        default=0,
        description=(
            "Steps *completed*: `run.step_finished` increments it. A run with a step in "
            "flight reports 0 here while `activity_evidence` is already `trace_step`, "
            "because the trace clock moves on `run.step_started` too. Not a contradiction, "
            "and not a count of steps attempted."
        ),
    )
    tool_call_count: int = Field(
        default=0,
        description=(
            "Tool calls *invoked*. Refused ones are not counted - `tool.denied` folds into "
            "nothing - so an agent whose every call was blocked reports zero here and this "
            "endpoint shows it as entirely silent."
        ),
    )
    activity_evidence: ActivityEvidence = Field(
        description=(
            "**Which timestamp `last_activity_at` came from, and nothing else.** "
            "`trace_step` means a step's timestamp is *strictly* newer than the run-level "
            "one. `run_event` means it is not - which includes a tie, and includes a run "
            "whose steps are older than its own clock - so this field does **not** tell "
            "you whether the run has steps. `none` means neither clock is set, which no "
            "ingested event can produce; it survives for rows written directly. "
            "**`run_event` is not evidence that no tool activity occurred**: tool calls "
            "move no clock, so they can never produce `trace_step`. Read "
            "`has_untimed_activity` for that."
        ),
    )
    has_untimed_activity: bool = Field(
        description=(
            "True when tool calls have been counted and no step timestamp is newer than "
            "the run-level one. It means only that this response holds counted tool "
            "activity it cannot date. **It does not mean a tool call is running now, and "
            "it does not mean the agent is still working** - and False does not mean no "
            "calls were made, only that something dated is newer."
        ),
    )

    @classmethod
    def of(cls, run: RunActivity, *, now: dt.datetime) -> RunActivityView:
        """`now` is passed in rather than read here.

        Keyword-only and without a default, as `AgentView.of` is: one
        instant decides the cut-off that selected these rows and the age
        reported for each of them, so a long page cannot age as it is built.
        """
        return cls(
            id=run.id,
            run_ref=run.run_ref,
            agent_id=run.agent_id,
            status=run.status,
            started_at=run.started_at,
            last_activity_at=run.last_activity_at,
            seconds_since_activity=run.seconds_since_activity(now),
            step_count=run.step_count,
            tool_call_count=run.tool_call_count,
            activity_evidence=run.activity_evidence,
            has_untimed_activity=run.has_untimed_activity,
        )


class AgentExecutionView(ExecutionView):
    """An execution, plus the one thing an agent's own listing has to say.

    The shared `ExecutionView` everywhere else, with `is_test_harness`
    added here and nowhere else. An agent's executions are mostly its own
    tests - `agent.test` is how a managed agent runs at all before anyone
    has drawn a workflow around it - so a reader of *this* list needs the
    distinction to make sense of it, while a reader of a project's or a
    workflow's list already knows which workflow they asked about.

    Subclassed rather than copied, so the common fields cannot drift, and
    additive, so a client parsing `ExecutionView` parses this too.
    """

    is_test_harness: bool = Field(
        default=False,
        description=(
            "True when this execution ran the agent's `agent.test` harness rather than a "
            "workflow somebody drew. The same fact `GET /v1/projects/{project_id}/"
            "workflows/{workflow_id}` reports, resolved here so a client needs no second "
            "call per row."
        ),
    )

    @classmethod
    def of_execution(cls, row: Any, *, is_test_harness: bool) -> AgentExecutionView:
        """`ExecutionView.of`, plus the flag this listing adds.

        The parent is called explicitly rather than through `cls`, so the
        shared mapping stays the one place that knows how an execution row
        becomes a view.
        """
        return cls(**ExecutionView.of(row).model_dump(), is_test_harness=is_test_harness)


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
    # One clock for the whole page: two agents that last reported at the same
    # instant must not come back with different ages because the list was
    # long.
    now = dt.datetime.now(dt.UTC)
    return [AgentView.of(agent, now=now) for agent in agents]


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
    return AgentView.of(agent, now=dt.datetime.now(dt.UTC))


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
    return AgentView.of(
        await service.get(principal, project_id, agent_id), now=dt.datetime.now(dt.UTC)
    )


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


@project_router.get(
    "/{agent_id}/runs",
    response_model=list[RunView],
    summary="What this agent reported doing",
    description=(
        "Newest first, folded from the events the agent sent about its own work: a run "
        "appears here once the agent has reported `run.started` for it. **An agent "
        "AgentWorld runs itself has none** - a managed agent reports nothing, and its "
        "work appears under `GET /v1/projects/{project_id}/agents/{agent_id}/executions` "
        "instead, so an empty list here is an answer about where to look rather than a "
        "missing record. Readable for an archived agent, because what it did before it "
        "was archived is the question."
    ),
)
async def agent_runs(
    project_id: UUID,
    agent_id: UUID,
    principal: CurrentUser,
    service: AgentServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunView]:
    """`viewer`, through `agent.read_runs`.

    The action the permission matrix has carried unused since it was
    written; this is the first route to ask for it. Separate from
    `agent.read_history` because the two tables have different producers,
    which is the argument the history route makes in the other direction.
    """
    runs = await service.runs(principal, project_id, agent_id, limit=limit)
    return [RunView.of(run) for run in runs]


@runs_router.get(
    "",
    response_model=list[RunActivityView],
    summary="Runs that have reported nothing recently",
    description=(
        "Runs still marked `running` whose newest timestamped evidence is older than "
        "`quiet_for_seconds`. **This is not a list of failed runs, and reading it changes "
        "nothing.** A run appears because nothing has been reported about it recently, "
        "which may mean the agent stopped, or that it is still thinking, or that the work "
        "it is doing leaves no timestamped trace. AgentWorld does not run these agents and "
        "cannot tell those apart.\n\n"
        "Check `has_untimed_activity` before concluding a run is idle: tool calls are "
        "counted and carry no timestamp, so a run doing tool work can look old on every "
        "clock here. A run whose tool calls were all *denied* reports no tool activity at "
        "all, so this endpoint can show a blocked agent as silent - the event feed is "
        "where those refusals are.\n\n"
        "Quietest first. Settled runs are never listed. `quiet_for_seconds` is required: "
        "there is no default patience, and an agent's heartbeat interval is not one."
    ),
)
async def quiet_runs(
    project_id: UUID,
    quiet_for_seconds: Annotated[
        int,
        Query(
            ge=1,
            le=2_592_000,
            description="How long a run must have been silent to be listed. Required.",
        ),
    ],
    principal: CurrentUser,
    service: AgentServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunActivityView]:
    """`viewer`, through `agent.read_runs` - the same action as the per-agent read.

    One clock for the whole response: `now` decides the cut-off that selects
    the rows and the age reported for each of them, so the two cannot
    disagree and a long page does not age while it is built. The reconciler
    computes its cut-off once per pass for the same reason.
    """
    now = dt.datetime.now(dt.UTC)
    quiet = await service.quiet_runs(
        principal, project_id, quiet_for_seconds=quiet_for_seconds, now=now, limit=limit
    )
    return [RunActivityView.of(run, now=now) for run in quiet]


@project_router.get(
    "/{agent_id}/executions",
    response_model=list[AgentExecutionView],
    summary="Executions this agent took part in",
    description=(
        "Newest first. An agent reaches an execution through the step rows bound to it, "
        "so an execution it appears in several times - a long graph, or a retried step - "
        "is listed once. Test harness runs are included and marked with "
        "`is_test_harness` rather than filtered out: for most agents they are the only "
        "executions there are. Readable for an archived agent."
    ),
)
async def agent_executions(
    project_id: UUID,
    agent_id: UUID,
    principal: CurrentUser,
    executions: ExecutionServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AgentExecutionView]:
    """`viewer`, through `agent.read` and `execution.read`, in that order.

    Not `agent.read_runs`: these are AgentWorld's own records of work it
    drove, and reading them is the same act as reading any other execution.
    """
    found = await executions.list_for_agent(principal, project_id, agent_id, limit=limit)
    return [
        AgentExecutionView.of_execution(row, is_test_harness=found.is_harness(row.id))
        for row in found.rows
    ]


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
    return AgentView.of(agent, now=dt.datetime.now(dt.UTC))


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
    return AgentView.of(
        await service.restore(principal, context, project_id, agent_id),
        now=dt.datetime.now(dt.UTC),
    )


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

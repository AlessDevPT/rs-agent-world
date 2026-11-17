"""Publishing a workflow, running one, and reading what happened.

`POST .../publish` and `POST .../run` are the two routes that make a workflow
more than a drawing. They are separate on purpose: publishing freezes a graph
and is reversible only by publishing another, while running spends money and
reaches the outside world. A single "save and run" would make the second
decision implicit in the first.

A publish refusal is a 422 whose `errors` array carries one entry per
validation finding, with the pointer the UI highlights. That is the same
shape a malformed request body produces, so a client has one parser.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from agentworld.api.deps import CallContextDep, CurrentUser, ExecutionServiceDep
from agentworld.domain.entities import ExecutionStep
from agentworld.domain.enums import ExecutionStatus, InitiatorKind, StepKind, StepStatus

workflow_router = APIRouter(prefix="/v1/projects/{project_id}/workflows", tags=["executions"])
router = APIRouter(prefix="/v1/projects/{project_id}/executions", tags=["executions"])


class PublishedVersion(BaseModel):
    """What freezing produced."""

    version_id: UUID
    published_at: dt.datetime


class RunRequest(BaseModel):
    """What to start, and with what.

    `extra="forbid"`: a client that sends `version_id` expecting to pick one
    has misunderstood something worth being told about - a run always
    executes the newest published version, because running an older one is a
    separate feature with its own questions about which is current.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(
        default_factory=dict, description="Available to expressions as `$.input.…`."
    )
    idempotency_key: Annotated[str | None, Field(max_length=200)] = Field(
        default=None,
        description=(
            "Starting twice with the same key returns the first execution rather than "
            "creating a second."
        ),
    )


class StepErrorView(BaseModel):
    """Why a step failed, narrowed to the two fields that may be shown.

    The reducer stores whatever mapping the producing event carried - its
    `_error` passes one through whole - so what is in the column is not a
    promise about its shape. This model is the promise: a code a client can
    switch on and a message a person can read, and nothing else travels, so a
    producer that put a provider's response body or a stack trace beside them
    cannot have it served.
    """

    code: str
    message: str


class ExecutionStepView(BaseModel):
    """One step of an execution, as an authorized reader may see it.

    `status` is reported exactly as stored. A step whose dispatch went to
    `needs_review` reads as whatever it was when the call left - `dispatched`
    - because the worker deliberately writes no event for an outcome nobody
    knows. There is no `needs_review` member of `StepStatus` and this does
    not invent one: the uncertainty belongs to the dispatch, and the review
    queue is where it is answered.
    """

    step_key: str
    kind: StepKind
    status: StepStatus
    attempt: int = Field(
        description="0 for the first attempt. A retry adds a row rather than replacing one."
    )
    agent_key: str | None = Field(
        default=None,
        description="The agent the frozen graph assigned, for an `agent_task` step.",
    )
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    output: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Absent when the step produced nothing, and also when its kind has no rule "
            "for showing one - see the redaction note on the endpoint."
        ),
    )
    error: StepErrorView | None = None

    @classmethod
    def of(cls, step: ExecutionStep) -> ExecutionStepView:
        return cls(
            step_key=step.step_key,
            kind=step.kind,
            status=step.status,
            attempt=step.attempt,
            agent_key=step.agent_key,
            started_at=step.started_at,
            finished_at=step.finished_at,
            output=_safe_output(step.kind, step.output),
            error=_safe_error(step.error),
        )


#: The fields of an `agent_task` output that may be served, and the type each
#: has to be. Everything else a provider turn's output carries - or that
#: anything else wrote beside it - is dropped rather than inspected.
_TURN_FIELDS: Final[Mapping[str, type | tuple[type, ...]]] = {
    "text": str,
    "stop_reason": str,
    "model": str,
}

#: Inside `usage`, which is the one nested shape this release produces.
_USAGE_FIELDS: Final[tuple[str, ...]] = ("input_tokens", "output_tokens")


def _safe_output(kind: StepKind, output: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What of a step's output may be served, decided by the step's kind.

    **The rule has to survive copying, which is why `transform` is not served
    at all.** A later step's `input_mapping` is evaluated against a scope that
    holds every earlier step's output *as stored* (`advance._scope`), and the
    expression grammar can name any key of it or the whole object - so
    `{"everything": "$.steps.research.output"}` puts a model turn's entire
    stored output, including anything this function would have dropped, inside
    a transform's own output. Serving that would make the narrowing below
    decorative: a reader would get the hidden fields through the next step
    instead. The boundary is therefore drawn where provenance is still known.

    - **`agent_task`** is a model's answer, assembled by the step runner from
      a provider's response. Only the fields this system puts there are
      served - the text, why it stopped, which model answered, what it cost -
      so a provider's own payload, an error document it quoted, or a field a
      dispatchable agent added when reporting its own step cannot be served.
    - **`condition`** has one known field, `{"result": bool}`, written by
      `advance()` itself and derived from nothing but the expression's truth.
      It is projected rather than passed through, so a condition row that
      somehow carried more would not widen this.
    - **`transform`** is **omitted**. Its values are copies, and this layer
      cannot tell a copy of the author's own input from a copy of something
      upstream that was not served. The execution's own `output` is
      unaffected and still carries the result. Serving transform output again
      needs provenance, or an author-declared output schema; both are
      decisions, not details.
    - **anything else** - a kind this release cannot publish, or one added
      later - is omitted whole, for the same reason the instruction says to
      omit rather than guess.
    """
    if output is None:
        return None
    if kind is StepKind.CONDITION:
        result = output.get("result")
        return {"result": result} if isinstance(result, bool) else None
    if kind is not StepKind.AGENT_TASK:
        return None

    safe: dict[str, Any] = {}
    for field_name, expected in _TURN_FIELDS.items():
        value = output.get(field_name)
        if isinstance(value, expected):
            safe[field_name] = value
    usage = output.get("usage")
    if isinstance(usage, Mapping):
        counted = {
            name: usage[name]
            for name in _USAGE_FIELDS
            if isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool)
        }
        if counted:
            safe["usage"] = counted
    return safe or None


def _safe_error(error: Mapping[str, Any] | None) -> StepErrorView | None:
    """A stored error narrowed to `{code, message}`, or nothing.

    An error with neither readable field becomes `None` rather than an empty
    object: "this step failed and here is why" and "this step failed" are
    different statements, and a client should not have to tell them apart by
    inspecting an object with no content.

    This narrows the *shape*. It cannot scrub a secret that a producer wrote
    into the message itself - no rule can - and the producers in this system
    do not: a provider failure's message is the generic
    `ProviderError.__str__`, and its `reason` never leaves the server.
    """
    if error is None:
        return None
    code = error.get("code")
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    return StepErrorView(
        code=code if isinstance(code, str) and code.strip() else "unspecified",
        message=message,
    )


class ExecutionView(BaseModel):
    """An execution's own record.

    Unchanged by T12a, deliberately. Three tests assert that every route
    returning an execution returns the *same* document -
    `test_the_view_is_the_one_the_execution_routes_return`,
    `test_the_execution_view_is_the_shared_one_plus_the_flag`, and
    `test_the_answer_is_read_through_the_existing_execution_endpoint`, which
    is decision 6's "no second result API". A step collection loaded on one
    route and absent on the others would break all three, so the steps are a
    route of their own instead.
    """

    id: UUID
    workflow_id: UUID
    workflow_version_id: UUID
    status: ExecutionStatus
    #: Written once when the execution was created and never recomputed, so
    #: it survives the initiator being demoted or leaving.
    initiator_kind: InitiatorKind
    initiator_label: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: dt.datetime | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None

    @classmethod
    def of(cls, row: Any) -> ExecutionView:
        return cls(
            id=row.id,
            workflow_id=row.workflow_id,
            workflow_version_id=row.workflow_version_id,
            status=row.status,
            initiator_kind=row.initiator_kind,
            initiator_label=row.initiator_label,
            input=dict(row.input or {}),
            output=dict(row.output) if row.output is not None else None,
            error=dict(row.error) if row.error is not None else None,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )


@workflow_router.post(
    "/{workflow_id}/publish",
    response_model=PublishedVersion,
    status_code=status.HTTP_201_CREATED,
    summary="Freeze the current draft",
    description=(
        "Normalises the draft into the frozen step and edge tables and marks it "
        "published, after which it cannot be edited. A draft that cannot run is refused "
        "with one `errors` entry per finding. A fresh draft is opened from what was "
        "published, so the editor has something to open."
    ),
)
async def publish_workflow(
    project_id: UUID,
    workflow_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: ExecutionServiceDep,
) -> PublishedVersion:
    version_id, published_at = await service.publish(principal, context, project_id, workflow_id)
    return PublishedVersion(version_id=version_id, published_at=published_at)


@workflow_router.post(
    "/{workflow_id}/run",
    response_model=ExecutionView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run the newest published version",
    description=(
        "202, not 201: the execution exists when this returns, and whether any work "
        "has happened yet depends on what the first round of decisions produced."
    ),
)
async def run_workflow(
    project_id: UUID,
    workflow_id: UUID,
    body: RunRequest,
    principal: CurrentUser,
    context: CallContextDep,
    service: ExecutionServiceDep,
) -> ExecutionView:
    execution_id = await service.start(
        principal,
        context,
        project_id,
        workflow_id,
        inputs=body.input,
        idempotency_key=body.idempotency_key,
    )
    return ExecutionView.of(await service.get(principal, project_id, execution_id))


@workflow_router.get(
    "/{workflow_id}/executions",
    response_model=list[ExecutionView],
    summary="This workflow's executions",
    description=(
        "Newest first, capped like the project-wide listing. The same `ExecutionView`, so "
        "a client has one shape for both; an unknown workflow is a 404 rather than an "
        "empty list, which would claim the workflow has never run."
    ),
)
async def list_workflow_executions(
    project_id: UUID,
    workflow_id: UUID,
    principal: CurrentUser,
    service: ExecutionServiceDep,
) -> list[ExecutionView]:
    """This workflow's runs, including an agent test harness's.

    `GET /workflows/{id}` is how a caller finds out which it is looking at:
    a harness reports `is_test_harness`, so the executions here can be
    labelled as tests rather than as runs of a workflow nobody can find.
    """
    rows = await service.list_for_workflow(principal, project_id, workflow_id)
    return [ExecutionView.of(row) for row in rows]


@router.get(
    "",
    response_model=list[ExecutionView],
    summary="Executions in a project",
    description="Newest first. Step detail arrives with the execution reads in T12.",
)
async def list_executions(
    project_id: UUID,
    principal: CurrentUser,
    service: ExecutionServiceDep,
) -> list[ExecutionView]:
    rows = await service.list_in(principal, project_id)
    return [ExecutionView.of(row) for row in rows]


class CancellationView(BaseModel):
    """What the cancellation stopped, and what it could not stop.

    `cancelling` is the field that matters, and it is why this is not a
    `204`. An execution whose dispatch a worker already holds is *being*
    cancelled: the call may be on the wire, and nothing here can recall it.
    The counts are returned so a client can say so rather than reporting a
    stop that has not happened yet.
    """

    execution: ExecutionView
    previous_status: ExecutionStatus = Field(description="What the execution was before this call.")
    cancelled_dispatches: int = Field(
        description="Queued dispatches dropped. These had not started."
    )
    in_flight_dispatches: int = Field(
        description=(
            "Dispatches a worker already holds. They settle when they report or their "
            "lease expires."
        )
    )
    cancelling: bool = Field(
        description="True while work is still in flight, so the stop is not complete."
    )


@router.post(
    "/{execution_id}/cancel",
    response_model=CancellationView,
    summary="Stop an execution",
    description=(
        "Drops queued work and marks the execution cancelled. Work a worker is already "
        "performing cannot be recalled: it is asked to stop and settles when it reports "
        "or its lease expires, which is what `cancelling` reports. Refused with 409 if "
        "the execution has already finished."
    ),
)
async def cancel_execution(
    project_id: UUID,
    execution_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: ExecutionServiceDep,
) -> CancellationView:
    result = await service.cancel(principal, context, project_id, execution_id)
    return CancellationView(
        execution=ExecutionView.of(await service.get(principal, project_id, execution_id)),
        previous_status=result.previous_status,
        cancelled_dispatches=result.cancelled_dispatches,
        in_flight_dispatches=result.in_flight_dispatches,
        cancelling=result.cancelling,
    )


@router.get(
    "/{execution_id}",
    response_model=ExecutionView,
    summary="One execution",
)
async def get_execution(
    project_id: UUID,
    execution_id: UUID,
    principal: CurrentUser,
    service: ExecutionServiceDep,
) -> ExecutionView:
    return ExecutionView.of(await service.get(principal, project_id, execution_id))


@router.get(
    "/{execution_id}/steps",
    response_model=list[ExecutionStepView],
    summary="What the execution's steps did",
    description=(
        "One entry per step row, ordered by the published version's graph rather than "
        "by what finished first, with a step's attempts together and ascending. A "
        "step's `output` is narrowed to what its kind makes safe to serve - a model "
        "turn to its text, stop reason, model and token counts, a condition to its "
        "result, a transform not at all - and `error` to its code and message, so no "
        "provider payload, credential or internal detail travels. A step whose dispatch "
        "is awaiting human review keeps the status it had when the call went out; it is "
        "not reported as succeeded or failed. A route of its own rather than a field on "
        "the execution, because every route that returns an execution returns the same "
        "document and this read is the only one that pays for the steps."
    ),
)
async def get_execution_steps(
    project_id: UUID,
    execution_id: UUID,
    principal: CurrentUser,
    service: ExecutionServiceDep,
) -> list[ExecutionStepView]:
    steps = await service.steps(principal, project_id, execution_id)
    return [ExecutionStepView.of(step) for step in steps]

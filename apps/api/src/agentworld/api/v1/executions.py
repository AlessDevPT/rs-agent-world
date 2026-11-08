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
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from agentworld.api.deps import CallContextDep, CurrentUser, ExecutionServiceDep
from agentworld.domain.enums import ExecutionStatus, InitiatorKind

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


class ExecutionView(BaseModel):
    """An execution's own record. Step detail is T12's."""

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

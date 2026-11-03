"""The review queue: work that is stuck until a person says what happened.

Two routes, and the split in privilege between them is the design. Seeing
that an execution is stuck is `execution.read` at `viewer` - an operator
should be able to look. Deciding what happened to an effect nobody observed
is `dispatch.resolve_review` at `admin`, because the person is not editing a
record, they are asserting a fact the system could not establish and taking
responsibility for it.

`retry` additionally requires `acknowledged: true`. It is the one outcome
that *acts* on the assertion rather than recording it, and acting may repeat
something that already happened. Refusing without the flag is the system
declining to infer consent from a button press.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from agentworld.api.deps import CallContextDep, CurrentUser, ReviewServiceDep
from agentworld.services.review import MIN_NOTE_LENGTH, Resolution, ReviewItem

router = APIRouter(prefix="/v1/projects/{project_id}/dispatches", tags=["review"])


class ReviewItemView(BaseModel):
    """One stuck dispatch, with what led it here.

    `previous_recoveries` is counted from the `dispatch.recovered` audit rows
    rather than read from a column. A reviewer deciding what to do needs to
    know this has been round before, and the audit trail is where that lives -
    which is why T6b needed no row-level history and no migration.
    """

    id: UUID
    execution_id: UUID
    step_key: str
    kind: str
    side_effect_class: str
    attempt: int
    claim_count: int
    #: What the worker or the reaper last recorded. The reason this is here
    #: rather than only in the audit trail is that it is the first thing a
    #: person reading the queue wants.
    last_error: str | None = None
    last_error_code: str | None = None
    created_at: dt.datetime | None = None
    previous_recoveries: int

    @classmethod
    def of(cls, item: ReviewItem) -> ReviewItemView:
        return cls(
            id=item.id,
            execution_id=item.execution_id,
            step_key=item.step_key,
            kind=item.kind,
            side_effect_class=item.side_effect_class,
            attempt=item.attempt,
            claim_count=item.claim_count,
            last_error=item.last_error,
            last_error_code=item.last_error_code,
            created_at=item.created_at,
            previous_recoveries=item.previous_recoveries,
        )


class ResolveRequest(BaseModel):
    """What the person is asserting, and why.

    `extra="forbid"`: a client sending `status` or `step_key` has
    misunderstood what this endpoint does, and a resolution is not a thing to
    be wrong about quietly.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Resolution = Field(
        description=(
            "`succeeded` asserts the external effect happened; `failed` asserts it did "
            "not and is not worth retrying; `retry` asserts it probably did not and "
            "accepts the risk of repeating it."
        )
    )
    note: Annotated[str, Field(min_length=MIN_NOTE_LENGTH, max_length=2000)] = Field(
        description=(
            "Required. The only record of why this was resolved as it was, kept with "
            "the resolving user's identity in the audit log."
        )
    )
    acknowledged: bool = Field(
        default=False,
        description=(
            "Required to be true for `retry`: an acknowledgement that the external "
            "effect may already have happened and may happen twice."
        ),
    )
    output: dict[str, Any] | None = Field(
        default=None,
        description="For `succeeded`: what the step should be recorded as having produced.",
    )


@router.get(
    "",
    response_model=list[ReviewItemView],
    summary="Dispatches awaiting human resolution",
    description=(
        "Oldest first. Each row is an execution that cannot finish until somebody says "
        "what happened to an outbound call whose outcome is unknown."
    ),
)
async def list_needing_review(
    project_id: UUID,
    principal: CurrentUser,
    service: ReviewServiceDep,
) -> list[ReviewItemView]:
    items = await service.list_needing_review(principal, project_id)
    return [ReviewItemView.of(item) for item in items]


@router.post(
    "/{dispatch_id}/resolve",
    status_code=204,
    summary="Resolve an uncertain outcome",
    description=(
        "Admin only. Records what the resolving user asserts happened, in one "
        "transaction with the step's outcome and the audit entry, and then advances the "
        "execution. A dispatch that is not awaiting review, or that someone else has "
        "just resolved, is a 409."
    ),
)
async def resolve_review(
    project_id: UUID,
    dispatch_id: UUID,
    body: ResolveRequest,
    principal: CurrentUser,
    context: CallContextDep,
    service: ReviewServiceDep,
) -> None:
    await service.resolve(
        principal,
        context,
        project_id,
        dispatch_id,
        outcome=body.outcome,
        note=body.note,
        acknowledged=body.acknowledged,
        output=body.output,
    )

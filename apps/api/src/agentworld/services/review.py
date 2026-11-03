"""Resolving a dispatch whose outcome nobody knows.

`needs_review` is the one state in the system that requires a person. A
worker died while an outbound call was in flight, or a call timed out: the
request was sent and nothing came back, so whether it landed is unknown.
Nothing retries it automatically, and until someone acts the execution cannot
finish.

**Resolving one is an assertion, not an edit.** The person is stating what
happened to an effect this system could not observe, and the system takes
their word for it. That is why `dispatch.resolve_review` is `admin` rather
than `member`, why the note is mandatory, and why `retry` needs a separate
acknowledgement: it authorises doing again something that may already have
been done.

Three outcomes:

| Outcome | What it asserts | What happens |
|---|---|---|
| `succeeded` | it did happen | the step succeeds; the execution advances |
| `failed` | it did not happen | the step fails; the execution fails |
| `retry` | probably not, and I accept the risk | the dispatch returns to `pending` |

**History is preserved rather than rewritten.** Every
`dispatch.recovered` row the reaper wrote stays exactly where it was - the
application role holds `INSERT, SELECT` on `audit_log` and nothing else, so
this service could not edit one if it tried. The resolution adds a row; it
replaces nothing. `previous_recoveries` on the view is read from those rows,
which is the whole of what T6b needed from them: no row-level history column,
and no migration.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from agentworld.domain.enums import DispatchStatus
from agentworld.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import UserPrincipal
from agentworld.domain.protocol import PROTOCOL_VERSION, parse_envelope
from agentworld.domain.reducers import fold
from agentworld.infra.logging import get_logger
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services.executions import ExecutionService

log = get_logger(__name__)

#: The shortest note that could mean anything. Not a formatting rule - the
#: note is the only record of *why* a human asserted what they did, and
#: "ok" is not a reason anybody can act on six months later.
MIN_NOTE_LENGTH = 10


class Resolution(StrEnum):
    """What the person is asserting happened."""

    #: The external effect did happen. The step succeeded.
    SUCCEEDED = "succeeded"
    #: It did not happen, and is not worth another attempt.
    FAILED = "failed"
    #: It probably did not happen, and the risk of repeating it is accepted.
    RETRY = "retry"

    @property
    def needs_acknowledgement(self) -> bool:
        """Whether this outcome may repeat an effect that already happened.

        Only `retry`. The other two record a belief about the past; this one
        acts on it, and acting is where the risk is.
        """
        return self is Resolution.RETRY


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """A dispatch waiting for a person, with what led it here.

    `previous_recoveries` comes from the `dispatch.recovered` audit rows, not
    from a column. That is the answer to whether `audit_log` can satisfy
    "preserve previous recovery records": a reviewer needs to know this has
    been round twice before, and a count plus the rows themselves is what
    they need.
    """

    id: UUID
    execution_id: UUID
    step_key: str
    kind: str
    side_effect_class: str
    attempt: int
    claim_count: int
    last_error: str | None
    last_error_code: str | None
    created_at: dt.datetime | None
    previous_recoveries: int


class ReviewService:
    def __init__(self, factory: UnitOfWorkFactory, *, executions: ExecutionService) -> None:
        self._factory = factory
        self._executions = executions

    async def list_needing_review(
        self, principal: UserPrincipal, project_id: UUID
    ) -> Sequence[ReviewItem]:
        """Every dispatch waiting for a person in this project.

        A read, so `execution.read` at `viewer`: seeing that work is stuck is
        not the same privilege as deciding what happened to it.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.EXECUTION_READ, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)

            rows = await work.dispatch_reads(scope).needing_review()
            if not rows:
                return ()
            recoveries = await work.audit(TenantScope(workspace_id)).count_by_resource(
                action="dispatch.recovered",
                resource_ids=[row.id for row in rows],
            )
            steps = await work.executions(scope).step_keys([row.execution_step_id for row in rows])
            return tuple(
                ReviewItem(
                    id=row.id,
                    execution_id=row.execution_id,
                    step_key=steps.get(row.execution_step_id, ""),
                    kind=row.kind.value,
                    side_effect_class=row.side_effect_class.value,
                    attempt=row.attempt,
                    claim_count=row.claim_count,
                    last_error=row.last_error,
                    last_error_code=row.last_error_code,
                    created_at=row.created_at,
                    previous_recoveries=recoveries.get(row.id, 0),
                )
                for row in rows
            )

    async def resolve(
        self,
        principal: UserPrincipal,
        context: Any,
        project_id: UUID,
        dispatch_id: UUID,
        *,
        outcome: Resolution,
        note: str,
        acknowledged: bool = False,
        output: Mapping[str, Any] | None = None,
    ) -> None:
        """Apply one person's assertion about one uncertain outcome.

        Everything happens in one transaction: the dispatch moves, the step's
        outcome event is appended and folded, and the audit row naming the
        person is written. A resolution that fails leaves the dispatch exactly
        as stuck as it was, which is the right failure - the alternative is a
        step told something a human never managed to say.

        The execution is advanced afterwards, in its own transaction, through
        `ExecutionService.advance_once`. Not a second execution path: the same
        one the run and the worker use.
        """
        cleaned = _validated_note(note)
        if outcome.needs_acknowledgement and not acknowledged:
            # The gate exists because `retry` may repeat something that
            # already happened. Refusing without it is the system declining
            # to infer consent from a button press.
            raise ValidationFailedError(
                "Retrying may repeat an external effect that has already happened; "
                "the request must acknowledge that explicitly",
                field="acknowledged",
            )

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.DISPATCH_RESOLVE_REVIEW, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)

            existing = await work.dispatch_reads(scope).get(dispatch_id)
            if existing is None:
                raise NotFoundError("Dispatch not found", dispatch_id=str(dispatch_id))
            if existing.status is not DispatchStatus.NEEDS_REVIEW:
                raise ConflictError(
                    f"This dispatch is '{existing.status.value}', not awaiting review",
                    dispatch_id=str(dispatch_id),
                )

            # Captured *before* the update. `existing` is a SQLAlchemy
            # identity-mapped instance, so the `UPDATE ... RETURNING` below
            # refreshes it in place - and reading `existing.last_error`
            # afterwards gives the note we just wrote rather than the reason
            # the reaper recorded. A test caught that: the audit row's
            # `previous_error` was a copy of the resolution's own note.
            previously = {
                "attempt": existing.attempt,
                "claim_count": existing.claim_count,
                "error": existing.last_error,
                "error_code": existing.last_error_code,
                "execution_id": existing.execution_id,
                "execution_step_id": existing.execution_step_id,
            }

            resolved = await work.dispatch_reads(scope).resolve_review(
                dispatch_id,
                status=_status_for(outcome),
                note=cleaned,
                run_after=dt.datetime.now(dt.UTC) if outcome is Resolution.RETRY else None,
            )
            if resolved is None:
                # Someone else resolved it between the read above and this
                # update. One resolution wins; this one reports a conflict
                # rather than overwriting a decision somebody else made.
                raise ConflictError(
                    "This dispatch was resolved by someone else",
                    dispatch_id=str(dispatch_id),
                )

            step_key = await work.executions(scope).step_key_of(previously["execution_step_id"])
            if outcome is not Resolution.RETRY:
                await _report_resolved_step(
                    work,
                    scope,
                    execution_id=previously["execution_id"],
                    step_key=step_key,
                    attempt=previously["attempt"],
                    outcome=outcome,
                    note=cleaned,
                    output=output,
                )

            await work.audit(TenantScope(workspace_id)).append(
                actor_type="user",
                actor_id=principal.user_id,
                action="dispatch.resolve_review",
                resource_type="dispatch",
                resource_id=dispatch_id,
                request_id=getattr(context, "request_id", None),
                ip=getattr(context, "ip", None),
                user_agent=getattr(context, "user_agent", None),
                metadata={
                    "outcome": outcome.value,
                    "note": cleaned,
                    "acknowledged": acknowledged,
                    "from": DispatchStatus.NEEDS_REVIEW.value,
                    "to": _status_for(outcome).value,
                    "attempt": previously["attempt"],
                    "claim_count": previously["claim_count"],
                    "previous_error": previously["error"],
                    "previous_error_code": previously["error_code"],
                    "step_key": step_key,
                },
            )
            log.info(
                "dispatch_review_resolved",
                dispatch_id=str(dispatch_id),
                outcome=outcome.value,
                resolved_by=str(principal.user_id),
            )

        await self._executions.advance_once(
            project_id, previously["execution_id"], visible={workspace_id}
        )


# ----------------------------------------------------------------- helpers ---


def _status_for(outcome: Resolution) -> DispatchStatus:
    """The dispatch status one assertion produces.

    `failed` becomes `dead` rather than `failed`: a human has decided this
    work is over, and `failed` is the retryable state. Nothing should pick it
    up again because somebody happened to leave attempts on the row.
    """
    match outcome:
        case Resolution.SUCCEEDED:
            return DispatchStatus.SUCCEEDED
        case Resolution.FAILED:
            return DispatchStatus.DEAD
        case Resolution.RETRY:
            return DispatchStatus.PENDING


def _validated_note(note: str) -> str:
    """A note that says something, or a 422.

    Mandatory because the note is the only record of *why* someone asserted
    what they did. The length floor is crude and is not trying to judge
    quality: it refuses "ok" and "done", which are the two things a person
    types when a field is in their way.
    """
    cleaned = note.strip()
    if len(cleaned) < MIN_NOTE_LENGTH:
        raise ValidationFailedError(
            f"A resolution note of at least {MIN_NOTE_LENGTH} characters is required: "
            "it is the only record of why this was resolved as it was",
            field="note",
        )
    return cleaned


async def _report_resolved_step(
    work: UnitOfWork,
    scope: TenantScope,
    *,
    execution_id: UUID,
    step_key: str,
    attempt: int,
    outcome: Resolution,
    note: str,
    output: Mapping[str, Any] | None,
) -> None:
    """Append the step outcome a human asserted, and fold it.

    The dedup key carries `review` and the attempt, so it cannot collide with
    the event a worker would have written for the same attempt - and so a
    second resolution of the same attempt, if one ever got past the status
    guard, would be a duplicate rather than a second fact.

    The payload says the outcome was asserted rather than observed. Six
    months later the difference between "the model returned this" and "a
    person decided it had" is the whole of what a reader needs.
    """
    name = "succeeded" if outcome is Resolution.SUCCEEDED else "failed"
    payload: dict[str, Any] = {"resolved_by_review": True, "note": note}
    if outcome is Resolution.SUCCEEDED:
        payload["output"] = dict(output or {})
    else:
        payload["error"] = {"code": "resolved_by_review", "message": note}

    events = work.events(scope)
    derived = work.derived(scope)
    envelope = parse_envelope(
        {
            "event_id": str(uuid4()),
            "dedup_key": f"exec:{execution_id}:{step_key}:review-{name}:{attempt}",
            "type": f"workflow.step_{name}",
            "protocol_version": PROTOCOL_VERSION,
            "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
            "subject": {"kind": "execution_step", "ref": f"{execution_id}:{step_key}"},
            "correlation": {"execution_ref": str(execution_id)},
            "payload": payload,
        }
    )
    event_id = await events.append(
        envelope, seq=await events.next_seq(), refs=await events.resolve(envelope)
    )
    for mutation in fold(envelope):
        await derived.apply(mutation, event_id=event_id)


async def _workspace_of(work: UnitOfWork, project_id: UUID) -> UUID:
    workspace_id = await work.project_lookup.workspace_of(project_id)
    if workspace_id is None:
        raise NotFoundError("Project not found", project_id=str(project_id))
    return workspace_id

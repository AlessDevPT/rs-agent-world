"""The step runner: claim a dispatch, perform it, report, advance.

One loop, and the order of its four steps is the design.

```
claim      UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)
perform    the only place an outbound call happens
report     append the outcome event, fold it
advance    ExecutionService.advance_once - the same path, never a second one
```

**The worker decides nothing.** It reads `kind`, `side_effect_class` and
`instruction` off a row that `advance()` already decided, and it has no
access to the graph, the step definition or the policy. That is what makes
requirement 7 structural: a worker cannot execute consequential work without
an authorized dispatch record, because the only thing it can execute *is* a
dispatch record - and `advance()` will not write a consequential one.

Even so, this refuses a consequential dispatch outright. There is no path by
which one can exist today, which is exactly why the refusal is cheap and
worth having: if one ever appears, it came from outside the approved path, and
the worker that found it should stop rather than perform it.

**Reporting is idempotent at three layers.** `settle` only moves a row out of
`leased`, so a second report changes nothing. The outcome event's dedup key is
deterministic in the step and attempt, so a second append is a duplicate. And
`advance()` emits a decision only for a step whose state does not already
reflect it.

**An uncertain outcome is never guessed at.** `side_effect_started_at` is set
*before* an outbound call and cleared after it returns. A lease that expires
with it still set means the call was in flight when the worker died, and
nobody knows whether it landed - so the row becomes `needs_review` and
nothing retries it automatically. A timeout is the same case: the request may
have arrived.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from agentworld.domain.enums import (
    DispatchKind,
    DispatchStatus,
    ExecutionStatus,
    SideEffect,
    StepStatus,
)
from agentworld.domain.errors import ConflictError, NotFoundError
from agentworld.domain.protocol import PROTOCOL_VERSION, parse_envelope
from agentworld.domain.reducers import fold
from agentworld.infra.logging import get_logger
from agentworld.infra.net import GuardedClient
from agentworld.infra.providers import (
    Completion,
    CompletionRequest,
    Message,
    ProviderError,
    ProviderFailure,
    Role,
    StopReason,
)
from agentworld.infra.providers import (
    build as build_provider,
)
from agentworld.repositories.base import TenantScope
from agentworld.repositories.dispatches import ClaimedDispatch, ExpiredLease
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services.credentials import CredentialService
from agentworld.services.executions import ExecutionService

log = get_logger(__name__)

#: How long a worker holds a claim before the reaper may take it back. Longer
#: than any single outbound call's total timeout, so a slow provider does not
#: look like a crash; short enough that a crashed worker's work moves within
#: a minute or two rather than at the next deploy.
DEFAULT_LEASE_SECONDS = 120

#: A model answered, and said nothing usable. Values are lower snake case
#: because that is what the rest of this taxonomy is - `rate_limited`,
#: `invalid_request`, `runner_error`, `lease_expired` - and an operator
#: filtering on `last_error_code` should not have to know which family a code
#: came from.
EMPTY_MODEL_RESPONSE = "empty_model_response"
#: The same, where the provider told us why: it ran out of output budget
#: before producing anything. Separated from the above because the remedy is
#: different - raise `max_output_tokens` - and an operator reading
#: `last_error_code` should not have to guess which it was.
INCOMPLETE_MODEL_RESPONSE = "incomplete_model_response"

#: The longest a provider's `Retry-After` may delay a retry, and the
#: ceiling of the fallback backoff. Approved 2026-11-02: a provider asking
#: for longer cannot park a tenant's queue.
RETRY_AFTER_MAX_SECONDS = 900

#: Attempts a dispatch gets before it is dead. One for consequential work,
#: which cannot be retried blindly - and which this release cannot produce.
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Outcome:
    """What performing a dispatch produced.

    `uncertain` is the field that matters. It means the outbound call may or
    may not have happened, which is not a failure and is not a success: it is
    a case for a person. Nothing retries it.
    """

    status: DispatchStatus
    output: Mapping[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    uncertain: bool = False
    #: From the provider's `Retry-After`, when it sent a usable one. The
    #: backoff honours it, clamped - see `_backoff`.
    retry_after_seconds: float | None = None
    #: Whether trying again could plausibly give a different answer, as the
    #: provider adapter classified it. False for a 400, a 401 or a malformed
    #: response: nothing about waiting changes those, and retrying a caller's
    #: mistake three times only spends three times as much.
    #:
    #: Defaults to True so that an outcome built outside the adapter - the
    #: runner-crash path, say - keeps the old behaviour of being retried,
    #: bounded by the attempt budget.
    retryable: bool = True

    @property
    def is_success(self) -> bool:
        return self.status is DispatchStatus.SUCCEEDED


class StepRunner:
    """Performs one claimed dispatch, whatever kind it is.

    The provider is reached through `infra/providers`' registry, so this
    class names no vendor and holds no model knowledge: the slug and the
    model id come from a `model_profiles` row as data. Swapping the provider
    is a different row, not a different runner.
    """

    def __init__(
        self,
        factory: UnitOfWorkFactory,
        *,
        credentials: CredentialService,
        client: GuardedClient,
    ) -> None:
        self._factory = factory
        self._credentials = credentials
        self._client = client

    async def perform(self, claimed: ClaimedDispatch) -> Outcome:
        """Do the work, and never raise.

        Every failure becomes an `Outcome`, because a worker that raises
        leaves a lease held until it expires - turning a classified failure
        into an uncertain one. The one thing that may raise is a programming
        error, which is what the loop's own handler is for.
        """
        if not await self._execution_still_wants_this(claimed):
            # Cancellation leaves a leased dispatch alone on purpose, because
            # its call may be on the wire. If that worker then dies, the
            # reaper returns the row to `pending` - correctly, as far as it
            # can see, since no side effect had started - and the next worker
            # would perform work the user stopped. Found by
            # `test_a_lease_that_expires_after_cancellation_does_not_resume_the_work`,
            # which made a real provider call for a cancelled execution.
            #
            # Checked here rather than in the claim query: this is the last
            # point before the call, so it covers every route to a `pending`
            # row under a dead execution, not just the reaper's.
            log.info(
                "dispatch_refused_execution_not_live",
                dispatch_id=str(claimed.id),
                execution_id=str(claimed.execution_id),
            )
            return Outcome(
                status=DispatchStatus.CANCELLED,
                error="the execution is no longer running",
                error_code="execution_not_live",
                retryable=False,
            )

        if claimed.side_effect_class is SideEffect.CONSEQUENTIAL:
            # Unreachable through the approved path: `advance()` cannot emit
            # one. Refused rather than trusted, because a row carrying this
            # came from somewhere else.
            log.error(
                "consequential_dispatch_refused",
                dispatch_id=str(claimed.id),
                kind=claimed.kind.value,
            )
            return Outcome(
                status=DispatchStatus.DEAD,
                error="consequential work cannot be performed in this release",
                error_code="consequential_refused",
            )

        match claimed.kind:
            case DispatchKind.MANAGED_AGENT_TURN:
                return await self._managed_agent_turn(claimed)
            case _:
                return Outcome(
                    status=DispatchStatus.DEAD,
                    error=f"no runner is implemented for '{claimed.kind.value}'",
                    error_code="unsupported_kind",
                )

    async def _managed_agent_turn(self, claimed: ClaimedDispatch) -> Outcome:
        """One model call on behalf of one managed agent.

        The credential is decrypted immediately before the call and lives as
        a local for its duration. It is not in the instruction, not in the
        event, and not in the outcome - `instruction` carries a credential
        *reference*, which is the only form in which one travels.
        """
        try:
            plan = await self._plan(claimed)
        except (NotFoundError, ConflictError) as exc:
            # A misconfigured agent is terminal: it will be just as
            # misconfigured in thirty seconds.
            return Outcome(status=DispatchStatus.DEAD, error=exc.detail, error_code="misconfigured")

        provider = build_provider(
            plan.provider_slug,
            self._client,
            api_key=plan.api_key,
            base_url=plan.base_url,
        )
        request = CompletionRequest(
            model=plan.model_id,
            messages=[Message(role=Role.USER, content=plan.prompt)],
            max_output_tokens=plan.max_output_tokens,
            system=plan.system,
            idempotency_token=str(claimed.idempotency_token),
        )

        # Before the call, not after: if the lease expires with this set, the
        # reaper knows the request was in flight.
        async with self._factory.begin(visible_workspaces={claimed.workspace_id}) as work:
            await work.dispatch_claims(TenantScope(claimed.workspace_id)).mark_side_effect_started(
                claimed.id
            )

        try:
            completion = await provider.complete(request)
        except ProviderError as exc:
            # Only a timeout is uncertain. A 5xx means the provider answered
            # and said it failed; a connection error means the request never
            # left. Both are known failures and are retried. A timeout means
            # the request was sent and nothing came back, which says nothing
            # about whether it was served - so it goes to a person rather
            # than being guessed at in either direction.
            uncertain = exc.failure is ProviderFailure.TIMEOUT
            return Outcome(
                status=DispatchStatus.NEEDS_REVIEW if uncertain else DispatchStatus.FAILED,
                error=str(exc),
                error_code=exc.failure.value,
                uncertain=uncertain,
                retry_after_seconds=exc.retry_after_seconds,
                retryable=exc.failure.is_retryable,
            )

        if (unusable := _unusable(completion)) is not None:
            return unusable

        return Outcome(
            status=DispatchStatus.SUCCEEDED,
            output={
                "text": completion.text,
                "stop_reason": completion.stop_reason.value,
                "model": completion.model,
                "usage": {
                    "input_tokens": completion.usage.input_tokens,
                    "output_tokens": completion.usage.output_tokens,
                },
            },
        )

    async def _execution_still_wants_this(self, claimed: ClaimedDispatch) -> bool:
        """Whether the execution this dispatch belongs to may still progress.

        One extra read per dispatch, which is the price of not performing
        cancelled work. `ClaimedDispatch.execution_id` is not optional - the
        claim query only yields dispatches that belong to an execution - so
        there is no "no execution to ask about" case to handle here; mypy
        said so when this was written defensively.
        """
        async with self._factory.begin(visible_workspaces={claimed.workspace_id}) as work:
            scope = TenantScope(claimed.workspace_id, claimed.project_id)
            header = await work.executions(scope).header(claimed.execution_id)
        return header is not None and ExecutionStatus(header.status).is_live

    async def _plan(self, claimed: ClaimedDispatch) -> _TurnPlan:
        """Everything the call needs, gathered under one tenant scope."""
        scope = TenantScope(claimed.workspace_id, claimed.project_id)
        agent_key = str(claimed.instruction.get("agent_key") or "")

        async with self._factory.begin(visible_workspaces={claimed.workspace_id}) as work:
            agent = await work.agents(scope).by_key(agent_key)
            if agent is None:
                raise NotFoundError(f"Agent '{agent_key}' is gone", agent_key=agent_key)
            if agent.model_profile_id is None:
                raise ConflictError(
                    f"Agent '{agent_key}' has no model profile", agent_key=agent_key
                )
            profile = await work.model_profiles(TenantScope(claimed.workspace_id)).get(
                agent.model_profile_id
            )
            if profile is None:
                raise ConflictError(
                    f"Agent '{agent_key}' names a model profile that is gone",
                    agent_key=agent_key,
                )
            if profile.credential_id is None:
                raise ConflictError(
                    f"Model profile '{profile.name}' has no credential",
                    profile=profile.name,
                )

        api_key = await self._credentials.use(
            workspace_id=claimed.workspace_id, credential_id=profile.credential_id
        )
        parameters = dict(profile.parameters)
        return _TurnPlan(
            provider_slug=profile.provider_slug,
            model_id=profile.model_id,
            api_key=api_key,
            # Operator configuration, carried as data on the profile: a
            # regional endpoint, a gateway, or a test server. Still validated
            # by the egress policy and still reached through the guarded
            # client.
            base_url=str(parameters["base_url"]) if "base_url" in parameters else None,
            system=agent.instructions or None,
            prompt=_prompt(claimed.instruction),
            max_output_tokens=int(parameters.get("max_output_tokens", 1024)),
        )


@dataclass(frozen=True, slots=True)
class _TurnPlan:
    provider_slug: str
    model_id: str
    api_key: str
    base_url: str | None
    system: str | None
    prompt: str
    max_output_tokens: int


class DispatchWorker:
    """The loop. Claims one dispatch at a time and reports what happened.

    Not a daemon: `run_once` does one unit of work and returns, so the
    process that owns the loop - a worker entry point, a test, or an API
    request in development - decides the cadence. A `while True` inside a
    service is a service that cannot be tested without a timer.
    """

    def __init__(
        self,
        factory: UnitOfWorkFactory,
        *,
        runner: StepRunner,
        executions: ExecutionService,
        name: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._factory = factory
        self._runner = runner
        self._executions = executions
        self._name = name or f"worker-{uuid4().hex[:8]}"
        self._lease_seconds = lease_seconds
        #: Where the next poll starts in the workspace list, so a busy
        #: workspace cannot starve the ones after it.
        self._cursor = 0

    @property
    def name(self) -> str:
        return self._name

    async def run_once(self) -> ClaimedDispatch | None:
        """Claim, perform, report, advance. Returns what it did, or None.

        The claim commits on its own, before the work starts. That is
        deliberate: a lease nobody can see is a lease that does not stop a
        second worker, so the row must be visibly `leased` for the duration
        of the call rather than at the end of it.

        Workspaces are tried in turn because claiming is workspace-scoped -
        `dispatches` is under forced row-level security, so a session with no
        visibility set sees nothing. The starting point rotates, so a
        workspace with a permanently full queue cannot starve the ones after
        it in the list.
        """
        claimed = await self._claim()
        if claimed is None:
            return None

        log.info(
            "dispatch_claimed",
            dispatch_id=str(claimed.id),
            kind=claimed.kind.value,
            worker=self._name,
            claim_count=claimed.claim_count,
        )
        try:
            outcome = await self._runner.perform(claimed)
        except Exception as exc:
            # A programming error. The lease is released as a plain failure
            # rather than left to expire, because an expired lease on work
            # that never started looks like a crash mid-flight.
            log.exception("dispatch_runner_crashed", dispatch_id=str(claimed.id))
            outcome = Outcome(
                status=DispatchStatus.FAILED,
                error=f"{type(exc).__name__}",
                error_code="runner_error",
            )

        await self.report(claimed, outcome)
        return claimed

    async def _claim(self) -> ClaimedDispatch | None:
        """The first due dispatch in any workspace, or None."""
        async with self._factory.begin() as work:
            workspaces = list(await work.workspaces.live_ids())
        if not workspaces:
            return None

        start = self._cursor % len(workspaces)
        self._cursor += 1
        ordered = workspaces[start:] + workspaces[:start]

        for workspace_id in ordered:
            async with self._factory.begin(visible_workspaces={workspace_id}) as work:
                claimed = await work.dispatch_claims(TenantScope(workspace_id)).claim(
                    worker=self._name, lease_seconds=self._lease_seconds
                )
            if claimed is not None:
                return claimed
        return None

    async def report(self, claimed: ClaimedDispatch, outcome: Outcome) -> bool:
        """Record the outcome, then advance the execution.

        Returns False when the lease had already been taken back - a worker
        that was slow enough for the reaper to act must not overwrite what
        the reaper decided, because the reaper may have decided
        `needs_review`.

        The settle and the event are one transaction. The advance is a
        separate one, after it: `advance_once` takes the execution's row lock
        and decides from committed state, so folding the outcome first is
        what lets it see the step as finished.
        """
        retrying = _will_retry(claimed, outcome)
        async with self._factory.begin(visible_workspaces={claimed.workspace_id}) as work:
            settled = await work.dispatch_claims(TenantScope(claimed.workspace_id)).settle(
                claimed.id,
                # `pending` when it will be tried again, not `failed`. The
                # claim query filters on `status = 'pending'`, so settling a
                # retryable failure as `failed` leaves a row with a future
                # `run_after` that nothing will ever pick up - which is what
                # `test_a_retryable_failure_goes_back_to_pending_with_backoff`
                # caught. The failure is still recorded, in `last_error` and
                # `last_error_code`.
                status=DispatchStatus.PENDING if retrying else outcome.status,
                error=outcome.error,
                error_code=outcome.error_code,
                run_after=_backoff(claimed, outcome) if retrying else None,
            )
            if not settled:
                log.info(
                    "dispatch_report_ignored",
                    dispatch_id=str(claimed.id),
                    reason="the lease was already taken back",
                )
                return False

            if outcome.uncertain:
                # No step event. The step's outcome is genuinely unknown, and
                # writing either `succeeded` or `failed` would be a guess
                # recorded as a fact.
                log.error(
                    "dispatch_outcome_uncertain",
                    dispatch_id=str(claimed.id),
                    step_key=claimed.step_key,
                )
                return True

            if retrying:
                # The step stays as it is and the row goes back to pending.
                # No event: nothing has been decided about the step, and
                # `workflow.step_retried` is the orchestrator's to emit when
                # T8 gives it a retry decision to emit it for.
                return True

            await _report_step(work, claimed, outcome)

        await self._executions.advance_once(
            claimed.project_id, claimed.execution_id, visible={claimed.workspace_id}
        )
        return True


class LeaseReaper:
    """Takes back leases whose worker is gone.

    The only thing that moves a crashed worker's dispatch, because nothing
    can ask a dead process what it was doing. Two outcomes, and the
    difference between them is one column:

    - `side_effect_started_at` is NULL: the worker died before making its
      call, so the work is safe to retry and the row goes back to `pending`.
    - it is set: the call was in flight. **Nobody knows whether it landed**,
      so the row becomes `needs_review` and a person decides. Retrying might
      do the thing twice and failing might not do it at all; guessing between
      those is exactly what this column exists to prevent.
    """

    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    async def sweep(self, *, limit: int = 100) -> tuple[int, int]:
        """Returns `(returned_to_pending, escalated_to_review)`.

        Per workspace, for the same reason claiming is: the outbox is under
        forced row-level security and an unscoped session sees none of it.

        Every recovery writes an `audit_log` row in the same transaction, so
        a rolled-back recovery leaves no claim that it happened - and so the
        error the recovery is about to overwrite survives somewhere that
        cannot be overwritten, since the application role holds `INSERT,
        SELECT` on that table and nothing else.

        Several reapers may run at once. `expire_leases` selects
        `FOR UPDATE SKIP LOCKED`, so a second reaper skips a row the first
        holds rather than blocking on it, and `reclaim` is guarded on the
        lease still being expired - so a reaper that arrives after another
        has committed changes nothing. Measured with four concurrent sweeps
        against one expired lease: exactly one recovery.
        """
        returned = escalated = 0
        async with self._factory.begin() as work:
            workspaces = list(await work.workspaces.live_ids())

        for workspace_id in workspaces:
            async with self._factory.begin(visible_workspaces={workspace_id}) as work:
                scope = TenantScope(workspace_id)
                claims = work.dispatch_claims(scope)
                for expired in await claims.expire_leases(limit=limit):
                    status, reason = _recovery_for(expired)
                    if not await claims.reclaim(expired.id, status=status, reason=reason):
                        # Another reaper got there first, or the worker came
                        # back and reported. Either way this one does
                        # nothing, and writes no audit row for a recovery it
                        # did not perform.
                        continue
                    await work.audit(scope).append(
                        actor_type="system",
                        action="dispatch.recovered",
                        resource_type="dispatch",
                        resource_id=expired.id,
                        metadata={
                            "from": DispatchStatus.LEASED.value,
                            "to": status.value,
                            "reason": reason,
                            "attempt": expired.attempt,
                            "claim_count": expired.claim_count,
                            "side_effect_in_flight": expired.side_effect_in_flight,
                            "leased_by": expired.leased_by,
                            # The history `reclaim` is about to overwrite.
                            "previous_error": expired.previous_error,
                            "previous_error_code": expired.previous_error_code,
                        },
                    )
                    if status is DispatchStatus.NEEDS_REVIEW:
                        escalated += 1
                        log.error(
                            "dispatch_needs_review",
                            dispatch_id=str(expired.id),
                            reason="uncertain outcome after lease expiry",
                        )
                    else:
                        returned += 1
        return returned, escalated


# ----------------------------------------------------------------- helpers ---


def _recovery_for(expired: ExpiredLease) -> tuple[DispatchStatus, str]:
    """What an expired lease becomes, and why.

    One column decides it. `side_effect_started_at` was set before the
    outbound call and cleared after it returned, so a lease that expired
    with it still set means the call was in flight when the worker died and
    **nobody knows whether it landed**. Retrying might do the thing twice and
    failing might not do it at all, so it goes to a person. Nothing - no
    worker, no timer, no later sweep - retries it.
    """
    if expired.side_effect_in_flight:
        return (
            DispatchStatus.NEEDS_REVIEW,
            "the lease expired while an outbound call was in flight; whether it landed is unknown",
        )
    if expired.attempt + 1 >= expired.max_attempts:
        return DispatchStatus.DEAD, "the lease expired and no attempts remain"
    return DispatchStatus.PENDING, "the lease expired before the work started"


def _unusable(completion: Completion) -> Outcome | None:
    """A failure when the model answered with nothing worth recording.

    Returns None when the completion is usable, which is the ordinary case.

    ## Why this exists

    A live run against the real API found an agent task recorded as
    `succeeded` with `output.text == ""`. The model had spent its whole
    output budget on an extended-thinking block, which `_completion`
    discards by design as an unknown block type, and nothing downstream
    treated an empty answer as a problem: the step succeeded, the execution
    completed, and the workflow's result was silence.

    Measured against a reasoning model that emits thinking blocks - named in
    `15-T6-WORKER.md` rather than here, because `test_vendor_containment`
    holds this layer to knowing no vendor, and it caught this docstring
    doing it:

    | `max_tokens` | content blocks | stop reason | text |
    | --- | --- | --- | --- |
    | 1 | none | `max_tokens` | `""` |
    | 4 | thinking only | `max_tokens` | `""` |
    | 20 | thinking, text | `max_tokens` | `"p"` |

    ## What counts as usable

    Text that is not blank, or at least one tool call. Nothing else is
    inspected: this works on the neutral `Completion`, so it holds for every
    provider rather than for the one adapter that happens to exist, and a
    new adapter cannot forget it.

    Blank-but-present text counts as nothing. A step whose recorded output
    is whitespace is as useless to the next step's input mapping as an empty
    string, and treating them differently would make the guarantee depend on
    whether a model emitted a newline.

    A tool call with no text **is** usable - that is what a tool-use turn
    looks like - so this cannot reject one. `advance()` cannot emit a
    `tool_call` dispatch in this release, so no such completion can arrive
    through the approved path yet; the rule is written for when it can.

    ## What this deliberately does not do

    It does not reject a truncated answer that *has* content. A completion
    with `stop_reason = max_tokens` and real text is a fragment - the
    vocabulary says so - but whether a fragment is useful is the caller's
    question, not this function's: a summary cut short at a thousand tokens
    may be exactly what was wanted, and a sentence cut mid-word may not be.
    The provider gives no signal that separates the two, and inventing one
    (a length floor, a sentence-boundary check) would be this layer guessing
    at intent. The stop reason is preserved in the successful output
    instead, so a consumer that cares can see it.

    So the only "demonstrably incomplete" case acted on is the one the
    provider itself makes unambiguous: it stopped at the budget *and*
    produced nothing to show for it.
    """
    if completion.text.strip() or completion.tool_calls:
        return None

    truncated = completion.stop_reason is StopReason.MAX_TOKENS
    return Outcome(
        status=DispatchStatus.FAILED,
        # Diagnostics, and only diagnostics: the stop reason, the counts and
        # the model the provider echoed back. No content - there is none to
        # leak here, and a future caller copying this shape should not learn
        # to put model output in an error string.
        error=(
            "the model returned no usable content "
            f"(stop reason: {completion.stop_reason.value}, "
            f"model: {completion.model or 'unreported'}, "
            f"output tokens: {completion.usage.output_tokens})"
        ),
        error_code=INCOMPLETE_MODEL_RESPONSE if truncated else EMPTY_MODEL_RESPONSE,
        # Not retried. The same request against the same budget produces the
        # same silence, so a retry spends money to learn nothing; and where
        # the budget is the cause, the remedy is a configuration change
        # rather than a repetition. A human can still resolve the step
        # through the T6b review route if they disagree.
        retryable=False,
    )


def _will_retry(claimed: ClaimedDispatch, outcome: Outcome) -> bool:
    """Whether this failure goes back to `pending` for another attempt.

    Three gates, and two of them were added after the Phase 3 acceptance
    review measured what this function actually did.

    **`needs_review` is never retried**, which is the whole point of it.

    **A failure the provider classified terminal is not retried.** A 400, a
    401 and a malformed response cannot be fixed by waiting, and retrying
    them turns one caller's mistake into three times the spend.
    `ProviderFailure.is_retryable` has said so since T3 and was consulted by
    nothing: the review found a 400 going back to `pending`, and an existing
    test whose docstring claimed the opposite while quietly setting
    `max_attempts = 1` to make its assertion hold.

    **The budget is counted in claims, not in `attempt`.** This read
    `claimed.attempt + 1 < claimed.max_attempts`, and migration 0019 makes
    `attempt` immutable - it is the identity of a unit of work, not a
    counter. So the expression was frozen at `0 + 1 < 3` and the budget was
    never consumed: five consecutive failures produced five provider calls
    and a row still `pending`, with the backoff ceiling also frozen at two
    seconds. Measured, not inferred.

    `claim_count` is what moves, so it is what bounds this. The consequence
    worth knowing: a claim that crashed before the call also consumes
    budget, because the counter cannot tell "the provider refused" from "our
    worker died". That is bounded failure instead of unbounded looping, and
    it is visible in `claim_count` - but it does mean a crash-looping worker
    can exhaust a dispatch. A separate failure counter would separate the
    two, and that needs a migration and a decision rather than being
    smuggled in here.
    """
    if outcome.status is not DispatchStatus.FAILED:
        return False
    if not outcome.retryable:
        return False
    return claimed.claim_count < claimed.max_attempts


def _backoff(claimed: ClaimedDispatch, outcome: Outcome) -> dt.datetime:
    """When the next attempt becomes due.

    A provider that sent `Retry-After` knows when it will serve this key
    again, and the backoff is guessing - so the header wins when it is
    usable. Four constraints on that, each a decision rather than an
    accident:

    **Clamped to 900 seconds.** A provider that asks for a day - hostile,
    misconfigured, or honestly reporting a long outage - cannot park a
    tenant's queue for one. The ceiling is the same one the jittered backoff
    already uses.

    **No extra attempts.** The header changes *when* the next attempt
    happens, never *whether* there is one: the budget is `max_attempts`, and
    a dispatch out of attempts is terminal whatever the header says. That is
    enforced by `_will_retry`, which runs before this is called.

    **No lease extension.** The row is back to `pending` with no lease; this
    is `run_after`, which the claim query filters on.

    **No jitter when the header is honoured.** The provider named a time, and
    adding noise to a number we asked for is second-guessing it. Jitter stays
    on the fallback, where several workers are guessing the same number and
    need not to converge on it.

    **The fallback jitter is *equal* jitter**, `uniform(ceiling / 2, ceiling)`,
    not full jitter from zero. Full jitter decorrelates retries better, which
    is its point, but it meant a measured 4.95% of first retries landed inside
    100 ms: a delay named "backoff" that did not delay anything. Half the
    ceiling keeps a 50% spread - enough that workers failing together do not
    converge - while guaranteeing the wait happened at all. It scales with the
    ceiling rather than introducing a constant to keep in step with it, and it
    cannot interact with the 900-second cap, because half of a capped ceiling
    is still under the cap.

    This buys less than it looks. With `max_attempts` at 3 and a 2-second
    base, the whole retry horizon is about 3 seconds expected and 6 at worst
    either way, so no provider outage of a realistic length is survived by
    jitter policy. The horizon - attempts, base, and a circuit breaker - is
    T8's, and a floor is not a substitute for it.
    """
    if (asked := outcome.retry_after_seconds) is not None and asked >= 0:
        return dt.datetime.now(dt.UTC) + dt.timedelta(seconds=min(asked, RETRY_AFTER_MAX_SECONDS))
    # `claim_count`, not `attempt`: `attempt` is immutable, so this ceiling
    # was frozen at two seconds for every retry of a row - a failing call
    # was re-sent roughly once a second for ever. The first retry keeps the
    # original two-second ceiling, so `claim_count - 1` rather than
    # `claim_count`.
    ceiling = min(float(RETRY_AFTER_MAX_SECONDS), 2.0 * (2 ** max(0, claimed.claim_count - 1)))
    return dt.datetime.now(dt.UTC) + dt.timedelta(seconds=random.uniform(ceiling / 2, ceiling))


async def _report_step(work: UnitOfWork, claimed: ClaimedDispatch, outcome: Outcome) -> None:
    """Append the step's outcome event and fold it.

    The dedup key is deterministic in the step and the attempt, so a second
    report of the same attempt is a duplicate rather than a second fact -
    which is the event layer's half of idempotent reporting.
    """
    name = "succeeded" if outcome.is_success else "failed"
    payload: dict[str, Any] = {}
    if outcome.output is not None:
        payload["output"] = dict(outcome.output)
    if outcome.error is not None:
        payload["error"] = {"code": outcome.error_code or "unspecified", "message": outcome.error}

    scope = TenantScope(claimed.workspace_id, claimed.project_id)
    events = work.events(scope)
    derived = work.derived(scope)
    envelope = parse_envelope(
        {
            "event_id": str(uuid4()),
            "dedup_key": f"exec:{claimed.execution_id}:{claimed.step_key}:{name}:{claimed.attempt}",
            "type": f"workflow.step_{name}",
            "protocol_version": PROTOCOL_VERSION,
            "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
            "subject": {
                "kind": "execution_step",
                "ref": f"{claimed.execution_id}:{claimed.step_key}",
            },
            "correlation": {"execution_ref": str(claimed.execution_id)},
            "payload": payload,
        }
    )
    event_id = await events.append(
        envelope, seq=await events.next_seq(), refs=await events.resolve(envelope)
    )
    for mutation in fold(envelope):
        await derived.apply(mutation, event_id=event_id)


def _prompt(instruction: Mapping[str, Any]) -> str:
    """The turn's prompt, from the step's resolved input.

    Deliberately simple: this release runs one user message per step. The
    conversation loop - tool results fed back, several turns per step - is
    T7's, and building a message history here with no caller for it would be
    guessing at T7's shape.
    """
    raw = instruction.get("input")
    if isinstance(raw, Mapping) and raw:
        lines = [f"{key}: {value}" for key, value in sorted(raw.items())]
        return "\n".join(lines)
    return str(instruction.get("prompt") or "Begin the task you have been assigned.")


def expected_step_status(outcome: Outcome) -> StepStatus:
    """What the step becomes, exported for the tests that assert it."""
    return StepStatus.SUCCEEDED if outcome.is_success else StepStatus.FAILED


def kinds_with_runners() -> Sequence[DispatchKind]:
    """Dispatch kinds this release can actually perform.

    One. Exported so a test can assert that every other kind is refused
    rather than silently attempted - and so the list is somewhere a reviewer
    can see it grow.
    """
    return (DispatchKind.MANAGED_AGENT_TURN,)

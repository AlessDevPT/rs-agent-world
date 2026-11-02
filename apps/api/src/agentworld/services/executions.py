"""Publishing a workflow, starting a run, and applying what `advance` decided.

Three responsibilities, and the third is the one the architecture is built
around.

**Publish** freezes a draft. It refuses an unpublishable one with the
validation findings rather than a boolean, and it writes the graph *before*
setting `published_at`, because migration 0018's trigger refuses an insert
into a published version's tables - including the publish's own.

**Start** creates an execution bound to that frozen version, seeds one step
row per step, and calls `advance` for the first time.

**Apply** is the transactional outbox, and the shape of it is the whole
design:

```
BEGIN
  SELECT ... FROM workflow_executions WHERE id = :id FOR UPDATE
  decisions = advance(definition, state)        -- pure, no I/O
  for each decision:
      INSERT INTO events          (the decision, as a fact)      -- replayed
      UPDATE execution_steps/...  (fold the decision into state)  -- rebuilt
      INSERT INTO dispatches      (the intent, as work to do)     -- NEVER replayed
COMMIT
```

Two properties fall out, and both are tested rather than asserted. The three
writes commit together, so there is no state claiming a step was dispatched
with nothing to dispatch it, and no dispatch for a step whose state rolled
back. And replaying the event log reproduces the state while creating no
work, because appending to `dispatches` is not something a reducer can do -
`MutationTarget` has no member for it.

`advance` is called in-process here, as Q4 decided. It imports no database,
no provider and no framework, so running it inside this transaction costs a
function call and keeps one code path rather than two.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from agentworld.domain.enums import InitiatorKind, StepKind
from agentworld.domain.errors import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
    WorkflowNotPublishableError,
)
from agentworld.domain.orchestration import (
    CompleteExecution,
    CompleteStep,
    Decision,
    DispatchStep,
    FailExecution,
    SkipStep,
    ValidationReport,
    WorkflowDefinition,
    advance,
    validate,
)
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import UserPrincipal
from agentworld.domain.protocol import PROTOCOL_VERSION, parse_envelope
from agentworld.domain.reducers import fold
from agentworld.infra.logging import get_logger
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services import audit
from agentworld.services.context import CallContext
from agentworld.services.definitions import definition_for_execution, facts_and_effects

log = get_logger(__name__)

#: Step kinds a published version may contain in this release. `tool_call` is
#: absent and `workflow_steps.tool_call_needs_tool` is why: that check
#: constraint requires a `tool_id`, and there is no `tools` table until
#: Phase 6, so there is no row for it to name. Refusing at publish is the
#: honest answer - the alternative is inventing an id that a later foreign
#: key will not recognise.
PUBLISHABLE_KINDS: frozenset[StepKind] = frozenset(
    {StepKind.AGENT_TASK, StepKind.CONDITION, StepKind.TRANSFORM}
)


@dataclass(frozen=True, slots=True)
class Applied:
    """What one decide-and-apply round did."""

    decisions: tuple[Decision, ...]
    dispatches: tuple[UUID, ...]

    @property
    def made_progress(self) -> bool:
        return bool(self.decisions)


class ExecutionService:
    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    # ----------------------------------------------------------- publishing ---

    async def publish(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        workflow_id: UUID,
    ) -> tuple[UUID, dt.datetime]:
        """Freeze the current draft, or refuse with the reasons.

        The refusal carries the findings, because "no" on its own is useless
        to the person who has to fix it - the same reason `validate` returns
        a report rather than a boolean.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_PUBLISH, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            workflows = work.workflows(scope)

            if await workflows.get(workflow_id) is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            draft = await workflows.draft_of(workflow_id)
            if draft is None:
                raise ConflictError(
                    "This workflow has no draft to publish", workflow_id=str(workflow_id)
                )

            facts, effects, approvers = await facts_and_effects(work, scope, workspace_id)
            definition = definition_for_execution(draft, facts, effects, approvers)
            report = _validated(definition)

            agent_ids = {
                agent.agent_key: agent.id
                for agent in await work.agents(scope).list_all()
                if not agent.is_archived
            }
            steps = [
                {
                    "step_key": step.step_key,
                    "name": step.step_key,
                    "kind": step.kind,
                    "agent_id": agent_ids.get(step.agent_key or ""),
                    "input_mapping": dict(step.input_mapping),
                    "config": {"condition": step.condition} if step.condition else {},
                }
                for step in definition.steps
            ]
            edges = [
                {
                    "from_step": edge.from_step,
                    "to_step": edge.to_step,
                    "condition": edge.condition,
                }
                for edge in definition.edges
            ]
            published_at = await workflows.publish(
                workflow_id, version_id=draft.id, steps=steps, edges=edges
            )
            await workflows.next_draft(workflow_id, after=draft.version)

            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="workflow.publish",
                resource_type="workflow",
                resource_id=workflow_id,
                metadata={
                    "version": draft.version,
                    "steps": len(steps),
                    "warnings": sorted({f.code.value for f in report.findings}),
                },
            )
            return draft.id, published_at

    # -------------------------------------------------------------- starting ---

    async def start(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        workflow_id: UUID,
        *,
        inputs: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> UUID:
        """Create an execution of the newest published version and advance it.

        The initiator is this user, written once and never recomputed. A
        `system` initiator comes from a trigger and arrives in Phase 7; there
        is no path here by which an execution borrows an account's authority
        after the fact.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_RUN, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)

            workflow = await work.workflows(scope).get(workflow_id)
            if workflow is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            version = await work.workflows(scope).published_version(workflow_id)
            if version is None:
                raise ConflictError(
                    "This workflow has no published version to run",
                    workflow_id=str(workflow_id),
                )

            executions = work.executions(scope)
            if idempotency_key is not None:
                existing = await executions.by_idempotency_key(idempotency_key)
                if existing is not None:
                    # A trigger firing twice yields one execution. Returning
                    # the first is what makes the key useful rather than an
                    # error the caller has to interpret.
                    return existing

            execution_id = await executions.create(
                workflow_id=workflow_id,
                workflow_version_id=version.id,
                initiator_kind=InitiatorKind.USER,
                initiator_label=principal.email,
                initiator_user_id=principal.user_id,
                inputs=inputs,
                idempotency_key=idempotency_key,
            )
            await executions.seed_steps(execution_id, version.id)

            await _append(
                work,
                scope,
                event_type="workflow.execution_started",
                subject={"kind": "workflow_execution", "ref": str(execution_id)},
                payload={"workflow_id": str(workflow_id), "version": version.version},
                dedup_key=f"exec:{execution_id}:started",
                occurred_at=_now(),
            )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="workflow.run",
                resource_type="workflow_execution",
                resource_id=execution_id,
                metadata={"workflow_id": str(workflow_id), "version": version.version},
            )

        # A separate transaction, deliberately. The execution exists and is
        # recorded whatever the first round of decisions does, so a graph
        # that fails immediately still leaves an execution a user can look
        # at rather than a 500 and nothing.
        await self.advance_once(project_id, execution_id, visible={workspace_id})
        return execution_id

    # -------------------------------------------------------------- applying ---

    async def advance_once(
        self, project_id: UUID, execution_id: UUID, *, visible: set[UUID]
    ) -> Applied:
        """Decide and apply, in one transaction, under the execution's lock.

        Takes no principal. The authority for this work was decided when the
        execution was started and is recorded immutably on the row; a caller
        that could pass a principal here would be a caller that could change
        whose authority a running execution uses. The reaper and the step
        runner call this with no user at all.
        """
        async with self._factory.begin(visible_workspaces=visible) as work:
            workspace_id = next(iter(visible))
            scope = TenantScope(workspace_id, project_id)
            executions = work.executions(scope)

            if await executions.lock(execution_id) is None:
                raise NotFoundError("Execution not found", execution_id=str(execution_id))
            header = await executions.header(execution_id)
            state = await executions.state_of(execution_id, now=_now())
            if header is None or state is None:  # pragma: no cover - the lock proved it
                raise NotFoundError("Execution not found", execution_id=str(execution_id))

            definition = await _definition_of(work, scope, workspace_id, header)
            decisions = advance(definition, state)
            if not decisions:
                return Applied(decisions=(), dispatches=())

            dispatched: list[UUID] = []
            for decision in decisions:
                dispatch_id = await self._apply(
                    work, scope, execution_id=execution_id, decision=decision
                )
                if dispatch_id is not None:
                    dispatched.append(dispatch_id)

            # No status write here. `start` appends `workflow.execution_started`
            # and folding it already sets `running` - the reducer's
            # `_EXECUTION_STATUS` maps it. A second writer would be a second
            # opinion about the same field, which is the arrangement
            # `StepStatus`'s docstring warns about: one writer per field,
            # because two is how the two disagree. The non-vacuity pass found
            # this - removing the write failed no test, because the fold had
            # already done it.
            return Applied(decisions=decisions, dispatches=tuple(dispatched))

    async def _apply(
        self,
        work: UnitOfWork,
        scope: TenantScope,
        *,
        execution_id: UUID,
        decision: Decision,
    ) -> UUID | None:
        """One decision: an event, its fold, and - for a dispatch - a row.

        The event is written first and the dispatch last, which matters only
        if the transaction is torn: it is not, so the order is a reading
        convenience rather than a guarantee. The guarantee is the
        transaction.
        """
        event = _event_for(execution_id, decision)
        await _append(work, scope, **event)

        if not isinstance(decision, DispatchStep):
            return None

        executions = work.executions(scope)
        step_id = await executions.step_id(execution_id, decision.step_key, decision.attempt)
        if step_id is None:
            # The step row is seeded at start, so this means the graph and
            # the execution disagree - a version edited underneath a run,
            # which the version's immutability should make impossible.
            raise ConflictError(
                "The execution has no step row for this decision",
                step_key=decision.step_key,
            )
        return await work.dispatches(scope).create(
            execution_id=execution_id,
            execution_step_id=step_id,
            kind=decision.kind,
            side_effect_class=decision.side_effect_class,
            instruction=decision.instruction,
            attempt=decision.attempt,
        )

    # ----------------------------------------------------------------- reads ---

    async def get(self, principal: UserPrincipal, project_id: UUID, execution_id: UUID) -> Any:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.EXECUTION_READ, Resource(workspace_id))
            header = await work.executions(TenantScope(workspace_id, project_id)).header(
                execution_id
            )
            if header is None:
                raise NotFoundError("Execution not found", execution_id=str(execution_id))
            return header

    async def list_in(self, principal: UserPrincipal, project_id: UUID) -> Sequence[Any]:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.EXECUTION_READ, Resource(workspace_id))
            return await work.executions(TenantScope(workspace_id, project_id)).list_in_project()


# ----------------------------------------------------------------- helpers ---


def _now() -> dt.datetime:
    """The one clock read in this file.

    Passed into `ExecutionState` and onto every event's `occurred_at`, so
    everything below it is a function of its argument. `advance` reads no
    clock at all, which `test_nothing_in_the_module_reads_a_clock` asserts.
    """
    return dt.datetime.now(dt.UTC)


def _validated(definition: WorkflowDefinition) -> ValidationReport:
    """Refuse an unpublishable draft, and one this release cannot freeze."""
    report = validate(definition)
    if not report.publishable:
        raise WorkflowNotPublishableError(
            "This workflow cannot be published yet",
            findings=[
                {"pointer": f.pointer, "code": f.code.value, "detail": f.detail}
                for f in report.findings
            ],
        )
    unsupported = sorted(
        {step.kind.value for step in definition.steps if step.kind not in PUBLISHABLE_KINDS}
    )
    if unsupported:
        raise ValidationFailedError(
            "This release cannot publish a workflow containing "
            + ", ".join(f"'{kind}'" for kind in unsupported)
            + " steps",
            kinds=unsupported,
        )
    return report


async def _definition_of(
    work: UnitOfWork, scope: TenantScope, workspace_id: UUID, header: Any
) -> WorkflowDefinition:
    """The frozen graph, plus the facts that can have changed since publish.

    The graph is immutable; the agents it names are not. An agent archived
    or switched to `observed` after publish makes the version unrunnable, and
    `advance` is given current facts precisely so it notices - which is why
    its validate gate runs at decision time rather than being trusted from
    publish.
    """
    version = await work.workflows(scope).published_version(header.workflow_id)
    if version is None:  # pragma: no cover - an execution cannot exist without one
        raise NotFoundError("The executed version is gone", execution_id=str(header.id))
    facts, effects, approvers = await facts_and_effects(work, scope, workspace_id)
    return definition_for_execution(version, facts, effects, approvers)


def _event_for(execution_id: UUID, decision: Decision) -> dict[str, Any]:
    """The event that records one decision.

    `dedup_key` is deterministic - execution, step, decision, attempt - and
    that is load-bearing rather than tidy: two advancers racing produce the
    same key, so the loser's append violates the per-project dedup index and
    its whole transaction rolls back. The dispatch's own unique constraint is
    the second line of the same defence.
    """
    occurred_at = _now()
    execution_subject = {"kind": "workflow_execution", "ref": str(execution_id)}

    match decision:
        case DispatchStep():
            return {
                "event_type": "workflow.step_dispatched",
                "subject": _step_subject(execution_id, decision.step_key),
                "correlation": {"execution_ref": str(execution_id)},
                "payload": {
                    "attempt": decision.attempt,
                    "dispatch_kind": decision.kind.value,
                    "side_effect_class": decision.side_effect_class.value,
                },
                "dedup_key": (
                    f"exec:{execution_id}:{decision.step_key}:dispatched:{decision.attempt}"
                ),
                "occurred_at": occurred_at,
            }
        case CompleteStep():
            return {
                "event_type": "workflow.step_succeeded",
                "subject": _step_subject(execution_id, decision.step_key),
                "correlation": {"execution_ref": str(execution_id)},
                "payload": {"output": dict(decision.output)},
                "dedup_key": f"exec:{execution_id}:{decision.step_key}:succeeded:0",
                "occurred_at": occurred_at,
            }
        case SkipStep():
            return {
                "event_type": "workflow.step_skipped",
                "subject": _step_subject(execution_id, decision.step_key),
                "correlation": {"execution_ref": str(execution_id)},
                "payload": {"reason": decision.reason},
                "dedup_key": f"exec:{execution_id}:{decision.step_key}:skipped:0",
                "occurred_at": occurred_at,
            }
        case FailExecution():
            return {
                "event_type": "workflow.execution_failed",
                "subject": execution_subject,
                "correlation": {"execution_ref": str(execution_id)},
                "payload": {"error": {"code": "execution_failed", "message": decision.reason}},
                "dedup_key": f"exec:{execution_id}:failed",
                "occurred_at": occurred_at,
            }
        case CompleteExecution():
            return {
                "event_type": "workflow.execution_completed",
                "subject": execution_subject,
                "correlation": {"execution_ref": str(execution_id)},
                "payload": {"output": dict(decision.output)},
                "dedup_key": f"exec:{execution_id}:completed",
                "occurred_at": occurred_at,
            }
        case _:  # pragma: no cover - `Decision` is a closed union
            raise ValidationFailedError(f"No event is defined for {type(decision).__name__}")


def _step_subject(execution_id: UUID, step_key: str) -> dict[str, str]:
    return {"kind": "execution_step", "ref": f"{execution_id}:{step_key}"}


async def _append(
    work: UnitOfWork,
    scope: TenantScope,
    *,
    event_type: str,
    subject: Mapping[str, str],
    dedup_key: str,
    occurred_at: dt.datetime,
    payload: Mapping[str, Any] | None = None,
    correlation: Mapping[str, Any] | None = None,
) -> UUID:
    """Append one orchestrator event and fold it, in the caller's transaction.

    The same three lines as `test_folding_a_workflow_trace_creates_no_dispatches`,
    which was written against this path before it existed. Folding is what
    moves the derived state; it cannot write a dispatch, because
    `MutationTarget` has no member for one.
    """
    events = work.events(scope)
    derived = work.derived(scope)
    envelope = parse_envelope(
        {
            "event_id": str(uuid4()),
            "dedup_key": dedup_key,
            "type": event_type,
            "protocol_version": PROTOCOL_VERSION,
            "occurred_at": occurred_at.isoformat(),
            "subject": dict(subject),
            "correlation": dict(correlation or {}),
            "payload": dict(payload or {}),
        }
    )
    event_id = await events.append(
        envelope, seq=await events.next_seq(), refs=await events.resolve(envelope)
    )
    for mutation in fold(envelope):
        await derived.apply(mutation, event_id=event_id)
    return event_id


async def _workspace_of(work: UnitOfWork, project_id: UUID) -> UUID:
    workspace_id = await work.project_lookup.workspace_of(project_id)
    if workspace_id is None:
        raise NotFoundError("Project not found", project_id=str(project_id))
    return workspace_id

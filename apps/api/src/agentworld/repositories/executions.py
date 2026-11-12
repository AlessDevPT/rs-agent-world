"""Executions, their steps, and the first writer of the outbox.

**`DispatchRepository.create` is the only statement in this system that can
insert a `dispatches` row**, and a dispatch row is the authority to perform a
side effect. `tests/unit/test_dispatch_containment.py` names this module in
its allow-list, and that list was empty until this commit - so the diff that
added this file is the diff a reviewer reads to see every path by which
AgentWorld can cause an irreversible action.

Everything else here is reading state and recording it. The three tables are
`workflow_executions`, `execution_steps` and `dispatches`, and each write is
a plain statement with the tenant predicate on it - no ORM relationship
traversal, because a lazy load is a query whose tenant scope is whatever the
session happened to have.

`state_of` is the one function worth reading twice. It assembles the
`ExecutionState` that `advance` decides from, and it is where "the pure
function is told everything and looks nothing up" is paid for. Every field it
fills is one query against rows this scope can see.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentworld.domain.enums import (
    DispatchKind,
    DispatchStatus,
    ExecutionStatus,
    InitiatorKind,
    SideEffect,
    StepStatus,
)
from agentworld.domain.orchestration import ExecutionState, StepState
from agentworld.repositories import models as m
from agentworld.repositories.base import ScopedRepository


class ExecutionRepository(ScopedRepository):
    """One project's executions and their step rows."""

    async def create(
        self,
        *,
        workflow_id: UUID,
        workflow_version_id: UUID,
        initiator_kind: InitiatorKind,
        initiator_label: str,
        initiator_user_id: UUID | None = None,
        inputs: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> UUID:
        """Start one execution of one frozen version.

        The initiator is written here and never again. `initiator_label` is
        plain text on purpose: an audit two years from now answers "what
        started this" without a join that may have gone, and the database's
        `initiator_user_xor_system` constraint is what stops a row claiming
        both a user and a system origin.
        """
        return (
            await self._session.execute(
                sa.insert(m.WorkflowExecution)
                .values(
                    workspace_id=self.scope.workspace_id,
                    project_id=self.scope.require_project(),
                    workflow_id=workflow_id,
                    workflow_version_id=workflow_version_id,
                    status=ExecutionStatus.PENDING,
                    initiator_kind=initiator_kind,
                    initiator_user_id=initiator_user_id,
                    initiator_label=initiator_label,
                    input=dict(inputs or {}),
                    idempotency_key=idempotency_key,
                )
                .returning(m.WorkflowExecution.id)
            )
        ).scalar_one()

    async def seed_steps(self, execution_id: UUID, workflow_version_id: UUID) -> int:
        """One `pending` step row per step of the frozen graph.

        Created up front rather than on demand, because a reducer's `Patch`
        selects a step by `(execution, step_key)` and updates zero rows if it
        is not there - so a step row that appeared only when work started
        would silently drop the event that started it.

        `attempt` is 0 for all of them. A retry inserts a new row at the next
        attempt, which is what `execution_steps_attempt_key` is for.
        """
        steps = (
            await self._session.execute(
                sa.select(m.WorkflowStep.id, m.WorkflowStep.step_key, m.WorkflowStep.agent_id)
                .where(
                    m.WorkflowStep.workflow_version_id == workflow_version_id,
                    m.WorkflowStep.project_id == self.scope.require_project(),
                )
                .order_by(m.WorkflowStep.step_key)
            )
        ).all()
        if not steps:
            return 0
        await self._session.execute(
            sa.insert(m.ExecutionStep),
            [
                {
                    "execution_id": execution_id,
                    "workspace_id": self.scope.workspace_id,
                    "project_id": self.scope.require_project(),
                    "workflow_version_id": workflow_version_id,
                    "workflow_step_id": step.id,
                    "step_key": step.step_key,
                    "status": StepStatus.PENDING,
                    "attempt": 0,
                    "agent_id": step.agent_id,
                }
                for step in steps
            ],
        )
        return len(steps)

    async def lock(self, execution_id: UUID) -> UUID | None:
        """Take the execution's row lock for a decide-and-apply transaction.

        One advancer per execution at a time. Executions are independent, so
        this serialises nothing that matters, and it is what stops `advance`
        seeing a half-applied sibling decision. The `UNIQUE (execution_step_id,
        kind, attempt)` constraint on `dispatches` is the backstop for a
        caller that forgets to take it - which is the arrangement the double
        -advance test exercises.
        """
        return (
            await self._session.execute(
                sa.select(m.WorkflowExecution.id)
                .where(
                    m.WorkflowExecution.id == execution_id,
                    m.WorkflowExecution.project_id == self.scope.require_project(),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def header(self, execution_id: UUID) -> Any:
        """The execution row itself, or None."""
        return (
            await self._session.execute(
                sa.select(m.WorkflowExecution).where(
                    m.WorkflowExecution.id == execution_id,
                    m.WorkflowExecution.project_id == self.scope.require_project(),
                )
            )
        ).scalar_one_or_none()

    async def by_idempotency_key(self, key: str) -> UUID | None:
        return (
            await self._session.execute(
                sa.select(m.WorkflowExecution.id).where(
                    m.WorkflowExecution.project_id == self.scope.require_project(),
                    m.WorkflowExecution.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()

    async def state_of(self, execution_id: UUID, *, now: dt.datetime) -> ExecutionState | None:
        """Everything `advance` is told, assembled from rows.

        `now` is an argument rather than a call, and it is passed straight
        through onto the state: the decision function must be handed its
        clock, and the only place a clock may be read is the caller that
        starts the transaction.
        """
        execution = await self.header(execution_id)
        if execution is None:
            return None

        rows = (
            await self._session.execute(
                sa.select(
                    m.ExecutionStep.step_key,
                    m.ExecutionStep.status,
                    m.ExecutionStep.attempt,
                    m.ExecutionStep.output,
                    m.ExecutionStep.error,
                )
                .where(
                    m.ExecutionStep.execution_id == execution_id,
                    m.ExecutionStep.project_id == self.scope.require_project(),
                )
                # Highest attempt per step wins: a retry inserts a new row and
                # the newest one is the state of that step.
                .order_by(m.ExecutionStep.step_key, m.ExecutionStep.attempt)
            )
        ).all()

        steps: dict[str, StepState] = {}
        for row in rows:
            steps[str(row.step_key)] = StepState(
                status=row.status,
                attempt=row.attempt,
                output=dict(row.output) if row.output is not None else None,
                failure_reason=_message(row.error),
            )
        return ExecutionState(
            status=execution.status,
            now=now,
            input=dict(execution.input),
            steps=steps,
            max_attempts=1,
        )

    async def step_id(self, execution_id: UUID, step_key: str, attempt: int) -> UUID | None:
        """The step row a dispatch must point at.

        `dispatches.execution_step_id` is NOT NULL since T0, so a dispatch
        cannot be written without one - which is why this returns None rather
        than raising and lets the caller decide what a missing step means.
        """
        return (
            await self._session.execute(
                sa.select(m.ExecutionStep.id).where(
                    m.ExecutionStep.execution_id == execution_id,
                    m.ExecutionStep.project_id == self.scope.require_project(),
                    m.ExecutionStep.step_key == step_key,
                    m.ExecutionStep.attempt == attempt,
                )
            )
        ).scalar_one_or_none()

    async def step_key_of(self, execution_step_id: UUID) -> str:
        """The step key a dispatch's step row carries.

        A dispatch names a step row; the human reading a review queue needs
        the key the author wrote. Returns an empty string for a step that is
        gone, because a missing key must not stop someone resolving the
        uncertainty the dispatch represents.
        """
        found = (
            await self._session.execute(
                sa.select(m.ExecutionStep.step_key).where(
                    m.ExecutionStep.id == execution_step_id,
                    m.ExecutionStep.project_id == self.scope.require_project(),
                )
            )
        ).scalar_one_or_none()
        return str(found) if found is not None else ""

    async def step_keys(self, execution_step_ids: Sequence[UUID]) -> Mapping[UUID, str]:
        """The same, for a list - one query rather than one per row."""
        if not execution_step_ids:
            return {}
        rows = await self._session.execute(
            sa.select(m.ExecutionStep.id, m.ExecutionStep.step_key).where(
                m.ExecutionStep.id.in_(list(execution_step_ids)),
                m.ExecutionStep.project_id == self.scope.require_project(),
            )
        )
        return {row[0]: str(row[1]) for row in rows.all()}

    async def needing_advance(
        self, *, stale_before: dt.datetime, limit: int
    ) -> Sequence[tuple[UUID, UUID]]:
        """Executions that look owed an advance, as `(execution_id, project_id)`.

        Derived rather than marked. A `running` execution with no dispatch in
        `pending`, `leased` or `needs_review` has nothing in flight, so it was
        either never told what to do next or it has already finished. Both are
        safe to hand to `advance_once`, which decides from committed state and
        writes nothing when there is nothing to decide - so a false positive
        here costs one row lock and no rows.

        `needs_review` counts as unsettled deliberately. An execution waiting
        for a person must never become a candidate; that is the state T6 and
        T6b exist to protect, and nothing automatic may advance past it.

        `stale_before` keeps the reconciler out of the normal path's way. A
        worker settles a dispatch and advances milliseconds later, so without
        a grace period this would compete for the execution's row lock on
        every ordinary step. `last_event_at` is the execution's own folded
        clock: a stalled execution's stops moving while a healthy one's does
        not. It is NULL only before the first event is folded, which is a
        state no advance is owed for - but it is included rather than excluded
        so that a row with a missing clock is visible to the sweep rather than
        invisible to it.

        Oldest trace first, so the longest-stalled execution is recovered
        first and a candidate beyond `limit` is picked up by a later pass
        rather than forgotten.

        Workspace-scoped, not project-scoped: the sweep covers a whole
        workspace, and `dispatches` is under forced row-level security, so the
        session has to be opened with that workspace visible. The project id
        comes back per row because `advance_once` needs it.
        """
        unsettled = (
            sa.select(sa.literal(1))
            .select_from(m.Dispatch)
            .where(
                m.Dispatch.execution_id == m.WorkflowExecution.id,
                m.Dispatch.status.in_([s for s in DispatchStatus if s.is_unsettled]),
            )
        )
        rows = await self._session.execute(
            sa.select(m.WorkflowExecution.id, m.WorkflowExecution.project_id)
            .where(
                m.WorkflowExecution.workspace_id == self.scope.workspace_id,
                m.WorkflowExecution.status == ExecutionStatus.RUNNING,
                sa.or_(
                    m.WorkflowExecution.last_event_at.is_(None),
                    m.WorkflowExecution.last_event_at < stale_before,
                ),
                ~unsettled.exists(),
            )
            .order_by(m.WorkflowExecution.last_event_at.asc().nullsfirst())
            .limit(limit)
        )
        return [(row[0], row[1]) for row in rows.all()]

    async def list_in_project(self, *, limit: int = 50) -> Sequence[Any]:
        rows = await self._session.execute(
            sa.select(m.WorkflowExecution)
            .where(m.WorkflowExecution.project_id == self.scope.require_project())
            .order_by(m.WorkflowExecution.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def list_for_workflow(self, workflow_id: UUID, *, limit: int = 50) -> Sequence[Any]:
        """One workflow's executions, newest first.

        The same rows, the same shape and the same cap as
        `list_in_project` - a caller reading a workflow's history should not
        meet a second set of conventions - narrowed by `workflow_id` and
        still scoped to the project, so the tenant predicate holds whatever
        workflow id is passed in.

        `id` breaks a tie on `created_at`, which the project-wide listing
        does not need and this does: `now()` is the transaction's start
        time, so executions created in one transaction share it exactly, and
        an order that left them to the planner could return them differently
        on two calls.
        """
        rows = await self._session.execute(
            sa.select(m.WorkflowExecution)
            .where(
                m.WorkflowExecution.project_id == self.scope.require_project(),
                m.WorkflowExecution.workflow_id == workflow_id,
            )
            .order_by(
                m.WorkflowExecution.created_at.desc(),
                m.WorkflowExecution.id.desc(),
            )
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def list_for_agent(self, agent_id: UUID, *, limit: int = 50) -> Sequence[Any]:
        """The executions this agent took part in, newest first.

        An agent reaches an execution through `execution_steps.agent_id`,
        which is copied from the frozen graph when the steps are seeded. A
        long graph gives one row per step and a retry gives another per
        attempt, so the join is written as an `EXISTS` rather than a join
        with `DISTINCT`: the question is "is this agent in it at all", one
        execution is one answer whatever the step count, and the planner can
        stop at the first matching step instead of collecting them all to
        throw duplicates away.

        The subquery carries the project predicate too. `execution_steps` is
        under forced row-level security like everything else, and the
        predicate is stated here for the same reason it is stated in the
        outer query: RLS sits behind the tenant check rather than instead of
        it. A step row from another project could not match an execution in
        this one anyway - the composite key makes that unstorable - which is
        what makes the predicate cheap rather than redundant.

        `agent_id IS NOT NULL` is implied by the equality and stated anyway,
        because `execution_steps_agent_idx` is partial on exactly that and a
        predicate the planner can match literally is what lets it use the
        index.

        The same order and the same cap as `list_for_workflow`: a caller
        reading an agent's history should not meet a third set of
        conventions.
        """
        involved = (
            sa.select(sa.literal(1))
            .select_from(m.ExecutionStep)
            .where(
                m.ExecutionStep.execution_id == m.WorkflowExecution.id,
                m.ExecutionStep.project_id == self.scope.require_project(),
                m.ExecutionStep.agent_id.is_not(None),
                m.ExecutionStep.agent_id == agent_id,
            )
        )
        rows = await self._session.execute(
            sa.select(m.WorkflowExecution)
            .where(
                m.WorkflowExecution.project_id == self.scope.require_project(),
                involved.exists(),
            )
            .order_by(
                m.WorkflowExecution.created_at.desc(),
                m.WorkflowExecution.id.desc(),
            )
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def harness_executions(self, execution_ids: Sequence[UUID]) -> frozenset[UUID]:
        """Which of these executions ran an agent test harness.

        One query for a list, like `step_keys`, rather than one per row.
        The fact is `workflows.test_for_agent_id IS NOT NULL` - the same
        column `WorkflowView.is_test_harness` reports - read through the
        execution's `workflow_id`.

        Returned as a set of ids rather than a flag per row, so the caller
        labels the rows it already has instead of this returning a second
        copy of them. An execution missing from the set is not a harness,
        which is the right default: the column being NULL is what "a person
        drew this" looks like.

        **Both tables carry the project predicate, and the joined one needs
        it most.** `workflow_executions.workflow_id` is a single-column key
        to `workflows (id)` - it says the workflow exists, not that it
        belongs to the execution's project, which is the gap 0012 and 0023
        closed elsewhere and left open here. Nothing writes a mismatched
        pair today, and a mismatch could only mislabel a boolean rather than
        return a row from another tenant, since the ids come from a listing
        already scoped to this project. Stating the predicate costs nothing
        and means this read does not depend on that being true.
        """
        if not execution_ids:
            return frozenset()
        rows = await self._session.execute(
            sa.select(m.WorkflowExecution.id)
            .join(m.Workflow, m.Workflow.id == m.WorkflowExecution.workflow_id)
            .where(
                m.WorkflowExecution.id.in_(list(execution_ids)),
                m.WorkflowExecution.project_id == self.scope.require_project(),
                m.Workflow.project_id == self.scope.require_project(),
                m.Workflow.test_for_agent_id.is_not(None),
            )
        )
        return frozenset(row[0] for row in rows.all())


class DispatchRepository(ScopedRepository):
    """The outbox. One method, and it is the only way work comes into being.

    Nothing here reads a dispatch: claiming is T6's, and a repository that
    could both create and claim would be the natural place for someone to add
    "create and immediately run", which is the one arrangement the outbox
    exists to prevent.
    """

    async def create(
        self,
        *,
        execution_id: UUID,
        execution_step_id: UUID,
        kind: DispatchKind,
        side_effect_class: SideEffect,
        instruction: Mapping[str, Any],
        attempt: int,
        max_attempts: int = 3,
    ) -> UUID:
        """Insert one unit of work.

        Raises on a duplicate, and that is the design: `UNIQUE
        (execution_step_id, kind, attempt)` means two concurrent advancers
        produce one dispatch and one constraint violation rather than two
        side effects. The loser's whole transaction rolls back, so it also
        leaves no event and no state change - the decision it tried to apply
        never happened.

        `side_effect_class` is copied from the decision rather than looked up
        here, so editing a tool while work is in flight cannot change the
        retry policy of work already decided. `advance` never emits
        `consequential`, so no row written through here can carry it in this
        release.
        """
        return (
            await self._session.execute(
                sa.insert(m.Dispatch)
                .values(
                    workspace_id=self.scope.workspace_id,
                    project_id=self.scope.require_project(),
                    execution_id=execution_id,
                    execution_step_id=execution_step_id,
                    kind=kind,
                    side_effect_class=side_effect_class,
                    instruction=dict(instruction),
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                .returning(m.Dispatch.id)
            )
        ).scalar_one()


def _message(error: Any) -> str | None:
    """The human-readable half of a stored error, if there is one."""
    if isinstance(error, Mapping):
        message = error.get("message")
        return str(message) if message is not None else None
    return None

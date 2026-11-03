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

    async def list_in_project(self, *, limit: int = 50) -> Sequence[Any]:
        rows = await self._session.execute(
            sa.select(m.WorkflowExecution)
            .where(m.WorkflowExecution.project_id == self.scope.require_project())
            .order_by(m.WorkflowExecution.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())


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

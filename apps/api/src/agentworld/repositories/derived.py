"""Applying what a reducer decided.

The reducers are pure and speak in producer references - `agent_key`,
`run_ref` - because the domain layer has no way to know a database id. This is
where those become rows. It is the only module that knows both vocabularies,
which is exactly why it is this small and this boring.

**A mutation that matches nothing is not an error.** An event about a deleted
agent, or one whose ref never resolved, applies to zero rows and the event is
still recorded. The count comes back so ingest can report it, because "we
stored your event and it changed nothing" is a useful thing for an integrator
to be told and a terrible thing to discover later.

**Field names are checked against the real columns before the SQL is built.**
The reducers are tested against the schema, so this should never fire - but
the alternative to checking is interpolating an unknown name into an UPDATE,
and the failure mode of that is worth one dictionary lookup.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult

from agentworld.domain.reducers import (
    AgentRef,
    AgentStatusRef,
    Append,
    ExecutionRef,
    Increment,
    Mutation,
    MutationTarget,
    Patch,
    RunRef,
    RunStepRef,
    Selector,
    StepRef,
    TaskRef,
)
from agentworld.repositories import models as m
from agentworld.repositories.base import ScopedRepository

_TABLES: Mapping[MutationTarget, type[m.Base]] = {
    MutationTarget.AGENTS: m.Agent,
    MutationTarget.AGENT_STATUS_HISTORY: m.AgentStatusHistory,
    MutationTarget.TASKS: m.Task,
    MutationTarget.RUNS: m.Run,
    MutationTarget.RUN_STEPS: m.RunStep,
    MutationTarget.WORKFLOW_EXECUTIONS: m.WorkflowExecution,
    MutationTarget.EXECUTION_STEPS: m.ExecutionStep,
}


class DerivedStateRepository(ScopedRepository):
    """The tables a folded event may change, and nothing else.

    The vocabulary it accepts is `MutationTarget`, which has no member for
    `dispatches` - so this class could not write one if a caller asked. That
    is the seventh protocol promise reaching the only layer that could have
    broken it.
    """

    async def apply(self, mutation: Mutation, *, event_id: UUID) -> int:
        """Apply one mutation, returning how many rows it changed."""
        if isinstance(mutation, Append):
            return await self._append(mutation, event_id=event_id)
        if isinstance(mutation, Increment):
            return await self._increment(mutation)
        return await self._patch(mutation)

    # ------------------------------------------------------------- verbs ---

    async def _patch(self, mutation: Patch) -> int:
        table = _TABLES[mutation.target]
        fields = _checked(table, mutation.fields)
        if not fields:
            return 0
        where = await self._where(mutation.selector)
        if where is None:
            return 0
        if mutation.not_before is not None:
            column_name, when = mutation.not_before
            column = getattr(table, column_name)
            # Null means nothing has been recorded yet, so anything is newer.
            where = sa.and_(where, sa.or_(column.is_(None), column <= when))
        return await self._rows(sa.update(table).where(where).values(**fields))

    async def _increment(self, mutation: Increment) -> int:
        table = _TABLES[mutation.target]
        deltas = _checked(table, mutation.deltas)
        if not deltas:
            return 0
        where = await self._where(mutation.selector)
        if where is None:
            return 0
        # `coalesce` because a counter that has never been set is NULL, and
        # NULL + 1 is NULL - which would silently zero a count rather than
        # start it.
        values = {
            name: sa.func.coalesce(getattr(table, name), 0) + delta
            for name, delta in deltas.items()
        }
        return await self._rows(sa.update(table).where(where).values(**values))

    async def _append(self, mutation: Append, *, event_id: UUID) -> int:
        """The two tables that accumulate rather than being overwritten."""
        if isinstance(mutation.selector, AgentStatusRef):
            return await self._append_status(mutation, event_id=event_id)
        if isinstance(mutation.selector, RunStepRef):
            return await self._append_run_step(mutation)
        return 0

    async def _append_status(self, mutation: Append, *, event_id: UUID) -> int:
        """One row per transition, with `from_status` read in the same statement.

        A reducer cannot know what the status was a moment ago, so it leaves
        the column out. Reading it here as part of the INSERT - rather than in
        a separate SELECT - means no window in which another transaction could
        change it between the two.
        """
        selector = mutation.selector
        assert isinstance(selector, AgentStatusRef)
        project_id = self.scope.require_project()

        source = (
            sa.select(
                m.Agent.id,
                sa.literal(self.scope.workspace_id),
                sa.literal(project_id),
                m.Agent.status,
                sa.literal(mutation.fields["to_status"]),
                sa.literal(mutation.fields.get("reason")),
                sa.literal(event_id),
                sa.literal(mutation.fields["changed_at"]),
            )
            .where(m.Agent.project_id == project_id, m.Agent.agent_key == selector.agent_key)
            .limit(1)
        )
        return await self._rows(
            sa.insert(m.AgentStatusHistory).from_select(
                [
                    "agent_id",
                    "workspace_id",
                    "project_id",
                    "from_status",
                    "to_status",
                    "reason",
                    "event_id",
                    "changed_at",
                ],
                source,
            )
        )

    async def _append_run_step(self, mutation: Append) -> int:
        """A trace step, keyed by its run's own sequence.

        `ON CONFLICT DO NOTHING`: a retried batch that got past the dedup
        check - because the first attempt's transaction had not committed -
        must not write the step twice.
        """
        selector = mutation.selector
        assert isinstance(selector, RunStepRef)
        run_id = await self._run_id(selector.run_ref)
        if run_id is None:
            return 0

        fields = _checked(m.RunStep, mutation.fields)
        return await self._rows(
            sa.dialects.postgresql.insert(m.RunStep)
            .values(
                run_id=run_id,
                workspace_id=self.scope.workspace_id,
                project_id=self.scope.require_project(),
                seq=selector.seq,
                **fields,
            )
            .on_conflict_do_nothing()
        )

    async def _rows(self, statement: sa.Executable) -> int:
        """Run a DML statement and report how many rows it touched.

        `session.execute` is typed as returning a `Result`; a DML statement
        really returns a `CursorResult`, which is the one that carries
        `rowcount`. The cast says so in one place instead of four.
        """
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return result.rowcount or 0

    # --------------------------------------------------------- selectors ---

    async def _where(self, selector: Selector) -> sa.ColumnElement[bool] | None:
        """The WHERE clause for one selector, or None if it names nothing."""
        project_id = self.scope.require_project()

        if isinstance(selector, AgentRef):
            return sa.and_(
                m.Agent.project_id == project_id, m.Agent.agent_key == selector.agent_key
            )
        if isinstance(selector, TaskRef):
            return sa.and_(m.Task.project_id == project_id, m.Task.task_ref == selector.task_ref)
        if isinstance(selector, RunRef):
            return sa.and_(m.Run.project_id == project_id, m.Run.run_ref == selector.run_ref)
        if isinstance(selector, RunStepRef):
            run_id = await self._run_id(selector.run_ref)
            if run_id is None:
                return None
            return sa.and_(m.RunStep.run_id == run_id, m.RunStep.seq == selector.seq)
        if isinstance(selector, ExecutionRef):
            execution_id = _as_uuid(selector.execution_ref)
            if execution_id is None:
                return None
            return sa.and_(
                m.WorkflowExecution.project_id == project_id,
                m.WorkflowExecution.id == execution_id,
            )
        if isinstance(selector, StepRef):
            execution_id = _as_uuid(selector.execution_ref)
            if execution_id is None:
                return None
            return sa.and_(
                m.ExecutionStep.project_id == project_id,
                m.ExecutionStep.execution_id == execution_id,
                m.ExecutionStep.step_key == selector.step_key,
            )
        # AgentStatusRef only ever appends, and `_append` handles it.
        return None

    async def _run_id(self, run_ref: str) -> UUID | None:
        return (
            await self._session.execute(
                sa.select(m.Run.id)
                .where(
                    m.Run.project_id == self.scope.require_project(),
                    m.Run.run_ref == run_ref,
                )
                .limit(1)
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------- helpers ---


def _checked(table: type[m.Base], fields: Mapping[str, Any]) -> dict[str, Any]:
    """Drop anything that is not a column on `table`.

    Dropping rather than raising, because the reducers are already tested
    against the real schema and the remaining risk is a *migration* removing
    a column one still writes. Failing the whole ingest batch for that would
    turn a cosmetic drift into an outage; the integration test is what
    catches it, loudly, before a deploy.
    """
    columns = set(table.__table__.columns.keys())
    return {name: value for name, value in fields.items() if name in columns}


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None

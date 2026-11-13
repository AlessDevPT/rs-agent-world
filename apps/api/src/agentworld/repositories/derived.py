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
from typing import Any, Final, cast
from uuid import UUID, uuid5

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
from agentworld.repositories.events import ResolvedRefs

#: The namespace a run's id is derived in. A fixed uuid4, generated once and
#: written down rather than computed, because the whole point is that it
#: never changes: `uuid5(RUN_NAMESPACE, ...)` has to produce the same id in
#: this release and in every later one, or refolding the stored log would
#: rebuild `runs` under new ids and `run_steps` would point at rows that are
#: no longer there.
RUN_NAMESPACE: Final[UUID] = UUID("8da617d2-1912-4443-8959-a725338de808")


def run_id_for(project_id: UUID, run_ref: str) -> UUID:
    """The id a run gets, as a function of what names it.

    Deterministic rather than random, so replay equivalence holds on the
    `id` column too: `test_refolding_the_stored_log_reproduces_the_state`
    compares whole rows, and a `gen_random_uuid()` default would differ on
    every rebuild - taking `run_steps.run_id` with it, since the steps point
    at whatever id their run was given.

    `RUN_NAMESPACE` is fixed for the same reason, and must never change once
    a production log exists: the ids in that log were computed with it, and
    a new namespace would rebuild every run under an id that `run_steps`
    does not point at.

    **The reference goes in exactly as it was sent**, with no case folding
    and no normalization of any kind. `run_ref` is `citext`, so which
    references are the same is the database's question, answered by
    `runs_project_ref_key` - not this function's. Python cannot answer it
    anyway: `citext` compares by `lower()` under the database's collation
    while `str.casefold()` is full Unicode folding, and the two disagree in
    both directions. Folding here would collapse references PostgreSQL keeps
    apart - LATIN SMALL LETTER SHARP S against `ss` is the short example -
    and the second run's insert would meet this id as a primary key
    conflict, be swallowed by `ON CONFLICT DO NOTHING`, and never exist.

    So the identity computed here is deliberately *finer* than the
    database's, which is the safe direction. Two spellings PostgreSQL calls
    equivalent get two ids and still make one row: the second insert
    conflicts on `runs_project_ref_key` instead, is swallowed in the same
    way, and the patch behind it matches the row already there - by
    `run_ref`, through the same `citext` comparison that made them
    equivalent in the first place. One run, one row, the spelling that
    arrived first.

    The ids are deterministic, not secret. Anyone who knows a project id and
    a run reference can compute one, and that costs nothing here: no route
    reads a run by id, and every read is scoped to a project the caller is
    authorized for. An id is a name in this system, never a capability.
    """
    return uuid5(RUN_NAMESPACE, f"{project_id}:{run_ref}")


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

    async def apply(
        self, mutation: Mutation, *, event_id: UUID, refs: ResolvedRefs | None = None
    ) -> int:
        """Apply one mutation, returning how many rows it changed.

        `refs` is what the ingest transaction already resolved for this
        event - the same project-scoped lookup that filled the event row's
        own columns, passed down rather than repeated, so the event and the
        rows folded from it cannot disagree about whose work they describe.

        Optional because only one mutation needs it: creating a run needs
        the agent that owns it, and `runs.agent_id` is NOT NULL. The
        orchestrator's callers fold workflow and approval events, which name
        no agent key and create no run, so their calls are unchanged.
        """
        if isinstance(mutation, Append):
            return await self._append(mutation, event_id=event_id, refs=refs)
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
        if mutation.unless is not None:
            column_name, values = mutation.unless
            column = getattr(table, column_name)
            # `notin_` and not `!=`: the column is a native enum, and the
            # comparison has to hold for every member of the forbidden set.
            where = sa.and_(where, column.notin_(sorted(values)))
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

    async def _append(self, mutation: Append, *, event_id: UUID, refs: ResolvedRefs | None) -> int:
        """One row added, to whichever of the three tables the selector names."""
        if isinstance(mutation.selector, AgentStatusRef):
            return await self._append_status(mutation, event_id=event_id)
        if isinstance(mutation.selector, RunStepRef):
            return await self._append_run_step(mutation)
        if isinstance(mutation.selector, RunRef):
            return await self._append_run(mutation, refs=refs)
        return 0

    async def _append_run(self, mutation: Append, *, refs: ResolvedRefs | None) -> int:
        """The run an external agent says it started.

        **The only statement in this system that creates a run.** Everything
        else about a run - its steps, its counters, how it ended - patches a
        row this put there, which is why `GET .../agents/{id}/runs` returned
        an empty list for every agent before R1.

        `agent_id` comes from the ingest transaction's own resolution rather
        than a second lookup here, so the run and the event row agree on the
        owner by construction. It is NOT NULL, so an event whose correlation
        names no agent, an agent in another project, or one that does not
        exist writes nothing and changes no rows - which is what every other
        unresolved reference already does, and is not an error. An archived
        agent resolves like any other and gets its run: archiving stops an
        agent being used, not having been.

        `ON CONFLICT DO NOTHING` without naming an index, so both keys are
        covered: the primary key, because the id is a function of the
        reference and so repeats, and `runs_project_ref_key`, because the
        reference itself is unique per project. Neither should be reachable -
        a duplicate event is refused before it is folded - so this is the
        second answer to a question that should only ever be asked once:
        during a replay, and in whatever race the dedup check does not see.
        """
        selector = mutation.selector
        assert isinstance(selector, RunRef)
        if refs is None or refs.agent_id is None:
            return 0

        project_id = self.scope.require_project()
        fields = _checked(m.Run, mutation.fields)
        return await self._rows(
            sa.dialects.postgresql.insert(m.Run)
            .values(
                id=run_id_for(project_id, selector.run_ref),
                workspace_id=self.scope.workspace_id,
                project_id=project_id,
                run_ref=selector.run_ref,
                agent_id=refs.agent_id,
                **fields,
            )
            .on_conflict_do_nothing()
        )

    async def _append_status(self, mutation: Append, *, event_id: UUID) -> int:
        """One row per transition, with `from_status` read in the same statement.

        A reducer cannot know what the status was a moment ago, so it leaves
        the column out. Reading it here as part of the INSERT - rather than in
        a separate SELECT - means no window in which another transaction could
        change it between the two.

        **This runs before the patch that moves the status**, which `fold`
        guarantees by emitting the append first. Until stage D1.1 it ran
        after, so `m.Agent.status` was already the new value and every row
        recorded the status it had moved to as the one it came from.

        `not_before` is applied to the select, so a stale event inserts
        nothing: `INSERT … FROM SELECT` writes a row only if the select
        returns one, and the condition is the same one the patch carries.
        An event therefore moves the status and records the move, or does
        neither.
        """
        selector = mutation.selector
        assert isinstance(selector, AgentStatusRef)
        project_id = self.scope.require_project()

        conditions = [
            m.Agent.project_id == project_id,
            m.Agent.agent_key == selector.agent_key,
        ]
        if mutation.not_before is not None:
            column_name, when = mutation.not_before
            column = getattr(m.Agent, column_name)
            # Null means nothing has been recorded yet, so anything is newer -
            # the wording and the comparison are `_patch`'s, because the two
            # have to agree for the pair to be all-or-nothing.
            conditions.append(sa.or_(column.is_(None), column <= when))

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
            .where(*conditions)
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

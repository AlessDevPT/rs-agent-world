"""Appending events, and resolving the references they carry.

Two jobs that belong together because both need the project pinned and both
run inside the ingest transaction.

**Allocating `seq`.** One row per project, locked `FOR UPDATE`, so two
concurrent batches for a project queue rather than racing for a number. That
serialises a project's ingest, which migration 0015 argues is the right trade
for a cursor readers depend on being ordered and gapless.

**Resolving references.** A producer sends names - `agent_key`, `task_ref`,
`run_ref` - because it cannot know a database id. This turns them into ids
where a row exists, and into nothing where it does not. An unresolved
reference is not an error: an event about an agent that was deleted is still
a true statement about the past, and refusing it would lose history to tidy
up a foreign key that deliberately is not there.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import InstrumentedAttribute

from agentworld.domain.enums import EventSeverity
from agentworld.domain.protocol import EventEnvelope
from agentworld.repositories import models as m
from agentworld.repositories.base import ScopedRepository


@dataclass(frozen=True, slots=True)
class LogLine:
    """One log line, as a producer sends it."""

    message: str
    occurred_at: datetime
    severity: EventSeverity = EventSeverity.INFO
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One numeric observation."""

    name: str
    value: float
    occurred_at: datetime
    unit: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolvedRefs:
    """The ids an event's references pointed at, where they pointed anywhere."""

    agent_id: UUID | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    execution_id: UUID | None = None
    execution_step_id: UUID | None = None


class EventRepository(ScopedRepository):
    """The `events` table, pinned to one project."""

    async def lock_project_cursor(self) -> None:
        """Take this project's ingest lock for the rest of the transaction.

        `next_seq` already locks the cursor row, but it is only reached for
        the *first event that is not a duplicate* - so the dedup check ran
        unserialised, and an all-duplicate batch took no lock at all. Two
        concurrent batches carrying one `dedup_key` could therefore both see
        `seen() is False`, and the second would lose to the partition's
        unique index mid-transaction, failing the whole batch instead of
        reporting one duplicate.

        Calling this once before the loop is what migration 0015 already
        says the design is: a project's ingest is serialised. The lock was
        simply being taken too late to serialise the part that needed it.

        Idempotent within a transaction - taking a row lock twice is free -
        so `next_seq` keeps its own and remains correct when called alone.
        """
        await self._ensure_cursor()

    async def next_seq(self) -> int:
        """Reserve this project's next sequence number.

        Creates the cursor row on first use, so a project that has never been
        written to needs no backfill. The `FOR UPDATE` is what makes the
        number ours until the transaction ends: without it two batches read
        the same value and the second loses to the primary key - after doing
        all the work of folding its events.
        """
        project_id = self.scope.require_project()
        current = await self._ensure_cursor()
        await self._session.execute(
            sa.update(m.ProjectEventCursor)
            .where(m.ProjectEventCursor.project_id == project_id)
            .values(next_seq=current + 1, updated_at=sa.func.now())
        )
        return int(current)

    async def _ensure_cursor(self) -> int:
        """Create the cursor row if absent, lock it, and return `next_seq`."""
        project_id = self.scope.require_project()
        await self._session.execute(
            sa.dialects.postgresql.insert(m.ProjectEventCursor)
            .values(workspace_id=self.scope.workspace_id, project_id=project_id, next_seq=1)
            .on_conflict_do_nothing(index_elements=["project_id"])
        )
        return int(
            (
                await self._session.execute(
                    sa.select(m.ProjectEventCursor.next_seq)
                    .where(m.ProjectEventCursor.project_id == project_id)
                    .with_for_update()
                )
            ).scalar_one()
        )

    async def seen(self, dedup_key: str) -> bool:
        """Whether this project already holds an event with that key.

        Checked explicitly rather than left to the unique index, for two
        reasons. The index lives on each monthly partition, so it would not
        catch a retry that crossed a month boundary - migration 0011 names
        that window. And a conflict raised mid-transaction would abort the
        whole batch, when the right answer to a duplicate is to skip it and
        carry on with the rest.
        """
        found = (
            await self._session.execute(
                sa.select(sa.literal(1))
                .select_from(m.Event)
                .where(
                    m.Event.project_id == self.scope.require_project(),
                    m.Event.dedup_key == dedup_key,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found is not None

    async def resolve(self, envelope: EventEnvelope) -> ResolvedRefs:
        """Turn the producer's names into ids, where they name something.

        The subject counts as a reference too: an event whose subject is an
        agent names that agent without repeating it in `correlation`.
        """
        correlation = envelope.correlation
        subject_kind, subject_ref = envelope.subject.kind, envelope.subject.ref

        agent_key = correlation.agent_key or (subject_ref if subject_kind == "agent" else None)
        task_ref = correlation.task_ref or (subject_ref if subject_kind == "task" else None)
        run_ref = correlation.run_ref or (subject_ref if subject_kind == "run" else None)
        execution_ref = correlation.execution_ref or (
            subject_ref if subject_kind == "workflow_execution" else None
        )

        return ResolvedRefs(
            agent_id=await self._lookup(m.Agent, m.Agent.agent_key, agent_key),
            task_id=await self._lookup(m.Task, m.Task.task_ref, task_ref),
            run_id=await self._lookup(m.Run, m.Run.run_ref, run_ref),
            execution_id=await self._execution(execution_ref),
        )

    async def append(self, envelope: EventEnvelope, *, seq: int, refs: ResolvedRefs) -> UUID:
        """Write the event row. The only INSERT this repository performs.

        `received_at` is left to the server's `now()`: it is the column that
        makes a producer's clock checkable, so taking it from the producer
        would defeat the point of having both.
        """
        result = await self._session.execute(
            sa.insert(m.Event)
            .values(
                workspace_id=self.scope.workspace_id,
                project_id=self.scope.require_project(),
                seq=seq,
                dedup_key=envelope.dedup_key,
                type=envelope.type,
                protocol_version=envelope.protocol_version,
                occurred_at=envelope.occurred_at,
                subject_kind=envelope.subject.kind,
                subject_ref=envelope.subject.ref,
                agent_id=refs.agent_id,
                task_id=refs.task_id,
                run_id=refs.run_id,
                execution_id=refs.execution_id,
                execution_step_id=refs.execution_step_id,
                correlation=_correlation_json(envelope),
                severity=envelope.severity,
                labels=dict(envelope.labels),
                payload=dict(envelope.payload),
            )
            .returning(m.Event.id)
        )
        return result.scalar_one()

    # ------------------------------------------------------------ lookups ---

    async def _lookup(
        self,
        table: type[m.Base],
        # Loosely typed because the three reference columns are not all
        # nullable in the same way, and narrowing here would buy nothing:
        # the query is the same shape whichever one it is.
        column: InstrumentedAttribute[Any],
        ref: str | None,
    ) -> UUID | None:
        if not ref:
            return None
        found = (
            await self._session.execute(
                sa.select(table.id)  # type: ignore[attr-defined]
                .where(
                    table.project_id == self.scope.require_project(),  # type: ignore[attr-defined]
                    column == ref,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found

    async def _execution(self, ref: str | None) -> UUID | None:
        """Executions are server-created, so their ref is their id.

        Unlike agents, tasks and runs, `workflow_executions` has no
        producer-facing reference column: nothing outside AgentWorld creates
        an execution, so there is no name for an outsider to have chosen. A
        ref that is not a uuid therefore resolves to nothing - which is the
        case for the illustrative `exec-9f2a` in the proposal, and for any
        producer inventing its own. When the orchestrator lands in Phase 3 it
        emits the execution's own id, and these resolve.
        """
        if not ref:
            return None
        try:
            candidate = UUID(ref)
        except ValueError:
            return None
        return (
            await self._session.execute(
                sa.select(m.WorkflowExecution.id)
                .where(
                    m.WorkflowExecution.project_id == self.scope.require_project(),
                    m.WorkflowExecution.id == candidate,
                )
                .limit(1)
            )
        ).scalar_one_or_none()


def _correlation_json(envelope: EventEnvelope) -> dict[str, object]:
    """The correlation as sent, including keys this release does not know.

    Stored verbatim so an upgrade can start reading a key that older rows
    already carry, which is the point of having preserved it at the parser.
    """
    correlation = envelope.correlation
    known = {
        "agent_key": correlation.agent_key,
        "task_ref": correlation.task_ref,
        "run_ref": correlation.run_ref,
        "execution_ref": correlation.execution_ref,
        "execution_step_ref": correlation.execution_step_ref,
        "workflow_slug": correlation.workflow_slug,
    }
    return {
        **{key: value for key, value in known.items() if value is not None},
        **dict(correlation.extra),
    }


class TelemetryRepository(ScopedRepository):
    """Log lines and metric samples, which are not events.

    Worth being clear about why these have their own tables and their own
    endpoints rather than riding the event stream. An event is a statement
    about something that happened to an agent, a task or an execution, and
    folding it can change state. A log line is output, and a metric sample is
    a number; neither changes anything, both arrive at a volume that would
    make the event cursor churn, and a producer that filled `events` with
    debug lines would push its own real events out of a reader's window.

    The `log.emitted` and `metric.recorded` event types still exist, for a
    producer that wants one ordered stream of everything. They are recorded as
    events and fold into nothing - so sending the same line to both endpoints
    stores it twice, which is a producer's choice and not a silent one.
    """

    async def append_logs(self, lines: Sequence[LogLine], *, refs: ResolvedRefs) -> int:
        if not lines:
            return 0
        rows = [
            {
                "workspace_id": self.scope.workspace_id,
                "project_id": self.scope.require_project(),
                "agent_id": refs.agent_id,
                "task_id": refs.task_id,
                "run_id": refs.run_id,
                "severity": line.severity,
                "message": line.message,
                "context": dict(line.context),
                "occurred_at": line.occurred_at,
            }
            for line in lines
        ]
        await self._session.execute(sa.insert(m.Log), rows)
        return len(rows)

    async def append_metrics(self, samples: Sequence[MetricPoint], *, refs: ResolvedRefs) -> int:
        if not samples:
            return 0
        rows = [
            {
                "workspace_id": self.scope.workspace_id,
                "project_id": self.scope.require_project(),
                "agent_id": refs.agent_id,
                "name": sample.name,
                "value": sample.value,
                "unit": sample.unit,
                "labels": dict(sample.labels),
                "occurred_at": sample.occurred_at,
            }
            for sample in samples
        ]
        await self._session.execute(sa.insert(m.MetricSample), rows)
        return len(rows)

    async def resolve_agent(self, agent_key: str | None) -> ResolvedRefs:
        """Logs and metrics correlate to an agent and nothing else."""
        if not agent_key:
            return ResolvedRefs()
        found = (
            await self._session.execute(
                sa.select(m.Agent.id)
                .where(
                    m.Agent.project_id == self.scope.require_project(),
                    m.Agent.agent_key == agent_key,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return ResolvedRefs(agent_id=found)

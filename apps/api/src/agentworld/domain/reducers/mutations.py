"""What a reducer is allowed to ask for.

A reducer cannot write to a database - the domain layer imports no SQLAlchemy
and never will - so folding an event returns a description of the change and
the repository layer applies it. That indirection is usually a cost. Here it
buys the protocol's seventh promise outright:

> **An event never causes work.** Folding the log mutates state and nothing
> else. Every side effect originates from a `dispatches` row, which is not
> part of the log and is not replayed.

Read the vocabulary below and notice what is missing. There is no mutation
that writes a dispatch, calls a provider, sends a request or enqueues
anything. Not "there is one and the reducers do not use it" - there is no such
mutation to use. A reducer that wanted to cause work would have to invent a
type, and `test_reducers.py` asserts that the set of targets stays within the
execution tables. The promise therefore holds by construction rather than by
discipline, which matters because discipline is what fails at 2am during an
incident.

**Selectors are producer references, not database ids.** A reducer sees
`agent_key="researcher"`, never a uuid, because the domain has no way to know
one and an event's producer has no way to send one. Resolving a reference to a
row is the repository's job, and it may legitimately fail - an event about an
agent that was deleted is still a valid event.

**A stale event must not overwrite a newer one.** Events arrive in the order
a producer managed to send them, which is not the order things happened: a
worker that was briefly offline flushes a backlog, and an `agent.paused` from
two minutes ago lands after the `agent.resumed` that followed it. Applied
blindly, the agent ends up paused and the island shows it shuttered while it
works. So the patches that move a row's current state carry `not_before`, and
the repository adds a predicate on the column that records how recent the row
is. Replay is unaffected: it applies events in `seq` order, which is the same
order the guard already produced live.

**`Increment` is sound because application is exactly-once.** The unique index
on `(project_id, dedup_key)` refuses a duplicate inside the same transaction
that folds it, so a retried batch cannot double-count; and replay is a full
rebuild from an empty state rather than a re-application onto existing rows.
Both halves are needed, and if either changed, counters would be the first
thing to go wrong.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MutationTarget(StrEnum):
    """The tables folding an event may change.

    Exhaustive on purpose, and the reason `dispatches` is not here is the
    whole of promise 7. Nor are the definition tables - `agents` appears
    because an agent's *derived* state (its status, its last heartbeat) is
    folded, while its configuration is control-plane and never arrives as an
    event.
    """

    AGENTS = "agents"
    AGENT_STATUS_HISTORY = "agent_status_history"
    TASKS = "tasks"
    RUNS = "runs"
    RUN_STEPS = "run_steps"
    WORKFLOW_EXECUTIONS = "workflow_executions"
    EXECUTION_STEPS = "execution_steps"


# ---------------------------------------------------------------- selectors ---


@dataclass(frozen=True, slots=True)
class AgentRef:
    """An agent by the key it registered under, scoped to a project."""

    agent_key: str

    @property
    def target(self) -> MutationTarget:
        return MutationTarget.AGENTS


@dataclass(frozen=True, slots=True)
class AgentStatusRef:
    """A row to append to an agent's status history.

    A separate selector from `AgentRef`, because the target comes from the
    selector and these two write different tables for the same agent. Folding
    `agent.paused` produces both: a patch to `agents` and an append here. One
    selector for both would have made the append claim it targeted `agents`,
    which a test caught before the repository layer existed to be confused by
    it.
    """

    agent_key: str

    @property
    def target(self) -> MutationTarget:
        return MutationTarget.AGENT_STATUS_HISTORY


@dataclass(frozen=True, slots=True)
class TaskRef:
    task_ref: str

    @property
    def target(self) -> MutationTarget:
        return MutationTarget.TASKS


@dataclass(frozen=True, slots=True)
class RunRef:
    run_ref: str

    @property
    def target(self) -> MutationTarget:
        return MutationTarget.RUNS


@dataclass(frozen=True, slots=True)
class RunStepRef:
    """One step of a run's trace, identified by the run's own sequence."""

    run_ref: str
    seq: int

    @property
    def target(self) -> MutationTarget:
        return MutationTarget.RUN_STEPS


@dataclass(frozen=True, slots=True)
class ExecutionRef:
    execution_ref: str

    @property
    def target(self) -> MutationTarget:
        return MutationTarget.WORKFLOW_EXECUTIONS


@dataclass(frozen=True, slots=True)
class StepRef:
    """A step of an execution, by the key the published version gave it.

    `attempt` is absent deliberately: a retried step is the same step, and
    folding its retry onto a second row would make "how is this step doing?"
    a question with two answers.
    """

    execution_ref: str
    step_key: str

    @property
    def target(self) -> MutationTarget:
        return MutationTarget.EXECUTION_STEPS


Selector = AgentRef | AgentStatusRef | TaskRef | RunRef | RunStepRef | ExecutionRef | StepRef


# ---------------------------------------------------------------- mutations ---


@dataclass(frozen=True, slots=True)
class Patch:
    """Set these fields on the row the selector names, if it exists.

    Absence is not an error. An event about a deleted agent, or one whose ref
    never resolved, folds to a patch that matches nothing - and the event is
    still recorded, which is the behaviour that keeps history honest.
    """

    selector: Selector
    fields: Mapping[str, Any]
    #: `(column, when)` - apply only if the row's `column` is null or not
    #: already newer than `when`. See the note below.
    not_before: tuple[str, dt.datetime] | None = None

    @property
    def target(self) -> MutationTarget:
        return self.selector.target


@dataclass(frozen=True, slots=True)
class Append:
    """Add a row. Used for the two tables that are logs of their own.

    `agent_status_history` and `run_steps` accumulate rather than being
    overwritten, so an append is the only honest shape for them.
    """

    selector: Selector
    fields: Mapping[str, Any]

    @property
    def target(self) -> MutationTarget:
        return self.selector.target


@dataclass(frozen=True, slots=True)
class Increment:
    """Add these deltas to the row's counters.

    Sound only because application is exactly-once and replay is a full
    rebuild - see the module docstring. Values are signed, though every
    current use is positive.
    """

    selector: Selector
    deltas: Mapping[str, int]

    @property
    def target(self) -> MutationTarget:
        return self.selector.target


Mutation = Patch | Append | Increment

__all__ = [
    "AgentRef",
    "AgentStatusRef",
    "Append",
    "ExecutionRef",
    "Increment",
    "Mutation",
    "MutationTarget",
    "Patch",
    "RunRef",
    "RunStepRef",
    "Selector",
    "StepRef",
    "TaskRef",
]

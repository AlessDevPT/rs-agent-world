"""Folding an event into state changes. Pure, total, and unable to cause work.

`fold(envelope)` returns the mutations an event implies and does nothing else.
No clock - every timestamp written comes from the event's own `occurred_at`,
because a reducer that read `now()` would produce different state on replay
than it did live, and replay equivalence is the property the whole log exists
to provide. No database, no registry lookups, no resolution of refs to ids:
those belong to the repository that applies the result.

**Total, not partial.** An unknown type folds to nothing. A known type whose
refs cannot be resolved folds to nothing. Neither is an error, and neither
stops the event being recorded - a log that drops what it cannot interpret is
not a log.

Kept in one module rather than six: each namespace below is some thirty lines
and they share the same three ref helpers, so splitting them would mostly
produce imports. The sections are the namespaces, in the taxonomy's order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from agentworld.domain.enums import (
    AgentStatus,
    ExecutionStatus,
    RunStatus,
    StepStatus,
    TaskStatus,
)
from agentworld.domain.protocol.refs import split_step_ref
from agentworld.domain.protocol.types import EventEnvelope
from agentworld.domain.reducers.mutations import (
    AgentRef,
    AgentStatusRef,
    Append,
    ExecutionRef,
    Increment,
    Mutation,
    Patch,
    RunRef,
    RunStepRef,
    StepRef,
    TaskRef,
)

#: "Recorded, folds into nothing" - returned often enough to be named, so a
#: reader can tell it apart from a reducer that forgot to return.
_NOTHING: Final[tuple[Mutation, ...]] = ()

# -------------------------------------------------- the monotonicity sets ---
#
# What `Patch.unless` is given: the statuses that are already final, so a late
# or duplicate event cannot reopen them. Four sets rather than one, because
# four different native enums are involved and a status string from the wrong
# one would silently never match.
#
# These are *settled* rather than *terminal* in the type's sense: an execution
# that is `paused` may still progress, so it is not here.

_Settled = tuple[str, frozenset[str]]

_SETTLED_RUNS: Final[_Settled] = (
    "status",
    frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}),
)
#: `run_steps.status` is the `run_status` enum, not `step_status`, so it shares
#: the set above rather than the step one below.
_SETTLED_RUN_STEPS: Final[_Settled] = _SETTLED_RUNS
_SETTLED_EXECUTIONS: Final[_Settled] = (
    "status",
    frozenset(status for status in ExecutionStatus if not status.is_live),
)
_SETTLED_STEPS: Final[_Settled] = (
    "status",
    frozenset(status for status in StepStatus if status.is_terminal),
)


#: Every reducer has this shape, which is also the whole of what a reducer
#: may do: envelope in, mutations out, no second argument to reach through.
Reducer = Callable[[EventEnvelope], tuple[Mutation, ...]]


def fold(envelope: EventEnvelope) -> tuple[Mutation, ...]:
    """The mutations `envelope` implies, in the order they must be applied."""
    namespace, _, _ = envelope.type.partition(".")
    reducer = _REDUCERS.get(namespace)
    return reducer(envelope) if reducer else _NOTHING


# -------------------------------------------------------------------- agent ---


def _agent(envelope: EventEnvelope) -> tuple[Mutation, ...]:
    """An agent's derived state: status, heartbeat, capabilities.

    Not its configuration. `agents.display_name` and the rest are control
    plane, they arrive over the API with an `audit_log` row, and no event
    here can reach them - which is what keeps a replay from re-creating a
    definition.
    """
    selector = AgentRef(agent_key=envelope.subject.ref)
    seen = {"last_event_at": envelope.occurred_at}
    name = envelope.type.split(".", 1)[1]
    # A backlog flushed by a worker that was offline arrives in send
    # order, not in the order things happened. Without this, the oldest
    # event in the batch decides what the agent looks like.
    fresh = ("last_event_at", envelope.occurred_at)

    if name == "heartbeat":
        return (
            Patch(
                selector,
                {**seen, "last_heartbeat_at": envelope.occurred_at},
                not_before=fresh,
            ),
        )

    if name == "registered":
        # `registered_at` and the declared shape of the worker. The agent row
        # itself already exists: registration announces a worker for an agent
        # someone configured, it does not create one.
        return (
            Patch(
                selector,
                {
                    **seen,
                    "registered_at": envelope.occurred_at,
                    "last_heartbeat_at": envelope.occurred_at,
                    "status": AgentStatus.IDLE,
                    "status_reason": None,
                    **_declared(envelope.payload),
                },
                not_before=fresh,
            ),
        )

    if name == "capability_changed":
        return (Patch(selector, {**seen, **_declared(envelope.payload)}, not_before=fresh),)

    if status := _agent_status_for(name, envelope.payload):
        reason = _text(envelope.payload.get("reason"))
        return (
            # **The append comes first, and the order is the whole of
            # `from_status`.** The history row records the transition itself,
            # and targets a different table, so it takes its own selector;
            # `from_status` is left to the repository, which is the only
            # layer that can read what the status was a moment ago. It can
            # only read that *before* the patch below overwrites it - which
            # is why these two are in this order and not the other, and why
            # stage D1.1 exists: for as long as the patch came first, every
            # history row said it came from where it went.
            Append(
                AgentStatusRef(agent_key=envelope.subject.ref),
                {"to_status": status, "reason": reason, "changed_at": envelope.occurred_at},
                not_before=fresh,
            ),
            # The same guard, deliberately the same object: a late event
            # must either move the status and record the move, or do
            # neither. One of the two alone would be a history that disagrees
            # with the agent.
            Patch(
                selector,
                {**seen, "status": status, "status_reason": reason},
                not_before=fresh,
            ),
        )

    return (Patch(selector, seen, not_before=fresh),)


def _agent_status_for(name: str, payload: Mapping[str, Any]) -> AgentStatus | None:
    if name == "paused":
        return AgentStatus.PAUSED
    if name == "resumed":
        return AgentStatus.IDLE
    if name == "deregistered":
        return AgentStatus.OFFLINE
    if name == "status_changed":
        # The producer names the status. An unknown one folds to nothing
        # rather than being coerced: `agents.status` is a native enum, and
        # inventing a value here would record one nobody sent.
        raw = payload.get("status")
        if isinstance(raw, str):
            try:
                return AgentStatus(raw)
            except ValueError:
                return None
    return None


def _declared(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of a registration a worker is entitled to assert.

    The shapes are not interchangeable, which an earlier version of this
    function got wrong: `agents.capabilities` is `text[]` and the other two
    are `jsonb`, so a producer sending an object for capabilities would have
    failed the whole ingest transaction with a type error rather than being
    quietly ignored. Anything of the wrong shape is dropped - a registration
    is a worker describing itself, and a malformed self-description should
    not take the batch down with it.
    """
    declared: dict[str, Any] = {}

    capabilities = payload.get("capabilities")
    if isinstance(capabilities, list) and all(isinstance(entry, str) for entry in capabilities):
        declared["capabilities"] = capabilities

    for field in ("labels", "runtime"):
        value = payload.get(field)
        if isinstance(value, Mapping):
            declared[field] = dict(value)

    ttl = payload.get("heartbeat_ttl_seconds")
    if isinstance(ttl, int) and not isinstance(ttl, bool) and 0 < ttl <= 86_400:
        declared["heartbeat_ttl_seconds"] = ttl
    return declared


# --------------------------------------------------------------------- task ---

_TASK_STATUS: Final[Mapping[str, TaskStatus]] = {
    "created": TaskStatus.PENDING,
    "assigned": TaskStatus.QUEUED,
    "started": TaskStatus.RUNNING,
    "blocked": TaskStatus.BLOCKED,
    "unblocked": TaskStatus.QUEUED,
    "completed": TaskStatus.COMPLETED,
    "failed": TaskStatus.FAILED,
    "cancelled": TaskStatus.CANCELLED,
}


def _task(envelope: EventEnvelope) -> tuple[Mutation, ...]:
    selector = TaskRef(task_ref=envelope.subject.ref)
    name = envelope.type.split(".", 1)[1]
    payload = envelope.payload
    # Guarded on `last_event_at`, not on `updated_at`: the latter moves
    # whenever anything touches the row, including the API creating it, so a
    # guard on it refuses every event about a task someone made first.
    seen = {"last_event_at": envelope.occurred_at, "updated_at": envelope.occurred_at}
    fresh = ("last_event_at", envelope.occurred_at)

    if name == "progress":
        fields: dict[str, Any] = {}
        percent = payload.get("percent")
        if isinstance(percent, int) and not isinstance(percent, bool) and 0 <= percent <= 100:
            fields["progress_percent"] = percent
        if message := _text(payload.get("message")):
            fields["progress_message"] = message
        # No status change: progress on a task that is not running would be
        # a producer bug, and quietly starting it here would hide it.
        if not fields:
            return _NOTHING
        return (Patch(selector, {**fields, **seen}, not_before=fresh),)

    status = _TASK_STATUS.get(name)
    if status is None:
        return _NOTHING

    fields = {"status": status, **seen}
    if name == "assigned":
        fields["queued_at"] = envelope.occurred_at
    if name == "started":
        fields["started_at"] = envelope.occurred_at
    if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        fields["finished_at"] = envelope.occurred_at
    if status is TaskStatus.COMPLETED:
        fields["progress_percent"] = 100
        if (result := payload.get("result")) is not None:
            fields["result"] = result
    if error := _error(payload):
        fields["error"] = error
    return (Patch(selector, fields, not_before=fresh),)


# ---------------------------------------------------------------------- run ---


def _run(envelope: EventEnvelope) -> tuple[Mutation, ...]:
    """A run and its trace. The one namespace that appends as well as patches."""
    name = envelope.type.split(".", 1)[1]
    run = RunRef(run_ref=envelope.subject.ref)
    payload = envelope.payload
    seen = {"last_event_at": envelope.occurred_at}
    fresh = ("last_event_at", envelope.occurred_at)

    if name == "started":
        return (
            Patch(
                run,
                {"status": RunStatus.RUNNING, "started_at": envelope.occurred_at, **seen},
                not_before=fresh,
                unless=_SETTLED_RUNS,
            ),
        )
    if name == "finished":
        return (
            Patch(
                run,
                {"status": RunStatus.SUCCEEDED, "finished_at": envelope.occurred_at, **seen},
                not_before=fresh,
                unless=_SETTLED_RUNS,
            ),
        )
    if name == "failed":
        return (
            Patch(
                run,
                {
                    "status": RunStatus.FAILED,
                    "finished_at": envelope.occurred_at,
                    "error": _error(payload),
                    **seen,
                },
                not_before=fresh,
                unless=_SETTLED_RUNS,
            ),
        )

    seq = payload.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        # A trace step with no sequence cannot be placed in the trace.
        return _NOTHING
    step = RunStepRef(run_ref=envelope.subject.ref, seq=seq)

    if name == "step_started":
        return (
            Append(
                step,
                {
                    "name": _text(payload.get("name")) or f"step-{seq}",
                    "kind": _text(payload.get("kind")),
                    "status": StepStatus.RUNNING,
                    "input": payload.get("input"),
                    "started_at": envelope.occurred_at,
                    **seen,
                },
            ),
        )
    if name == "step_finished":
        failed = bool(payload.get("error"))
        return (
            Patch(
                step,
                {
                    "status": StepStatus.FAILED if failed else StepStatus.SUCCEEDED,
                    "output": payload.get("output"),
                    "error": _error(payload),
                    "finished_at": envelope.occurred_at,
                    **seen,
                },
                not_before=fresh,
                unless=_SETTLED_RUN_STEPS,
            ),
            # `runs.step_count` is a counter rather than a query, because the
            # island reads it per frame and counting rows per agent per frame
            # is the kind of query that looks free until there are a thousand.
            Increment(run, {"step_count": 1}),
        )
    return _NOTHING


# --------------------------------------------------------------------- tool ---


def _tool(envelope: EventEnvelope) -> tuple[Mutation, ...]:
    """Tool calls count against the run that made them.

    `tool.denied` counts too, deliberately. A refused call is work the agent
    tried to do, and leaving it out of the count would make a blocked agent
    look like an idle one - the same reason the event type exists at all.
    """
    run_ref = envelope.correlation.run_ref or (
        envelope.subject.ref if envelope.subject.kind == "run" else None
    )
    if not run_ref:
        return _NOTHING
    if envelope.type == "tool.invoked":
        return (Increment(RunRef(run_ref=run_ref), {"tool_call_count": 1}),)
    return _NOTHING


# ----------------------------------------------------------------- workflow ---

_STEP_STATUS: Final[Mapping[str, StepStatus]] = {
    "step_ready": StepStatus.READY,
    "step_dispatched": StepStatus.DISPATCHED,
    "step_started": StepStatus.RUNNING,
    "step_succeeded": StepStatus.SUCCEEDED,
    "step_failed": StepStatus.FAILED,
    "step_skipped": StepStatus.SKIPPED,
}

_EXECUTION_STATUS: Final[Mapping[str, ExecutionStatus]] = {
    "execution_started": ExecutionStatus.RUNNING,
    "execution_completed": ExecutionStatus.COMPLETED,
    "execution_failed": ExecutionStatus.FAILED,
    "execution_cancelled": ExecutionStatus.CANCELLED,
    "execution_paused": ExecutionStatus.PAUSED,
    "execution_resumed": ExecutionStatus.RUNNING,
}


def _workflow(envelope: EventEnvelope) -> tuple[Mutation, ...]:
    """The orchestrator's record of decisions it already made.

    Worth being clear about the direction: these events do not *cause* the
    orchestrator to do anything. `advance()` decided, wrote the event and the
    dispatch row in one transaction, and this folds the event half. Replaying
    it reproduces the state and dispatches nothing, because there is no
    mutation here that could.
    """
    name = envelope.type.split(".", 1)[1]
    payload = envelope.payload
    seen = {"last_event_at": envelope.occurred_at}
    fresh = ("last_event_at", envelope.occurred_at)

    if (status := _EXECUTION_STATUS.get(name)) is not None:
        selector = ExecutionRef(execution_ref=envelope.subject.ref)
        fields: dict[str, Any] = {"status": status, **seen}
        if name == "execution_started":
            fields["started_at"] = envelope.occurred_at
        if not status.is_live:
            fields["finished_at"] = envelope.occurred_at
        if (output := payload.get("output")) is not None:
            fields["output"] = output
        if error := _error(payload):
            fields["error"] = error
        return (Patch(selector, fields, not_before=fresh, unless=_SETTLED_EXECUTIONS),)

    step_status = _STEP_STATUS.get(name)
    if step_status is None and name != "step_retried":
        return _NOTHING

    reference = split_step_ref(
        envelope.subject.ref, execution_ref=envelope.correlation.execution_ref
    )
    if reference is None:
        # A bare step key, or a correlation that disagrees with the ref. The
        # event is recorded; nothing identifies the step to fold it onto.
        return _NOTHING
    step = StepRef(execution_ref=reference.execution_ref, step_key=reference.step_key)

    if name == "step_retried":
        attempt = payload.get("attempt")
        fields = {
            "status": StepStatus.READY,
            "error": None,
            "finished_at": None,
            **seen,
        }
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0:
            fields["attempt"] = attempt
        # No `unless`. Every other step patch refuses to touch a settled step;
        # this one exists precisely to reopen one, and a retry that could not
        # would make the whole retry path a no-op against a failed step.
        return (Patch(step, fields, not_before=fresh),)

    fields = {"status": step_status, **seen}
    if name == "step_started":
        fields["started_at"] = envelope.occurred_at
    if step_status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED):
        fields["finished_at"] = envelope.occurred_at
    if (output := payload.get("output")) is not None:
        fields["output"] = output
    if error := _error(payload):
        fields["error"] = error
    return (Patch(step, fields, not_before=fresh, unless=_SETTLED_STEPS),)


# ----------------------------------------------------------------- approval ---


def _approval(envelope: EventEnvelope) -> tuple[Mutation, ...]:
    """An approval moves the step it gates, and nothing else.

    The step has to be named: an approval's own subject ref identifies the
    approval, not the step, so `correlation.execution_step_ref` is required
    to fold one. The proposal's example trace omits it, which is a gap in the
    example rather than a reason to guess - an approval folded onto the wrong
    step is a gate opened on the wrong work.

    Granting an approval sets the step back to `ready`. It does **not**
    dispatch it, and could not: there is no mutation in this layer that
    writes a dispatch. The orchestrator notices a ready step and decides
    separately, which is what keeps the gate structural.
    """
    reference = split_step_ref(
        envelope.correlation.execution_step_ref or "",
        execution_ref=envelope.correlation.execution_ref,
    )
    if reference is None:
        return _NOTHING

    selector = StepRef(execution_ref=reference.execution_ref, step_key=reference.step_key)
    name = envelope.type.split(".", 1)[1]
    seen = {"last_event_at": envelope.occurred_at}
    fresh = ("last_event_at", envelope.occurred_at)

    if name == "requested":
        return (
            Patch(
                selector,
                {"status": StepStatus.WAITING_APPROVAL, **seen},
                not_before=fresh,
                unless=_SETTLED_STEPS,
            ),
        )
    if name == "granted":
        return (
            Patch(
                selector,
                {"status": StepStatus.READY, **seen},
                not_before=fresh,
                unless=_SETTLED_STEPS,
            ),
        )
    if name in ("denied", "expired"):
        return (
            Patch(
                selector,
                {
                    "status": StepStatus.FAILED,
                    "finished_at": envelope.occurred_at,
                    "error": {
                        "code": f"approval_{name}",
                        "message": _text(envelope.payload.get("reason"))
                        or f"the approval was {name}",
                    },
                    **seen,
                },
                not_before=fresh,
                unless=_SETTLED_STEPS,
            ),
        )
    return _NOTHING


# ------------------------------------------------------------------ helpers ---


def _text(value: Any) -> str | None:
    """A non-empty string, or None. Whitespace is not a reason."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _error(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The error shape, from whichever form a producer used.

    A string is accepted and wrapped rather than refused: `"error": "timed
    out"` is what a producer writes first, and rejecting it would push the
    detail into a message nobody reads.
    """
    error = payload.get("error")
    if isinstance(error, Mapping):
        return error
    if message := _text(error):
        return {"code": "unspecified", "message": message}
    return None


#: Namespace -> reducer. `log`, `metric` and `custom` are absent on purpose:
#: they are recorded and fold into nothing, and an entry returning `()` would
#: only invite someone to fill it in.
_REDUCERS: Final[Mapping[str, Reducer]] = {
    "agent": _agent,
    "task": _task,
    "run": _run,
    "tool": _tool,
    "workflow": _workflow,
    "approval": _approval,
}

__all__ = ["fold"]

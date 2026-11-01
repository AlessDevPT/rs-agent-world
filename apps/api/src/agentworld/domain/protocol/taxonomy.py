"""Which event types this release knows, and what each one is about.

The taxonomy is deliberately *not* a closed enum. The protocol's first
compatibility promise is that an unknown event type is accepted and stored, so
a type this release has never heard of is valid; it simply folds into nothing.
What this module provides is the set of types that do fold, and the subject
kinds each of them is allowed to carry - enough for ingest to catch a producer
sending `workflow.step_succeeded` about an agent, which is a bug in the
producer rather than a future version of the protocol.

One namespace is open by design: `custom.<anything>` is always valid, always
stored, and never folded. It is the escape hatch that keeps the closed part
closed.

**Nothing here describes creating or editing a definition.** There is no
`agent.created` and no `workflow.published`. Those are control-plane
mutations: they write an `audit_log` row and emit a realtime delta, and they
are not in the event log because replaying the log must never re-create a
definition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from agentworld.domain.protocol.types import SubjectKind

#: The one open namespace. A type under it is valid whatever follows the dot.
CUSTOM_NAMESPACE: Final = "custom"


@dataclass(frozen=True, slots=True)
class EventSpec:
    """What a known event type is about, and where it lands.

    `folds_into` is documentation rather than a dispatch mechanism - the
    reducers are keyed on the type itself - but it is the honest answer to
    "what does this event change?", and the table it produces is the one a
    reviewer reads before touching a reducer.
    """

    #: The subject kinds a correct producer may use. The first is canonical.
    subjects: tuple[SubjectKind, ...]
    #: The tables a reducer may change for this type. Empty means the event is
    #: recorded and folds into nothing.
    folds_into: tuple[str, ...] = ()


def _spec(subject: SubjectKind, *also: SubjectKind, folds_into: tuple[str, ...] = ()) -> EventSpec:
    return EventSpec(subjects=(subject, *also), folds_into=folds_into)


_AGENTS = ("agents",)
_AGENT_HISTORY = ("agents", "agent_status_history")
_TASKS = ("tasks",)
_RUNS = ("runs",)
_RUN_STEPS = ("runs", "run_steps")
_EXECUTIONS = ("workflow_executions",)
_STEPS = ("execution_steps",)
_EXECUTION_AND_STEPS = ("workflow_executions", "execution_steps")

#: Every event type this release folds. Unknown types are valid and absent.
KNOWN_EVENTS: Final[Mapping[str, EventSpec]] = {
    # ---- agent: what a worker says about itself -------------------------
    "agent.registered": _spec(SubjectKind.AGENT, folds_into=_AGENTS),
    "agent.heartbeat": _spec(SubjectKind.AGENT, folds_into=_AGENTS),
    "agent.status_changed": _spec(SubjectKind.AGENT, folds_into=_AGENT_HISTORY),
    "agent.capability_changed": _spec(SubjectKind.AGENT, folds_into=_AGENTS),
    "agent.paused": _spec(SubjectKind.AGENT, folds_into=_AGENT_HISTORY),
    "agent.resumed": _spec(SubjectKind.AGENT, folds_into=_AGENT_HISTORY),
    "agent.deregistered": _spec(SubjectKind.AGENT, folds_into=_AGENT_HISTORY),
    # ---- task: a unit of work's own life cycle --------------------------
    "task.created": _spec(SubjectKind.TASK, folds_into=_TASKS),
    "task.assigned": _spec(SubjectKind.TASK, folds_into=_TASKS),
    "task.started": _spec(SubjectKind.TASK, folds_into=_TASKS),
    "task.progress": _spec(SubjectKind.TASK, folds_into=_TASKS),
    "task.blocked": _spec(SubjectKind.TASK, folds_into=_TASKS),
    "task.unblocked": _spec(SubjectKind.TASK, folds_into=_TASKS),
    "task.completed": _spec(SubjectKind.TASK, folds_into=_TASKS),
    "task.failed": _spec(SubjectKind.TASK, folds_into=_TASKS),
    "task.cancelled": _spec(SubjectKind.TASK, folds_into=_TASKS),
    # ---- run: one pass of an agent's loop, and its trace ----------------
    "run.started": _spec(SubjectKind.RUN, folds_into=_RUNS),
    "run.step_started": _spec(SubjectKind.RUN, folds_into=_RUN_STEPS),
    "run.step_finished": _spec(SubjectKind.RUN, folds_into=_RUN_STEPS),
    "run.finished": _spec(SubjectKind.RUN, folds_into=_RUNS),
    "run.failed": _spec(SubjectKind.RUN, folds_into=_RUNS),
    # ---- tool: what an agent reached for --------------------------------
    # `tool.denied` is the one that earns its place: a tool call refused by a
    # grant or a policy is information the island should show, and silence
    # would make a blocked agent look like an idle one.
    "tool.invoked": _spec(SubjectKind.TOOL_CALL, SubjectKind.RUN, folds_into=_RUNS),
    "tool.returned": _spec(SubjectKind.TOOL_CALL, SubjectKind.RUN, folds_into=_RUNS),
    "tool.failed": _spec(SubjectKind.TOOL_CALL, SubjectKind.RUN, folds_into=_RUNS),
    "tool.denied": _spec(SubjectKind.TOOL_CALL, SubjectKind.RUN, folds_into=_RUNS),
    # ---- workflow: the orchestrator's record of its own decisions -------
    "workflow.execution_started": _spec(SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS),
    "workflow.step_ready": _spec(SubjectKind.EXECUTION_STEP, folds_into=_STEPS),
    "workflow.step_dispatched": _spec(SubjectKind.EXECUTION_STEP, folds_into=_STEPS),
    "workflow.step_started": _spec(SubjectKind.EXECUTION_STEP, folds_into=_STEPS),
    "workflow.step_succeeded": _spec(SubjectKind.EXECUTION_STEP, folds_into=_STEPS),
    "workflow.step_failed": _spec(SubjectKind.EXECUTION_STEP, folds_into=_STEPS),
    "workflow.step_skipped": _spec(SubjectKind.EXECUTION_STEP, folds_into=_STEPS),
    "workflow.step_retried": _spec(SubjectKind.EXECUTION_STEP, folds_into=_STEPS),
    "workflow.execution_completed": _spec(SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS),
    "workflow.execution_failed": _spec(SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS),
    "workflow.execution_cancelled": _spec(
        SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTION_AND_STEPS
    ),
    "workflow.execution_paused": _spec(SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS),
    "workflow.execution_resumed": _spec(SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS),
    # ---- approval: a real gate, not a notification ----------------------
    "approval.requested": _spec(SubjectKind.APPROVAL, folds_into=_STEPS),
    "approval.granted": _spec(SubjectKind.APPROVAL, folds_into=_STEPS),
    "approval.denied": _spec(SubjectKind.APPROVAL, folds_into=_STEPS),
    "approval.expired": _spec(SubjectKind.APPROVAL, folds_into=_STEPS),
    # ---- log and metric: recorded, never folded into state --------------
    "log.emitted": _spec(SubjectKind.AGENT, SubjectKind.RUN, SubjectKind.PROJECT),
    "metric.recorded": _spec(SubjectKind.AGENT, SubjectKind.PROJECT),
}

#: The namespaces the closed part of the taxonomy covers.
KNOWN_NAMESPACES: Final[frozenset[str]] = frozenset(
    event_type.split(".", 1)[0] for event_type in KNOWN_EVENTS
)


def spec_for(event_type: str) -> EventSpec | None:
    """The spec for `event_type`, or None if this release does not know it.

    None is not an error. It means the event is stored and folds into
    nothing, which is what the compatibility promise requires.
    """
    return KNOWN_EVENTS.get(event_type)


def is_custom(event_type: str) -> bool:
    return event_type.split(".", 1)[0] == CUSTOM_NAMESPACE


def folds(event_type: str) -> bool:
    """Whether folding this type can change any state at all."""
    spec = spec_for(event_type)
    return bool(spec and spec.folds_into)

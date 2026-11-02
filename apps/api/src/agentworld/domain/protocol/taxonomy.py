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
from enum import StrEnum
from typing import Final

from agentworld.domain.protocol.types import SubjectKind

#: The one open namespace. A type under it is valid whatever follows the dot.
CUSTOM_NAMESPACE: Final = "custom"


class Producer(StrEnum):
    """Who is allowed to originate an event of a given type.

    A closed vocabulary, in the same spirit as `Condition` in `policy.py`:
    four values a reviewer can hold in their head, rather than a predicate
    per type.

    This exists because it was missing. Until T0, `EventSpec` described what
    an event was *about* and which tables it folded into, and ingest checked
    the key's scope, its project pin, the clock skew and - only for events
    naming an agent - the agent allowlist. Nothing checked the type. So any
    holder of an ingest key could post `approval.granted`, which folds a step
    to `ready`, which `advance()` then dispatches. The approval gate is "the
    absence of a dispatch row" rather than a flag a worker checks, and that
    defends against a buggy approval UI - but not against a forged event,
    because the forged event makes the orchestrator create the dispatch
    itself. The same key could post `workflow.step_succeeded` with any
    `output`, which the next step reads through the expression language.

    A blanket ban on the namespace was not available: orchestration requires
    a `dispatchable` agent to report its own step outcome. Hence the second
    member, which is narrow rather than boolean.
    """

    #: Any ingest key for the pinned project, subject to the agent allowlist.
    AGENT = "agent"
    #: An ingest key, but only for a step that is *currently dispatched* to an
    #: agent that key may act as. Anything else - a step in another state, a
    #: step handed to a different agent, a step that does not exist - is
    #: refused.
    AGENT_FOR_DISPATCHED_STEP = "agent_for_dispatched_step"
    #: Written only by `services/executions.py`, in the transaction that also
    #: writes the state and the outbox row. Never ingestible.
    ORCHESTRATOR = "orchestrator"
    #: Written only by a role-gated control-plane service, with an
    #: `audit_log` row beside it. Never ingestible.
    CONTROL_PLANE = "control_plane"

    @property
    def is_ingestible(self) -> bool:
        """Whether an API key may post this at all."""
        return self in (Producer.AGENT, Producer.AGENT_FOR_DISPATCHED_STEP)


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
    #: Who may originate this type. Unlike `folds_into`, this one *is*
    #: enforced: `services/ingest.py` refuses a type the caller may not
    #: produce. The default is the permissive one, so a new type is only as
    #: privileged as a worker unless it says otherwise - and the totality
    #: test in `tests/unit/test_protocol_envelope.py` makes a folding type
    #: that silently took the default a failure rather than an oversight.
    producer: Producer = Producer.AGENT


def _spec(
    subject: SubjectKind,
    *also: SubjectKind,
    folds_into: tuple[str, ...] = (),
    producer: Producer = Producer.AGENT,
) -> EventSpec:
    return EventSpec(subjects=(subject, *also), folds_into=folds_into, producer=producer)


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
    #
    # `ORCHESTRATOR` for all but two. These are the record of a decision
    # `advance()` already made and wrote in one transaction with the state and
    # the outbox row; an event arriving from outside claiming the orchestrator
    # decided something is not late news, it is a forgery.
    #
    # The two exceptions are how a `dispatchable` agent reports the work it
    # was handed, which orchestration requires - so they are
    # `AGENT_FOR_DISPATCHED_STEP` rather than `AGENT`: allowed for the step
    # that agent is actually holding, and nothing else. Note that
    # `step_started` is *not* among them: nothing in the design needs an
    # external agent to announce starting, and the narrower rule is the one
    # worth having by default.
    "workflow.execution_started": _spec(
        SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.step_ready": _spec(
        SubjectKind.EXECUTION_STEP, folds_into=_STEPS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.step_dispatched": _spec(
        SubjectKind.EXECUTION_STEP, folds_into=_STEPS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.step_started": _spec(
        SubjectKind.EXECUTION_STEP, folds_into=_STEPS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.step_succeeded": _spec(
        SubjectKind.EXECUTION_STEP,
        folds_into=_STEPS,
        producer=Producer.AGENT_FOR_DISPATCHED_STEP,
    ),
    "workflow.step_failed": _spec(
        SubjectKind.EXECUTION_STEP,
        folds_into=_STEPS,
        producer=Producer.AGENT_FOR_DISPATCHED_STEP,
    ),
    "workflow.step_skipped": _spec(
        SubjectKind.EXECUTION_STEP, folds_into=_STEPS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.step_retried": _spec(
        SubjectKind.EXECUTION_STEP, folds_into=_STEPS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.execution_completed": _spec(
        SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.execution_failed": _spec(
        SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.execution_cancelled": _spec(
        SubjectKind.WORKFLOW_EXECUTION,
        folds_into=_EXECUTION_AND_STEPS,
        producer=Producer.ORCHESTRATOR,
    ),
    "workflow.execution_paused": _spec(
        SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS, producer=Producer.ORCHESTRATOR
    ),
    "workflow.execution_resumed": _spec(
        SubjectKind.WORKFLOW_EXECUTION, folds_into=_EXECUTIONS, producer=Producer.ORCHESTRATOR
    ),
    # ---- approval: a real gate, not a notification ----------------------
    #
    # `CONTROL_PLANE`, every one. An approval is an accountability record: who
    # decided, under which frozen requirement, with an `audit_log` row beside
    # the event. None of that can come from a key, and `approval.granted` in
    # particular is the single event that most directly causes external work.
    "approval.requested": _spec(
        SubjectKind.APPROVAL, folds_into=_STEPS, producer=Producer.CONTROL_PLANE
    ),
    "approval.granted": _spec(
        SubjectKind.APPROVAL, folds_into=_STEPS, producer=Producer.CONTROL_PLANE
    ),
    "approval.denied": _spec(
        SubjectKind.APPROVAL, folds_into=_STEPS, producer=Producer.CONTROL_PLANE
    ),
    "approval.expired": _spec(
        SubjectKind.APPROVAL, folds_into=_STEPS, producer=Producer.CONTROL_PLANE
    ),
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

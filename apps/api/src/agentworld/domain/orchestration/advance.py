"""What should happen next. No I/O, no provider, no framework, no clock.

```python
def advance(definition: WorkflowDefinition, state: ExecutionState) -> tuple[Decision, ...]
```

Called when an execution is created, when a dispatch reports an outcome, when
an approval is decided, when a wait expires, and when an operator resumes.
**Never on a timer** - a timer makes throughput depend on a polling interval
and hides a missed wake-up behind "it will catch up next tick".

Four properties, each of which is a test rather than a claim:

**Pure.** The import list is the proof: `datetime` for a type annotation,
this package, and `domain`. No session, no client, no settings. The
architecture contracts already forbid a framework or a provider SDK in
`domain/`; `test_advance_imports_nothing_it_should_not` asserts the narrower
thing, which is that this module reaches for nothing outside the domain at
all.

**Deterministic.** `now` is a field on the state, not a call. Nothing here
generates an id - `dispatches.id` and `idempotency_token` come from database
defaults. Iteration follows the author's step order, never a set's. So
replaying a recorded state sequence reproduces the decisions exactly, which
is how a production incident becomes a unit test.

**Idempotent.** A decision is emitted only for a step whose state does not
already reflect it, so calling it twice on the same state returns the same
tuple. The database's `UNIQUE (execution_step_id, kind, attempt)` makes a
concurrent double-apply a constraint violation rather than two side effects.

**Total.** Every state produces a decision list, including states that should
not exist. A definition that no longer validates, a step whose kind cannot
run, an agent that cannot be given work: each is a `FailExecution` with a
reason, never an exception. An orchestrator that raises on malformed input
leaves the execution exactly as stuck as the input was malformed.

## What this phase can execute

Deliberately narrow, because the alternative is a half-built approval gate.
Executable: `condition`, `transform`, `tool_call` whose tool is `read` or
`write`, and `agent_task` on a **managed** agent. Everything else is refused
with a reason, by one predicate - `_not_executable_yet` - which is the seam
Phase 3a and T8 reopen:

| Refused | Arrives with |
|---|---|
| a step that requires approval, or an `approval` step | approvals, Phase 3a |
| a `tool_call` whose tool is `consequential` | approvals, Phase 3a |
| `agent_task` on a `dispatchable` agent | dispatchable agents, Phase 3a |
| a `wait` step | a step configuration shape that can express a duration |

The first two are the execution-safety requirement made structural: **no
decision this function returns can carry
`side_effect_class = consequential`**, so no dispatch row for consequential
work can exist, so no worker can find one. A successful model response is
never authority to act, because the authority to act is a dispatch row and
`advance` will not write one.

`agent_task` on an **observed** agent is refused for a different and
permanent reason: an observed agent reports and is never given work. That is
not a phase gate, it is what the mode means.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum, auto
from typing import Any, Final

from agentworld.domain.enums import (
    AgentInteractionMode,
    DispatchKind,
    ExecutionStatus,
    SideEffect,
    StepKind,
    StepStatus,
)
from agentworld.domain.errors import ValidationFailedError
from agentworld.domain.expressions import MISSING, evaluate, evaluate_condition
from agentworld.domain.orchestration.decisions import (
    CompleteExecution,
    CompleteStep,
    Decision,
    DispatchStep,
    FailExecution,
    SkipStep,
)
from agentworld.domain.orchestration.definition import (
    EdgeDefinition,
    StepDefinition,
    WorkflowDefinition,
)
from agentworld.domain.orchestration.state import ExecutionState
from agentworld.domain.orchestration.validate import validate

#: A step the orchestrator is waiting on. Not terminal, not actionable.
_IN_FLIGHT: Final[frozenset[StepStatus]] = frozenset(
    {
        StepStatus.READY,
        StepStatus.DISPATCHED,
        StepStatus.RUNNING,
        StepStatus.WAITING_APPROVAL,
    }
)

#: Tool side effects a dispatch may carry in this phase. `CONSEQUENTIAL` is
#: absent, and its absence is the execution-safety guarantee.
_EXECUTABLE_EFFECTS: Final[frozenset[SideEffect]] = frozenset({SideEffect.READ, SideEffect.WRITE})


def advance(definition: WorkflowDefinition, state: ExecutionState) -> tuple[Decision, ...]:
    """Every decision the current state justifies, in author order."""
    # A graph that cannot run is never advanced. Checked here rather than
    # trusted from publish time, because a version that validated when it
    # was published can stop validating when an agent is deleted or a tool
    # grant is revoked - and the definition handed in carries those facts.
    report = validate(definition)
    if not report.publishable:
        codes = ", ".join(sorted({f.code.value for f in report.findings}))
        return (FailExecution(reason=f"The workflow is no longer runnable ({codes})"),)

    if not state.status.is_live or state.status is ExecutionStatus.PAUSED:
        # A settled execution has nothing to decide, and a paused one is
        # waiting on a person. Neither is an error and neither is progress.
        return ()

    scope = _scope(definition, state)
    decisions: list[Decision] = []

    for step in definition.steps:
        current = state.step(step.step_key)
        if current.status is not StepStatus.PENDING:
            continue

        readiness = _readiness(definition, state, step, scope)
        if readiness is _Readiness.WAITING:
            continue
        if readiness is _Readiness.SKIPPED:
            decisions.append(
                SkipStep(
                    step_key=step.step_key,
                    reason="the branch leading to this step was not taken",
                )
            )
            continue

        decided = _decide(definition, state, step, scope)
        if isinstance(decided, FailExecution):
            # The execution is over. Returning a dispatch alongside it would
            # be deciding to start work and to stop at the same time.
            return (decided,)
        decisions.append(decided)

    if decisions:
        return tuple(decisions)

    return _settle(definition, state)


# ------------------------------------------------------------------ readiness ---


class _Readiness(Enum):
    """Three outcomes. Private, and not a `StrEnum`.

    Internal to one pass of one function: exporting it would invite a caller
    to ask about readiness separately, which is a second code path for the
    same question. Not a `StrEnum` because nothing serialises it.
    """

    READY = auto()
    WAITING = auto()
    SKIPPED = auto()


def _readiness(
    definition: WorkflowDefinition,
    state: ExecutionState,
    step: StepDefinition,
    scope: Mapping[str, Any],
) -> _Readiness:
    """Whether `step` can be decided now, later, or never.

    An entry step - one named by an edge with no source, or one with no
    incoming edges at all - is ready immediately.

    Otherwise **every** incoming edge must be satisfied: its source
    succeeded, and its condition evaluated true. All rather than any,
    because a step with two incoming edges is a join, and running it when
    one arm is still working would hand it half its input. A branch is
    expressed by conditions on the edges out of a `condition` step, not by
    relaxing this rule.

    When every incoming edge has been decided and not all are satisfied, the
    step is skipped rather than left pending - otherwise an execution whose
    branch was not taken never settles.
    """
    incoming = [e for e in definition.edges if e.to_step == step.step_key]
    if not incoming or any(edge.from_step is None for edge in incoming):
        return _Readiness.READY

    for edge in incoming:
        assert edge.from_step is not None  # narrowed by the loop above
        source = state.step(edge.from_step)
        if not source.status.is_terminal:
            return _Readiness.WAITING

    satisfied = all(_edge_satisfied(state, edge, scope) for edge in incoming)
    return _Readiness.READY if satisfied else _Readiness.SKIPPED


def _edge_satisfied(state: ExecutionState, edge: EdgeDefinition, scope: Mapping[str, Any]) -> bool:
    assert edge.from_step is not None
    if state.step(edge.from_step).status is not StepStatus.SUCCEEDED:
        return False
    if edge.condition is None:
        return True
    return _condition_holds(edge.condition, scope)


def _condition_holds(expression: str, scope: Mapping[str, Any]) -> bool:
    """Evaluate an edge or step condition, treating a failure as false.

    `validate` has already refused a condition that does not parse, so this
    is the residual case: one that parses and fails to evaluate against this
    particular state - a reference into an output that is not there. False
    rather than an exception, because the alternative is one unreachable
    field stopping an execution that was otherwise fine, and false is what
    an absent reference means everywhere else in this language.
    """
    try:
        return evaluate_condition(expression, scope)
    except ValidationFailedError:
        return False


# ------------------------------------------------------------- the step kinds ---


def _decide(
    definition: WorkflowDefinition,
    state: ExecutionState,
    step: StepDefinition,
    scope: Mapping[str, Any],
) -> Decision:
    """What to do with one ready step."""
    if (refusal := _not_executable_yet(definition, step)) is not None:
        return FailExecution(reason=refusal, step_key=step.step_key)

    match step.kind:
        case StepKind.CONDITION:
            # Evaluating it is performing it. The branch is expressed by the
            # conditions on the edges out of here, which the next call reads.
            holds = step.condition is None or _condition_holds(step.condition, scope)
            return CompleteStep(step_key=step.step_key, output={"result": holds})

        case StepKind.TRANSFORM:
            return CompleteStep(step_key=step.step_key, output=_mapped(step.input_mapping, scope))

        case StepKind.TOOL_CALL:
            effect = definition.tool_effects.get(step.tool_name or "", SideEffect.READ)
            return DispatchStep(
                step_key=step.step_key,
                attempt=state.step(step.step_key).attempt,
                kind=DispatchKind.TOOL_CALL,
                side_effect_class=effect,
                instruction={
                    "tool_name": step.tool_name,
                    "input": _mapped(step.input_mapping, scope),
                },
            )

        case StepKind.AGENT_TASK:
            return DispatchStep(
                step_key=step.step_key,
                attempt=state.step(step.step_key).attempt,
                kind=DispatchKind.MANAGED_AGENT_TURN,
                # A managed agent turn is a model call. Its own tool calls
                # are classified and gated individually by the step runner;
                # the turn itself reaches nothing outside AgentWorld.
                side_effect_class=SideEffect.READ,
                instruction={
                    "agent_key": step.agent_key,
                    "input": _mapped(step.input_mapping, scope),
                },
            )

        case _:  # pragma: no cover - `_not_executable_yet` has refused these
            return FailExecution(
                reason=f"'{step.kind.value}' steps cannot be executed",
                step_key=step.step_key,
            )


def _not_executable_yet(definition: WorkflowDefinition, step: StepDefinition) -> str | None:
    """Why this step cannot run in this phase, or None if it can.

    One place, so that Phase 3a and T8 change one function rather than
    hunting for every branch that assumed approvals do not exist. The
    observed-agent refusal is in here too but is **not** a phase gate: it is
    permanent, and it is marked as such below.
    """
    if step.kind is StepKind.APPROVAL or step.requires_approval:
        return (
            "this step requires an approval, and approvals are not available yet; "
            "no work has been started"
        )

    if step.kind is StepKind.TOOL_CALL:
        effect = definition.tool_effects.get(step.tool_name or "", SideEffect.READ)
        if effect not in _EXECUTABLE_EFFECTS:
            # The execution-safety requirement. Until an approval can gate
            # it, consequential work is refused rather than performed - and
            # refused *before* any dispatch exists, so there is nothing for
            # a worker to find.
            return (
                f"'{step.tool_name}' is a consequential tool and cannot be run until "
                "approvals are available; no work has been started"
            )

    if step.kind is StepKind.AGENT_TASK:
        facts = definition.agents.get(step.agent_key or "")
        if facts is None:
            # `validate` reports this as `AGENT_MISSING`, so reaching here
            # means the definition changed between validating and deciding.
            return f"no agent is configured for '{step.step_key}'"
        if facts.interaction_mode is AgentInteractionMode.OBSERVED:
            # Permanent, not a phase gate. An observed agent reports its own
            # activity and is never given work; no dispatch kind can reach
            # one, and this is the line that makes that true at decision
            # time as well as at publish time.
            return (
                f"agent '{facts.agent_key}' only reports its own activity and cannot be given work"
            )
        if facts.interaction_mode is AgentInteractionMode.DISPATCHABLE:
            return (
                f"agent '{facts.agent_key}' is dispatchable, and pushing work to an "
                "external agent is not available yet"
            )

    if step.kind is StepKind.WAIT:
        return "'wait' steps have no configured duration in this release and cannot be executed"

    return None


# ------------------------------------------------------------------ settling ---


def _settle(definition: WorkflowDefinition, state: ExecutionState) -> tuple[Decision, ...]:
    """Nothing was ready. Decide whether the execution is over.

    A step still in flight means wait: the dispatch that is running will
    report an outcome and `advance` will be called again. Only when nothing
    is in flight and nothing is ready is the execution's own status this
    function's business.
    """
    states = [state.step(step.step_key) for step in definition.steps]
    if any(current.status in _IN_FLIGHT for current in states):
        return ()

    failed = [
        step.step_key
        for step in definition.steps
        if state.step(step.step_key).status is StepStatus.FAILED
    ]
    if failed:
        reasons = [state.step(key).failure_reason or "no reason recorded" for key in failed]
        return (
            FailExecution(reason=f"step '{failed[0]}' failed: {reasons[0]}", step_key=failed[0]),
        )

    if all(current.status.is_terminal for current in states) and states:
        return (CompleteExecution(output=_final_output(definition, state), decided_at=state.now),)

    # Nothing ready, nothing in flight, something not terminal. Reachable
    # when a step is `PENDING` behind a skipped branch that has not been
    # skipped yet - the next call, after those skips are applied, settles
    # it. Returning nothing is correct and is not a stuck execution: the
    # caller applied decisions in the previous round.
    return ()


def _final_output(definition: WorkflowDefinition, state: ExecutionState) -> Mapping[str, Any]:
    """The outputs of the leaf steps that succeeded.

    Leaves rather than every step: an execution's result is what came out of
    the end of it, and returning every intermediate output would make the
    result grow with the graph. Author order, so the mapping is stable.
    """
    has_outgoing = {edge.from_step for edge in definition.edges if edge.from_step is not None}
    return {
        step.step_key: output
        for step in definition.steps
        if step.step_key not in has_outgoing
        and (current := state.step(step.step_key)).status is StepStatus.SUCCEEDED
        and (output := current.output) is not None
    }


# --------------------------------------------------------------- expressions ---


def _scope(definition: WorkflowDefinition, state: ExecutionState) -> Mapping[str, Any]:
    """What `$.input.…` and `$.steps.….output.…` resolve against.

    Built once per call rather than per expression. Only succeeded steps
    contribute an output: a condition that can read a failed step's partial
    output is a condition whose meaning depends on how far the failure got.
    """
    steps: dict[str, Any] = {}
    for step in definition.steps:
        current = state.step(step.step_key)
        if current.status is StepStatus.SUCCEEDED and current.output is not None:
            steps[step.step_key] = {"output": dict(current.output)}
    return {"input": dict(state.input), "steps": steps}


def _mapped(mapping: Mapping[str, str], scope: Mapping[str, Any]) -> Mapping[str, Any]:
    """Evaluate each field of an input mapping.

    A field whose expression cannot be evaluated is omitted rather than set
    to null, for the same reason `_condition_holds` returns false: absent
    and null mean different things to whatever reads the input, and "the
    reference did not resolve" is absent.
    """
    result: dict[str, Any] = {}
    for field_name, expression in sorted(mapping.items()):
        try:
            value = evaluate(expression, scope)
        except ValidationFailedError:
            continue
        if value is not MISSING:
            result[field_name] = value
    return result


def dispatchable_effects() -> Sequence[SideEffect]:
    """The side effects a dispatch may carry in this release.

    Exported for the test that asserts `consequential` is not among them -
    the execution-safety requirement, checked against the constant the
    decision function actually uses rather than against a copy of it.
    """
    return tuple(sorted(_EXECUTABLE_EFFECTS))

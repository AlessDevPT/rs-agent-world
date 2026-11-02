"""`advance`: pure, deterministic, total, and unable to start consequential work.

No fixtures, no database, no network, no clock. Every test here is a mapping
from one state to one decision list, which is the whole reason the decision
function was separated from the thing that performs the decisions: the cases
that actually produce orchestration bugs - a branch not taken, a join with
one arm still running, a step whose agent was deleted after publish - are
cheap to enumerate and impossible to enumerate against a live worker.

Three of the four acceptance criteria are structural rather than behavioural,
and each has its own section: purity (what the module may import),
determinism (the same state twice, and the same state with a different
clock), and the two refusals that cannot be bypassed.
"""

from __future__ import annotations

import ast
import datetime as dt
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from agentworld.domain.enums import (
    AgentInteractionMode,
    DispatchKind,
    ExecutionStatus,
    SideEffect,
    StepKind,
    StepStatus,
)
from agentworld.domain.orchestration import (
    AgentFacts,
    CompleteExecution,
    CompleteStep,
    Decision,
    DispatchStep,
    EdgeDefinition,
    ExecutionState,
    FailExecution,
    StepDefinition,
    StepState,
    WorkflowDefinition,
    advance,
    dispatchable_effects,
    validate,
)

#: The *module*, not the function. `orchestration/__init__.py` re-exports
#: `advance`, so `from ... import advance as advance_module` binds the
#: function and every attribute lookup below fails with a message about
#: `'function' object has no attribute '__file__'`. `import_module` returns
#: what is in `sys.modules`, which is the module. The same shadowing caught
#: the reducers' `fold` in T0.
advance_module = import_module("agentworld.domain.orchestration.advance")

NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.UTC)
LATER = dt.datetime(2027, 6, 1, 3, 30, tzinfo=dt.UTC)

MANAGED = AgentFacts(
    agent_key="researcher",
    interaction_mode=AgentInteractionMode.MANAGED,
    display_name="Researcher",
    granted_tools=frozenset({"web_search", "send_email"}),
)
OBSERVED = AgentFacts(
    agent_key="watcher",
    interaction_mode=AgentInteractionMode.OBSERVED,
    display_name="Watcher",
)
DISPATCHABLE = AgentFacts(
    agent_key="pusher",
    interaction_mode=AgentInteractionMode.DISPATCHABLE,
    display_name="Pusher",
)

EFFECTS = {"web_search": SideEffect.READ, "send_email": SideEffect.CONSEQUENTIAL}


def _definition(
    *steps: StepDefinition,
    edges: tuple[EdgeDefinition, ...] = (),
    agents: dict[str, AgentFacts] | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        slug="flow",
        steps=steps,
        edges=edges,
        agents=agents if agents is not None else {MANAGED.agent_key: MANAGED},
        tool_effects=EFFECTS,
    )


def _state(
    status: ExecutionStatus = ExecutionStatus.RUNNING,
    steps: dict[str, StepState] | None = None,
    inputs: dict[str, Any] | None = None,
    now: dt.datetime = NOW,
) -> ExecutionState:
    return ExecutionState(status=status, now=now, input=inputs or {}, steps=steps or {})


def _agent_step(key: str = "work", **kwargs: Any) -> StepDefinition:
    return StepDefinition(
        step_key=key, kind=StepKind.AGENT_TASK, agent_key=MANAGED.agent_key, **kwargs
    )


def _done(output: dict[str, Any] | None = None) -> StepState:
    return StepState(status=StepStatus.SUCCEEDED, output=output or {})


# ------------------------------------------------------------------- purity ---


def test_advance_imports_nothing_it_should_not() -> None:
    """The acceptance criterion "no database and no network", as a property.

    Read from the module's own AST rather than from `sys.modules`: an import
    inside a function would not show up in a list of what the module pulled
    in at import time, and a function-level import is exactly how a pure
    function stops being one.

    The architecture contracts already forbid a framework or a provider SDK
    anywhere in `domain/`. This is narrower: this module may reach for the
    standard library and for `agentworld.domain`, and nothing else at all.
    """
    source = Path(advance_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    allowed_stdlib = {"__future__", "collections.abc", "enum", "typing"}
    for name in sorted(imported):
        assert name in allowed_stdlib or name.startswith("agentworld.domain"), (
            f"advance.py imports '{name}', which is neither the standard library "
            "nor the domain layer"
        )
    # Non-vacuity: if the walk found nothing, the loop above asserted nothing.
    assert len(imported) > 4


def test_nothing_in_the_module_reads_a_clock() -> None:
    """`now` is a field, not a call.

    A grep would be weaker: this looks for an attribute access named `now`
    or `time` anywhere in the module's syntax tree, so a `dt.datetime.now()`
    added in a branch nobody tests is still caught.
    """
    tree = ast.parse(Path(advance_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "now" not in calls
    assert "utcnow" not in calls
    assert "monotonic" not in calls


# -------------------------------------------------------------- determinism ---


def test_the_same_state_twice_gives_the_same_decisions() -> None:
    definition = _definition(_agent_step())
    state = _state()
    assert advance(definition, state) == advance(definition, state)


def test_a_different_clock_changes_nothing_but_the_timestamp() -> None:
    """`now` flows into `CompleteExecution.decided_at` and nowhere else.

    Two states identical except for a clock seven months apart. If anything
    inside branched on time, the decisions would differ.
    """
    definition = _definition(_agent_step())
    early = advance(definition, _state(now=NOW))
    late = advance(definition, _state(now=LATER))
    assert early == late

    finished = {"work": _done({"found": 1})}
    early_end = advance(definition, _state(steps=finished, now=NOW))
    late_end = advance(definition, _state(steps=finished, now=LATER))
    assert isinstance(early_end[0], CompleteExecution)
    assert isinstance(late_end[0], CompleteExecution)
    assert early_end[0].decided_at == NOW
    assert late_end[0].decided_at == LATER
    assert early_end[0].output == late_end[0].output


def test_a_recorded_sequence_replays_to_the_same_decisions() -> None:
    """How a production incident becomes a unit test.

    The sequence is the one a real execution goes through: nothing done,
    then the first step succeeded, then both. Replayed twice, decision for
    decision.
    """
    definition = _definition(
        _agent_step("first"),
        StepDefinition(step_key="second", kind=StepKind.TRANSFORM),
        edges=(EdgeDefinition(index=0, from_step="first", to_step="second"),),
    )
    sequence = [
        _state(),
        _state(steps={"first": _done({"n": 1})}),
        _state(steps={"first": _done({"n": 1}), "second": _done({"n": 1})}),
    ]
    first_pass = [advance(definition, state) for state in sequence]
    second_pass = [advance(definition, state) for state in sequence]
    assert first_pass == second_pass
    # And the sequence really does progress, or this proves nothing.
    assert [type(decisions[0]).__name__ for decisions in first_pass] == [
        "DispatchStep",
        "CompleteStep",
        "CompleteExecution",
    ]


def test_decisions_follow_the_authors_step_order() -> None:
    """Not a set's iteration order, which is stable per process and not
    across them."""
    definition = _definition(_agent_step("c"), _agent_step("a"), _agent_step("b"))
    decisions = advance(definition, _state())
    assert [d.step_key for d in decisions if isinstance(d, DispatchStep)] == ["c", "a", "b"]


# -------------------------------------------------- a graph that cannot run ---


@pytest.mark.parametrize(
    ("definition", "code"),
    [
        (
            _definition(
                _agent_step("a"),
                edges=(EdgeDefinition(index=0, from_step="nowhere", to_step="a"),),
            ),
            "dangling_edge",
        ),
        (
            _definition(
                _agent_step("a"),
                _agent_step("b"),
                edges=(
                    EdgeDefinition(index=0, from_step="a", to_step="b"),
                    EdgeDefinition(index=1, from_step="b", to_step="a"),
                ),
            ),
            "cycle",
        ),
        (
            _definition(StepDefinition(step_key="a", kind=StepKind.TOOL_CALL)),
            "tool_missing",
        ),
        (
            _definition(StepDefinition(step_key="a", kind=StepKind.AGENT_TASK)),
            "agent_missing",
        ),
    ],
)
def test_a_graph_validate_rejects_is_never_advanced(
    definition: WorkflowDefinition, code: str
) -> None:
    """The acceptance criterion. Checked at decision time, not trusted from
    publish time: a version that validated when published stops validating
    when an agent is deleted or a grant is revoked, and the definition handed
    in carries those facts.
    """
    assert not validate(definition).publishable, "the fixture must be invalid"
    decisions = advance(definition, _state())
    assert len(decisions) == 1
    assert isinstance(decisions[0], FailExecution)
    assert code in decisions[0].reason
    assert not any(isinstance(d, DispatchStep) for d in decisions)


def test_an_empty_workflow_starts_nothing_and_fails_nothing() -> None:
    """`NO_STEPS` is an error, so this is the validate gate again - but it is
    the one malformed input most likely to arrive from a draft."""
    decisions = advance(_definition(), _state())
    assert all(isinstance(d, FailExecution) for d in decisions)


# ------------------------------------------------------- the observed agent ---


@pytest.mark.parametrize(
    ("facts", "dispatches"),
    [(MANAGED, True), (OBSERVED, False), (DISPATCHABLE, False)],
    ids=lambda value: getattr(value, "agent_key", str(value)),
)
def test_only_a_managed_agent_can_be_given_work(facts: AgentFacts, dispatches: bool) -> None:
    definition = WorkflowDefinition(
        slug="flow",
        steps=(
            StepDefinition(step_key="work", kind=StepKind.AGENT_TASK, agent_key=facts.agent_key),
        ),
        agents={facts.agent_key: facts},
        tool_effects=EFFECTS,
    )
    decisions = advance(definition, _state())
    assert any(isinstance(d, DispatchStep) for d in decisions) is dispatches
    if not dispatches:
        assert all(isinstance(d, FailExecution) for d in decisions)


def test_no_dispatch_kind_can_reach_an_observed_agent() -> None:
    """The criterion as a statement about every step kind, not just one.

    An observed agent on any kind of step produces no dispatch at all - and
    for the kinds that could carry one, the refusal is explicit.
    """
    for kind in StepKind:
        definition = WorkflowDefinition(
            slug="flow",
            steps=(
                StepDefinition(
                    step_key="work",
                    kind=kind,
                    agent_key=OBSERVED.agent_key,
                    tool_name="web_search" if kind is StepKind.TOOL_CALL else None,
                ),
            ),
            agents={OBSERVED.agent_key: OBSERVED},
            tool_effects=EFFECTS,
        )
        decisions = advance(definition, _state())
        dispatched = [d for d in decisions if isinstance(d, DispatchStep)]
        assert dispatched == [], f"{kind.value} produced {dispatched}"


def test_the_decision_layer_refuses_an_observed_agent_on_its_own() -> None:
    """Defence in depth, tested on its own because it is currently redundant.

    `validate` reports `OBSERVED_AGENT_ASSIGNED` as an **error**, so the
    gate at the top of `advance` already refuses any definition that names
    one, and this second check can never fire through the public function
    today. It is kept because the severity of that finding is a product
    decision: `CONSEQUENTIAL_WITHOUT_APPROVAL` is a warning for reasons the
    validator's own docstring explains, and if the observed-agent finding
    were ever softened the same way, this is what would still refuse to
    hand an observed agent work.
    """
    step = StepDefinition(step_key="work", kind=StepKind.AGENT_TASK, agent_key=OBSERVED.agent_key)
    definition = WorkflowDefinition(
        slug="flow", steps=(step,), agents={OBSERVED.agent_key: OBSERVED}
    )
    refusal = advance_module._not_executable_yet(definition, step)
    assert refusal is not None
    assert "cannot be given work" in refusal


# ------------------------------------------------- consequential work, never ---


def test_a_dispatch_can_never_carry_consequential_work() -> None:
    """The execution-safety requirement, as a property of the constant the
    decision function actually uses."""
    assert SideEffect.CONSEQUENTIAL not in dispatchable_effects()


def test_a_consequential_tool_is_refused_before_any_dispatch_exists() -> None:
    """`validate` lets this publish - a consequential step with nothing
    gating it is a warning, because gating it is the approval policy's call.
    So this refusal is the only thing standing between a published workflow
    and an irreversible side effect, which is why it is at decision time.
    """
    definition = _definition(
        StepDefinition(step_key="post", kind=StepKind.TOOL_CALL, tool_name="send_email")
    )
    assert validate(definition).publishable, "the fixture must be publishable"

    decisions = advance(definition, _state())
    assert len(decisions) == 1
    assert isinstance(decisions[0], FailExecution)
    assert "consequential" in decisions[0].reason
    assert "no work has been started" in decisions[0].reason


def test_a_read_tool_still_dispatches() -> None:
    """The refusal is about the effect class, not about tools."""
    definition = _definition(
        StepDefinition(step_key="look", kind=StepKind.TOOL_CALL, tool_name="web_search")
    )
    decisions = advance(definition, _state())
    assert [type(d).__name__ for d in decisions] == ["DispatchStep"]
    assert isinstance(decisions[0], DispatchStep)
    assert decisions[0].kind is DispatchKind.TOOL_CALL
    assert decisions[0].side_effect_class is SideEffect.READ


def test_a_step_requiring_approval_is_refused_with_nothing_started() -> None:
    definition = _definition(_agent_step(requires_approval=True))
    decisions = advance(definition, _state())
    assert isinstance(decisions[0], FailExecution)
    assert "approvals are not available yet" in decisions[0].reason


@pytest.mark.parametrize("kind", [StepKind.APPROVAL, StepKind.WAIT])
def test_the_kinds_this_release_cannot_run_are_refused(kind: StepKind) -> None:
    definition = _definition(StepDefinition(step_key="x", kind=kind))
    decisions = advance(definition, _state())
    assert isinstance(decisions[0], FailExecution)
    assert not any(isinstance(d, DispatchStep) for d in decisions)


def test_every_dispatch_this_release_emits_is_a_safe_effect() -> None:
    """Exhaustive over step kinds: whatever comes out, nothing is
    consequential."""
    for kind in StepKind:
        definition = _definition(
            StepDefinition(
                step_key="x",
                kind=kind,
                agent_key=MANAGED.agent_key if kind is StepKind.AGENT_TASK else None,
                tool_name="web_search" if kind is StepKind.TOOL_CALL else None,
            )
        )
        for decision in advance(definition, _state()):
            if isinstance(decision, DispatchStep):
                assert decision.side_effect_class in dispatchable_effects()


# ------------------------------------------------------------- the graph walk ---


def test_an_entry_step_is_ready_at_once() -> None:
    definition = _definition(_agent_step())
    decisions = advance(definition, _state(status=ExecutionStatus.PENDING))
    assert isinstance(decisions[0], DispatchStep)
    assert decisions[0].attempt == 0


def test_a_later_step_waits_for_the_one_before_it() -> None:
    definition = _definition(
        _agent_step("first"),
        _agent_step("second"),
        edges=(EdgeDefinition(index=0, from_step="first", to_step="second"),),
    )
    in_flight = _state(steps={"first": StepState(status=StepStatus.RUNNING)})
    assert advance(definition, in_flight) == ()


def test_a_join_waits_for_every_arm() -> None:
    """All incoming edges, not any: running a join with one arm still
    working would hand it half its input."""
    definition = _definition(
        _agent_step("left"),
        _agent_step("right"),
        StepDefinition(step_key="join", kind=StepKind.TRANSFORM),
        edges=(
            EdgeDefinition(index=0, from_step="left", to_step="join"),
            EdgeDefinition(index=1, from_step="right", to_step="join"),
        ),
    )
    half = _state(steps={"left": _done(), "right": StepState(status=StepStatus.DISPATCHED)})
    assert advance(definition, half) == ()

    both = _state(steps={"left": _done(), "right": _done()})
    decisions = advance(definition, both)
    assert [type(d).__name__ for d in decisions] == ["CompleteStep"]


def test_a_join_whose_arm_was_skipped_does_not_run_on_half_its_input() -> None:
    """All incoming edges, not any - and this is the case that distinguishes
    them.

    The test above only proves the join waits while an arm is still working,
    which "any" also does: an unfinished arm is not terminal, so readiness
    returns WAITING either way. The distinction only appears once every arm
    has *finished* and not all of them succeeded. With "any" the join runs
    with one input missing; with "all" it is skipped. The non-vacuity pass
    found this hole - weakening `all` to `any` failed no test.
    """
    definition = _definition(
        _agent_step("left"),
        _agent_step("right"),
        StepDefinition(step_key="join", kind=StepKind.TRANSFORM),
        edges=(
            EdgeDefinition(index=0, from_step="left", to_step="join"),
            EdgeDefinition(index=1, from_step="right", to_step="join"),
        ),
    )
    state = _state(steps={"left": _done({"n": 1}), "right": StepState(status=StepStatus.SKIPPED)})
    decisions = advance(definition, state)
    assert [type(d).__name__ for d in decisions] == ["SkipStep"]
    assert decisions[0].step_key == "join"


def test_a_failure_waits_for_work_that_is_still_in_flight() -> None:
    """Failing the execution now would mean a running dispatch reporting its
    outcome into an execution that is already over.

    The other case the non-vacuity pass found: removing the in-flight check
    failed no test, because with nothing else wrong the "all terminal" check
    gives the same answer. It only differs when something has failed *and*
    something is still running.
    """
    definition = _definition(_agent_step("one"), _agent_step("two"))
    state = _state(
        steps={
            "one": StepState(status=StepStatus.FAILED, failure_reason="boom"),
            "two": StepState(status=StepStatus.RUNNING),
        }
    )
    assert advance(definition, state) == ()

    # Once the second one lands, the failure is reported.
    settled = _state(
        steps={
            "one": StepState(status=StepStatus.FAILED, failure_reason="boom"),
            "two": _done(),
        }
    )
    decisions = advance(definition, settled)
    assert isinstance(decisions[0], FailExecution)
    assert decisions[0].step_key == "one"


def test_an_unparseable_condition_is_false_at_the_decision_layer() -> None:
    """Tested directly, because it is unreachable through `advance`.

    `validate._expressions` parses every step condition, every mapping
    expression and every edge condition, and an unparseable one is an
    error - so the gate at the top of `advance` refuses the definition
    before any expression is evaluated. The `try` in `_condition_holds` is
    therefore defence in depth against a validator that stops covering
    something, and this is what keeps it honest rather than decorative.
    """
    assert advance_module._condition_holds("$.input.ok", {"input": {"ok": True}}) is True
    assert advance_module._condition_holds("not a valid ( expression", {}) is False


def test_a_branch_not_taken_is_skipped_rather_than_left_pending() -> None:
    """Otherwise the execution never settles."""
    definition = _definition(
        StepDefinition(step_key="check", kind=StepKind.CONDITION, condition="$.input.go"),
        _agent_step("yes"),
        _agent_step("no"),
        edges=(
            EdgeDefinition(index=0, from_step="check", to_step="yes", condition="$.input.go"),
            EdgeDefinition(index=1, from_step="check", to_step="no", condition="not $.input.go"),
        ),
    )
    state = _state(steps={"check": _done({"result": True})}, inputs={"go": True})
    decisions = advance(definition, state)
    by_key = {d.step_key: type(d).__name__ for d in decisions}
    assert by_key == {"yes": "DispatchStep", "no": "SkipStep"}


def test_a_condition_step_is_performed_rather_than_dispatched() -> None:
    """Evaluating it is performing it: nothing to dispatch, nothing that
    could fail halfway."""
    definition = _definition(
        StepDefinition(step_key="check", kind=StepKind.CONDITION, condition="$.input.count > 2")
    )
    decisions = advance(definition, _state(inputs={"count": 5}))
    assert decisions == (CompleteStep(step_key="check", output={"result": True}),)

    decisions = advance(definition, _state(inputs={"count": 1}))
    assert decisions == (CompleteStep(step_key="check", output={"result": False}),)


def test_a_transform_maps_prior_output_into_its_own() -> None:
    definition = _definition(
        _agent_step("research"),
        StepDefinition(
            step_key="shape",
            kind=StepKind.TRANSFORM,
            input_mapping={"name": "$.steps.research.output.title", "where": "$.input.city"},
        ),
        edges=(EdgeDefinition(index=0, from_step="research", to_step="shape"),),
    )
    state = _state(steps={"research": _done({"title": "Findings"})}, inputs={"city": "Lisbon"})
    decisions = advance(definition, state)
    assert decisions == (
        CompleteStep(step_key="shape", output={"name": "Findings", "where": "Lisbon"}),
    )


def test_a_mapping_field_that_does_not_resolve_is_omitted_not_nulled() -> None:
    """Absent and null mean different things to whatever reads the input."""
    definition = _definition(
        StepDefinition(
            step_key="shape",
            kind=StepKind.TRANSFORM,
            input_mapping={"here": "$.input.city", "gone": "$.input.nothing"},
        )
    )
    decisions = advance(definition, _state(inputs={"city": "Lisbon"}))
    assert isinstance(decisions[0], CompleteStep)
    assert decisions[0].output == {"here": "Lisbon"}


# ---------------------------------------------------------------- settling ---


def test_an_execution_completes_with_the_output_of_its_leaves() -> None:
    definition = _definition(
        _agent_step("first"),
        _agent_step("last"),
        edges=(EdgeDefinition(index=0, from_step="first", to_step="last"),),
    )
    state = _state(steps={"first": _done({"n": 1}), "last": _done({"n": 2})})
    decisions = advance(definition, state)
    assert len(decisions) == 1
    assert isinstance(decisions[0], CompleteExecution)
    # The leaf only: an execution's result is what came out of the end of it.
    assert decisions[0].output == {"last": {"n": 2}}


def test_a_failed_step_fails_the_execution_with_its_own_reason() -> None:
    """Quoted from the state rather than invented, so the message is
    something that actually happened."""
    definition = _definition(_agent_step())
    state = _state(
        steps={"work": StepState(status=StepStatus.FAILED, failure_reason="the tool timed out")}
    )
    decisions = advance(definition, state)
    assert decisions == (
        FailExecution(reason="step 'work' failed: the tool timed out", step_key="work"),
    )


def test_a_skipped_step_does_not_prevent_completion() -> None:
    definition = _definition(
        StepDefinition(step_key="check", kind=StepKind.CONDITION, condition="$.input.go"),
        _agent_step("yes"),
        _agent_step("no"),
        edges=(
            EdgeDefinition(index=0, from_step="check", to_step="yes", condition="$.input.go"),
            EdgeDefinition(index=1, from_step="check", to_step="no", condition="not $.input.go"),
        ),
    )
    state = _state(
        steps={
            "check": _done({"result": True}),
            "yes": _done({"done": True}),
            "no": StepState(status=StepStatus.SKIPPED),
        },
        inputs={"go": True},
    )
    decisions = advance(definition, state)
    assert isinstance(decisions[0], CompleteExecution)
    assert decisions[0].output == {"yes": {"done": True}}


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.PAUSED,
    ],
)
def test_a_settled_or_paused_execution_decides_nothing(status: ExecutionStatus) -> None:
    """A paused execution is waiting on a person; a settled one is over.
    Neither is an error and neither is progress."""
    definition = _definition(_agent_step())
    assert advance(definition, _state(status=status)) == ()


def test_a_step_in_flight_means_wait() -> None:
    for status in (
        StepStatus.READY,
        StepStatus.DISPATCHED,
        StepStatus.RUNNING,
        StepStatus.WAITING_APPROVAL,
    ):
        definition = _definition(_agent_step())
        state = _state(steps={"work": StepState(status=status)})
        assert advance(definition, state) == (), f"{status.value} should wait"


# ---------------------------------------------------------------- totality ---


def test_the_very_first_call_sees_no_step_rows_at_all() -> None:
    """An execution row is created before its step rows exist, and `advance`
    is called immediately after."""
    definition = _definition(_agent_step())
    assert advance(definition, ExecutionState(status=ExecutionStatus.PENDING, now=NOW))


def test_a_state_naming_a_step_the_definition_does_not_have_is_ignored() -> None:
    """A workflow version was edited, or a state was assembled from the
    wrong version. Neither should raise."""
    definition = _definition(_agent_step())
    state = _state(steps={"work": _done(), "ghost": _done()})
    decisions = advance(definition, state)
    assert isinstance(decisions[0], CompleteExecution)


def test_a_condition_that_cannot_be_evaluated_is_false_not_an_exception() -> None:
    """One unreachable field must not stop an execution that was otherwise
    fine, and false is what an absent reference means everywhere else in
    this language."""
    definition = _definition(
        StepDefinition(
            step_key="check", kind=StepKind.CONDITION, condition="$.steps.absent.output.x"
        )
    )
    decisions = advance(definition, _state())
    assert decisions == (CompleteStep(step_key="check", output={"result": False}),)


def test_every_decision_type_is_reachable() -> None:
    """Guard on the union: a member nothing emits is a member nothing tests.

    `RequestApproval`, `RetryStep` and `PauseExecution` are deliberately
    absent from `Decision` for exactly this reason - see `decisions.py`.
    """
    emitted: set[str] = set()
    cases: list[tuple[WorkflowDefinition, ExecutionState]] = [
        (_definition(_agent_step()), _state()),
        (
            _definition(StepDefinition(step_key="t", kind=StepKind.TRANSFORM)),
            _state(),
        ),
        (
            _definition(
                StepDefinition(step_key="c", kind=StepKind.CONDITION, condition="false"),
                _agent_step("y"),
                edges=(EdgeDefinition(index=0, from_step="c", to_step="y", condition="false"),),
            ),
            _state(steps={"c": _done({"result": False})}),
        ),
        (_definition(_agent_step()), _state(steps={"work": _done()})),
        (
            _definition(_agent_step()),
            _state(steps={"work": StepState(status=StepStatus.FAILED)}),
        ),
    ]
    for definition, state in cases:
        emitted.update(type(d).__name__ for d in advance(definition, state))

    assert emitted == {
        "DispatchStep",
        "CompleteStep",
        "SkipStep",
        "CompleteExecution",
        "FailExecution",
    }


def test_the_union_has_no_member_without_a_test() -> None:
    """The other half of the guard above, read off the type itself."""
    members = {member.__name__ for member in Decision.__value__.__args__}
    assert members == {
        "DispatchStep",
        "CompleteStep",
        "SkipStep",
        "FailExecution",
        "CompleteExecution",
    }

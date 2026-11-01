"""Workflow validation: one test per finding, and one that there are no others.

The roadmap asks for every documented finding code to have a test. The last
test in this file is the one that keeps that true as codes are added - a new
member of `Code` with no test fails it.

What is being checked throughout is a single claim: a graph that looks
connected on the island and cannot run says so, in words a beginner can act
on, with a pointer the UI can highlight.
"""

from __future__ import annotations

import pytest

from agentworld.domain.enums import AgentInteractionMode, SideEffect, StepKind
from agentworld.domain.orchestration import (
    AgentFacts,
    Code,
    EdgeDefinition,
    Severity,
    StepDefinition,
    WorkflowDefinition,
    validate,
)

MANAGED = AgentFacts(
    agent_key="researcher",
    interaction_mode=AgentInteractionMode.MANAGED,
    display_name="Researcher",
    granted_tools=frozenset({"web_search", "send_slack_message"}),
)
OBSERVED = AgentFacts(
    agent_key="crawler",
    interaction_mode=AgentInteractionMode.OBSERVED,
    display_name="Legacy crawler",
)
UNGRANTED = AgentFacts(
    agent_key="writer",
    interaction_mode=AgentInteractionMode.MANAGED,
    display_name="Writer",
    granted_tools=frozenset({"web_search"}),
)

AGENTS = {agent.agent_key: agent for agent in (MANAGED, OBSERVED, UNGRANTED)}

EFFECTS = {
    "web_search": SideEffect.READ,
    "send_slack_message": SideEffect.CONSEQUENTIAL,
}


def step(key: str, **overrides: object) -> StepDefinition:
    base: dict[str, object] = {
        "step_key": key,
        "kind": StepKind.AGENT_TASK,
        "agent_key": "researcher",
    }
    base.update(overrides)
    return StepDefinition(**base)  # type: ignore[arg-type]


def workflow(
    *, steps: tuple[StepDefinition, ...], edges: tuple[EdgeDefinition, ...] = (), approvers: int = 2
) -> WorkflowDefinition:
    return WorkflowDefinition(
        slug="competitor-brief",
        steps=steps,
        edges=edges,
        agents=AGENTS,
        tool_effects=EFFECTS,
        eligible_approvers=approvers,
    )


def codes_of(definition: WorkflowDefinition) -> frozenset[Code]:
    return validate(definition).codes()


# ------------------------------------------------------------- the happy path ---


def test_a_runnable_workflow_is_publishable_with_no_findings() -> None:
    """The baseline. Without it, every test below could pass vacuously."""
    report = validate(
        workflow(
            steps=(step("research"), step("write")),
            edges=(EdgeDefinition(index=0, from_step="research", to_step="write"),),
        )
    )
    assert report.findings == ()
    assert report.publishable


def test_a_warning_does_not_block_publishing() -> None:
    """Whether to gate a consequential step is the approval policy's call,
    and refusing to publish would be validation overruling it."""
    report = validate(workflow(steps=(step("post", tool_name="send_slack_message"),)))
    assert Code.CONSEQUENTIAL_WITHOUT_APPROVAL in report.codes()
    assert report.publishable


# --------------------------------------------------- one test per finding code ---


def test_no_steps() -> None:
    assert Code.NO_STEPS in codes_of(workflow(steps=()))


def test_duplicate_step_key() -> None:
    """Step names are how edges and conditions refer to them."""
    assert Code.DUPLICATE_STEP_KEY in codes_of(workflow(steps=(step("research"), step("research"))))


def test_dangling_edge() -> None:
    assert Code.DANGLING_EDGE in codes_of(
        workflow(
            steps=(step("research"),),
            edges=(EdgeDefinition(index=0, from_step="research", to_step="ghost"),),
        )
    )


def test_no_entry_step() -> None:
    """Two steps each waiting for the other: nothing can start."""
    assert Code.NO_ENTRY_STEP in codes_of(
        workflow(
            steps=(step("a"), step("b")),
            edges=(
                EdgeDefinition(index=0, from_step="a", to_step="b"),
                EdgeDefinition(index=1, from_step="b", to_step="a"),
            ),
        )
    )


def test_cycle() -> None:
    report = validate(
        workflow(
            steps=(step("a"), step("b"), step("c")),
            edges=(
                EdgeDefinition(index=0, from_step="a", to_step="b"),
                EdgeDefinition(index=1, from_step="b", to_step="c"),
                EdgeDefinition(index=2, from_step="c", to_step="b"),
            ),
        )
    )
    assert Code.CYCLE in report.codes()
    # The pointer names an edge, because an edge is what the user deletes.
    cycle_findings = [f for f in report.findings if f.code is Code.CYCLE]
    assert all(f.pointer.startswith("/edges/") for f in cycle_findings)


def test_unreachable_step() -> None:
    """A step dragged out and never connected - the commonest real mistake."""
    report = validate(
        workflow(
            steps=(step("research"), step("write"), step("cleanup")),
            edges=(EdgeDefinition(index=0, from_step="research", to_step="write"),),
        )
    )
    finding = next(f for f in report.findings if f.code is Code.UNREACHABLE_STEP)
    assert finding.pointer == "/steps/cleanup"
    assert not report.publishable


def test_a_step_fed_only_by_a_cycle_is_also_unreachable() -> None:
    """The rarer shape: it has an incoming edge, but nothing that runs."""
    report = validate(
        workflow(
            steps=(step("start"), step("a"), step("b"), step("after")),
            edges=(
                EdgeDefinition(index=0, from_step="a", to_step="b"),
                EdgeDefinition(index=1, from_step="b", to_step="a"),
                EdgeDefinition(index=2, from_step="b", to_step="after"),
            ),
        )
    )
    assert Code.UNREACHABLE_STEP in report.codes()


def test_multiple_entry_steps() -> None:
    """Legal, and far more often an accident than a plan - so it is said
    out loud without blocking the publish."""
    report = validate(
        workflow(
            steps=(step("a"), step("b"), step("joined")),
            edges=(
                EdgeDefinition(index=0, from_step="a", to_step="joined"),
                EdgeDefinition(index=1, from_step="b", to_step="joined"),
            ),
        )
    )
    finding = next(f for f in report.findings if f.code is Code.MULTIPLE_ENTRY_STEPS)
    assert finding.severity is Severity.WARNING
    assert report.publishable


def test_observed_agent_assigned() -> None:
    """The honesty check: a line drawn to an agent that only reports.

    On the island this looks exactly like a line to one that can be
    instructed, which is the whole reason the finding exists.
    """
    report = validate(workflow(steps=(step("research", agent_key="crawler"),)))
    finding = next(f for f in report.findings if f.code is Code.OBSERVED_AGENT_ASSIGNED)
    assert finding.severity is Severity.ERROR
    assert "Legacy crawler" in finding.detail
    assert "cannot be given work" in finding.detail
    assert finding.remedy


def test_unknown_agent() -> None:
    assert Code.UNKNOWN_AGENT in codes_of(workflow(steps=(step("r", agent_key="nobody"),)))


def test_agent_missing() -> None:
    assert Code.AGENT_MISSING in codes_of(workflow(steps=(step("r", agent_key=None),)))


def test_tool_missing() -> None:
    assert Code.TOOL_MISSING in codes_of(
        workflow(steps=(step("call", kind=StepKind.TOOL_CALL, agent_key=None),))
    )


def test_tool_not_granted() -> None:
    """A tool the agent's connections do not grant."""
    report = validate(
        workflow(steps=(step("post", agent_key="writer", tool_name="send_slack_message"),))
    )
    finding = next(f for f in report.findings if f.code is Code.TOOL_NOT_GRANTED)
    assert "Writer" in finding.detail
    assert "send_slack_message" in finding.detail
    assert finding.pointer == "/steps/post/tool_id"


def test_consequential_without_approval() -> None:
    report = validate(workflow(steps=(step("post", tool_name="send_slack_message"),)))
    finding = next(f for f in report.findings if f.code is Code.CONSEQUENTIAL_WITHOUT_APPROVAL)
    assert finding.severity is Severity.WARNING


def test_a_gated_consequential_step_warns_about_nothing() -> None:
    assert Code.CONSEQUENTIAL_WITHOUT_APPROVAL not in codes_of(
        workflow(steps=(step("post", tool_name="send_slack_message", requires_approval=True),))
    )


def test_approval_unsatisfiable() -> None:
    """Four eyes in a workspace with one pair is a deadlock built by
    configuration, and publishing is the cheap moment to notice."""
    report = validate(
        workflow(
            steps=(step("post", approval_requires_different_user=True),),
            approvers=1,
        )
    )
    finding = next(f for f in report.findings if f.code is Code.APPROVAL_UNSATISFIABLE)
    assert "only one member who can approve" in finding.detail
    assert "Invite another member" in finding.remedy


def test_four_eyes_is_fine_with_two_approvers() -> None:
    assert Code.APPROVAL_UNSATISFIABLE not in codes_of(
        workflow(steps=(step("post", approval_requires_different_user=True),), approvers=2)
    )


def test_invalid_expression() -> None:
    """A condition that cannot parse fails validation rather than stranding
    a running execution at three in the morning."""
    report = validate(workflow(steps=(step("research", condition="__import__('os')"),)))
    finding = next(f for f in report.findings if f.code is Code.INVALID_EXPRESSION)
    assert finding.pointer == "/steps/research/condition"


def test_an_invalid_expression_in_an_input_mapping_is_found_too() -> None:
    report = validate(workflow(steps=(step("write", input_mapping={"brief": "open('x')"}),)))
    assert any(f.pointer == "/steps/write/input_mapping/brief" for f in report.findings)


def test_an_invalid_expression_on_an_edge_is_found_too() -> None:
    report = validate(
        workflow(
            steps=(step("a"), step("b")),
            edges=(EdgeDefinition(index=0, from_step="a", to_step="b", condition="lambda: 1"),),
        )
    )
    assert any(f.pointer == "/edges/0/condition" for f in report.findings)


def test_a_valid_expression_produces_no_finding() -> None:
    assert Code.INVALID_EXPRESSION not in codes_of(
        workflow(steps=(step("r", condition="$.input.limit > 3"),))
    )


# ------------------------------------------------------------- totality ---


def test_validation_is_total_over_a_badly_broken_definition() -> None:
    """Several problems at once must produce findings, never an exception.

    This is the shape the conversational builder will hand it while a user
    is still describing what they want.
    """
    report = validate(
        workflow(
            steps=(
                step("a", agent_key="crawler", condition="!!!"),
                step("a", agent_key=None),
                step("orphan", tool_name="send_slack_message", agent_key="writer"),
            ),
            edges=(
                EdgeDefinition(index=0, from_step="ghost", to_step="a"),
                EdgeDefinition(index=1, from_step="a", to_step="a"),
            ),
            approvers=1,
        )
    )
    assert len(report.findings) >= 5
    assert not report.publishable


def test_every_finding_carries_a_pointer_and_a_detail() -> None:
    """The UI highlights the pointer and shows the detail; a finding missing
    either is one a user cannot act on."""
    report = validate(
        workflow(
            steps=(step("a", agent_key="crawler"), step("a")),
            edges=(EdgeDefinition(index=0, from_step="a", to_step="ghost"),),
        )
    )
    assert report.findings
    for finding in report.findings:
        assert finding.pointer.startswith("/")
        assert finding.detail.endswith(".")


@pytest.mark.parametrize("code", list(Code))
def test_every_finding_code_has_a_test(code: Code) -> None:
    """The test that keeps the rest honest.

    A new member of `Code` with no test above fails here, which is the only
    way "every documented finding has a test" stays true rather than being
    true on the day it was written.
    """
    import pathlib

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    assert f"Code.{code.name}" in source, f"{code.value} has no test"

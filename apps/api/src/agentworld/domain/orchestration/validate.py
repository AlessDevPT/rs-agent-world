"""Can this graph actually run?

`POST /v1/workflows/{id}/validate` is the endpoint the product leans on, and
it returns findings rather than a boolean because "no" is useless to the
person who has to fix it. Every finding carries a `pointer` the UI
highlights, a `detail` written for a beginner, and a `remedy` that says what
to do.

This is where **"do not treat a visual line as proof two components can
communicate"** stops being a principle and becomes code. A graph can look
perfectly connected on the island and still be unrunnable: the agent it
points at only reports and cannot be given work; the tool it calls was never
granted; a branch can never be reached; an approval can never be satisfied.
Each of those is a line a user drew that the backend will not honour, and
each one says so here rather than at three in the morning.

Pure, and total over malformed input: a definition with a dangling edge or a
condition that does not parse produces findings, never an exception. The
conversational builder calls this before proposing anything, which is what
stops it producing an unrunnable ecosystem and claiming success.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from agentworld.domain.enums import AgentInteractionMode, SideEffect, StepKind
from agentworld.domain.errors import ValidationFailedError
from agentworld.domain.expressions import parse
from agentworld.domain.orchestration.definition import (
    StepDefinition,
    WorkflowDefinition,
)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class Code(StrEnum):
    """Every finding this release can produce.

    A closed set, because the UI maps each to a highlight and the builder
    maps each to a repair it knows how to offer. A free-text code would make
    both of those "best effort".
    """

    NO_STEPS = "no_steps"
    DUPLICATE_STEP_KEY = "duplicate_step_key"
    DANGLING_EDGE = "dangling_edge"
    NO_ENTRY_STEP = "no_entry_step"
    CYCLE = "cycle"
    UNREACHABLE_STEP = "unreachable_step"
    MULTIPLE_ENTRY_STEPS = "multiple_entry_steps"
    OBSERVED_AGENT_ASSIGNED = "observed_agent_assigned"
    UNKNOWN_AGENT = "unknown_agent"
    AGENT_MISSING = "agent_missing"
    TOOL_MISSING = "tool_missing"
    TOOL_NOT_GRANTED = "tool_not_granted"
    CONSEQUENTIAL_WITHOUT_APPROVAL = "consequential_without_approval"
    APPROVAL_UNSATISFIABLE = "approval_unsatisfiable"
    INVALID_EXPRESSION = "invalid_expression"


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    code: Code
    pointer: str
    detail: str
    remedy: str = ""


@dataclass(frozen=True, slots=True)
class ValidationReport:
    findings: tuple[Finding, ...]

    @property
    def publishable(self) -> bool:
        """Warnings do not block. Errors do.

        The distinction is whether the workflow *can* run, not whether it
        *should*: a consequential step with nothing gating it is a decision
        someone may legitimately have made, and refusing to publish it would
        be this function overruling the approval policy that governs it.
        """
        return not any(f.severity is Severity.ERROR for f in self.findings)

    def codes(self) -> frozenset[Code]:
        return frozenset(finding.code for finding in self.findings)


#: Steps that hand work to something. Only these can name an agent, and only
#: these are affected by what that agent is able to do.
_ASSIGNABLE: Final = frozenset({StepKind.AGENT_TASK})


def validate(definition: WorkflowDefinition) -> ValidationReport:
    """Every reason this workflow could not run, in one pass."""
    findings: list[Finding] = []
    findings.extend(_structure(definition))
    findings.extend(_graph(definition))
    findings.extend(_assignments(definition))
    findings.extend(_approvals(definition))
    findings.extend(_expressions(definition))
    return ValidationReport(tuple(findings))


# ------------------------------------------------------------- the structure ---


def _structure(definition: WorkflowDefinition) -> Iterator[Finding]:
    if not definition.steps:
        yield Finding(
            Severity.ERROR,
            Code.NO_STEPS,
            "/steps",
            "This workflow has no steps, so there is nothing for it to do.",
            "Add a step - an agent task, a tool call, or a condition.",
        )
        return

    seen: set[str] = set()
    for step in definition.steps:
        if step.step_key in seen:
            yield Finding(
                Severity.ERROR,
                Code.DUPLICATE_STEP_KEY,
                f"/steps/{step.step_key}",
                f"Two steps are both called '{step.step_key}'.",
                "Rename one of them. Step names are how edges and conditions refer to them.",
            )
        seen.add(step.step_key)

    for edge in definition.edges:
        for role, key in (("from", edge.from_step), ("to", edge.to_step)):
            if key is not None and key not in seen:
                yield Finding(
                    Severity.ERROR,
                    Code.DANGLING_EDGE,
                    f"/edges/{edge.index}",
                    f"This connection points {role} a step called '{key}', which does not exist.",
                    "Delete the connection, or create the step it points at.",
                )


# ----------------------------------------------------------------- the graph ---


def _graph(definition: WorkflowDefinition) -> Iterator[Finding]:
    """Entry points, cycles, and everything a run would never arrive at."""
    keys = {step.step_key for step in definition.steps}
    if not keys:
        return

    outgoing: dict[str, list[str]] = {key: [] for key in keys}
    has_incoming: set[str] = set()
    for edge in definition.edges:
        if edge.to_step not in keys:
            continue
        if edge.from_step is None:
            continue
        if edge.from_step not in keys:
            continue
        outgoing[edge.from_step].append(edge.to_step)
        has_incoming.add(edge.to_step)

    # Three kinds of step have nothing pointing at them, and they are not
    # the same thing. A *declared* entry is what the author marked. A step
    # with no incoming edge but outgoing ones is a start they drew. A step
    # with no edges at all is almost always one they dragged out and never
    # connected - which is the `cleanup` in the proposal's example, and the
    # commonest real mistake in a visual builder.
    declared = {edge.to_step for edge in definition.edges if edge.from_step is None}
    # A step with no edges is only "left out" if there is something to be
    # left out of. A workflow of one step and no edges is complete, not
    # disconnected - and so is a workflow of three steps that all run on
    # their own, which earns the multiple-starts warning instead.
    isolated = (
        {key for key in keys - has_incoming if not outgoing[key]}
        if any(edge.from_step is not None for edge in definition.edges)
        else set()
    )
    entries = declared or (keys - has_incoming - isolated)

    for key in sorted(isolated):
        if key in declared:
            continue
        yield Finding(
            Severity.ERROR,
            Code.UNREACHABLE_STEP,
            f"/steps/{key}",
            f"'{key}' is not connected to anything, so it would never run.",
            "Connect it to a step that runs before it, or delete it.",
        )

    # More than one start is legal - the engine begins both - but it is far
    # more often an accident than a plan, so it is said out loud without
    # blocking the publish.
    if len(entries) > 1 and not declared:
        yield Finding(
            Severity.WARNING,
            Code.MULTIPLE_ENTRY_STEPS,
            "/steps",
            "This workflow has more than one starting point ("
            + ", ".join(f"'{key}'" for key in sorted(entries))
            + "), so several branches begin at once.",
            "Connect them in the order you want, or confirm they should run in parallel.",
        )

    if not entries and not isolated:
        yield Finding(
            Severity.ERROR,
            Code.NO_ENTRY_STEP,
            "/steps",
            "Every step here is waiting for another one, so nothing can start.",
            "Remove a connection so that one step has nothing pointing at it.",
        )

    cycle_members = _cycles(keys, outgoing)
    for edge in definition.edges:
        if (
            edge.from_step in cycle_members
            and edge.to_step in cycle_members
            and edge.from_step is not None
        ):
            yield Finding(
                Severity.ERROR,
                Code.CYCLE,
                f"/edges/{edge.index}",
                f"'{edge.from_step}' and '{edge.to_step}' are part of a loop, "
                "so this workflow would never finish.",
                "Remove one of the connections in the loop. To repeat work, "
                "use a step that retries rather than a loop.",
            )

    reachable = _reachable(entries, outgoing)
    for step in definition.steps:
        if (
            step.step_key in reachable
            or step.step_key in cycle_members
            or step.step_key in isolated
        ):
            continue
        # What is left: a step whose only predecessors are themselves
        # unreachable, or fed exclusively from a cycle. Rarer than the
        # isolated case and worth a different sentence.
        yield Finding(
            Severity.ERROR,
            Code.UNREACHABLE_STEP,
            f"/steps/{step.step_key}",
            f"Nothing that runs leads to '{step.step_key}', so it would never run.",
            "Connect it to a step that runs before it, or delete it.",
        )


def _cycles(keys: set[str], outgoing: dict[str, list[str]]) -> frozenset[str]:
    """Every step on a cycle, by iterated removal of what cannot be on one.

    Kahn's algorithm run to exhaustion: whatever still has an unresolved
    predecessor when no node has in-degree zero is on a cycle or downstream
    of one. Simpler than enumerating cycles, and the answer the user needs is
    "which edges are involved", not "here is the shortest cycle".
    """
    in_degree = dict.fromkeys(keys, 0)
    for source in keys:
        for target in outgoing[source]:
            in_degree[target] += 1

    queue = [key for key, degree in in_degree.items() if degree == 0]
    settled: set[str] = set()
    while queue:
        key = queue.pop()
        settled.add(key)
        for target in outgoing[key]:
            in_degree[target] -= 1
            if in_degree[target] == 0:
                queue.append(target)
    return frozenset(keys - settled)


def _reachable(entries: set[str], outgoing: dict[str, list[str]]) -> frozenset[str]:
    seen: set[str] = set()
    stack = list(entries)
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        stack.extend(outgoing.get(key, ()))
    return frozenset(seen)


# ------------------------------------------------------------ the assignments ---


def _assignments(definition: WorkflowDefinition) -> Iterator[Finding]:
    """The checks that make a drawn line honest."""
    for step in definition.steps:
        if step.kind in _ASSIGNABLE:
            yield from _agent_checks(definition, step)
        if step.kind is StepKind.TOOL_CALL and not step.tool_name:
            yield Finding(
                Severity.ERROR,
                Code.TOOL_MISSING,
                f"/steps/{step.step_key}",
                "This step calls a tool but no tool is chosen.",
                "Pick the tool this step should call.",
            )
        if step.tool_name:
            yield from _tool_checks(definition, step)


def _agent_checks(definition: WorkflowDefinition, step: StepDefinition) -> Iterator[Finding]:
    if not step.agent_key:
        yield Finding(
            Severity.ERROR,
            Code.AGENT_MISSING,
            f"/steps/{step.step_key}",
            "This step gives work to an agent but no agent is chosen.",
            "Pick the agent that should do this work.",
        )
        return

    agent = definition.agents.get(step.agent_key)
    if agent is None:
        yield Finding(
            Severity.ERROR,
            Code.UNKNOWN_AGENT,
            f"/steps/{step.step_key}/agent_key",
            f"There is no agent called '{step.agent_key}' in this project.",
            "Pick an existing agent, or create one with that name.",
        )
        return

    if agent.interaction_mode is AgentInteractionMode.OBSERVED:
        # The honesty check. An observed agent is one AgentWorld can watch
        # and cannot instruct, and a line drawn to it on the island looks
        # exactly like a line drawn to one it can.
        name = agent.display_name or agent.agent_key
        yield Finding(
            Severity.ERROR,
            Code.OBSERVED_AGENT_ASSIGNED,
            f"/steps/{step.step_key}",
            f"'{name}' only reports activity and cannot be given work.",
            "Pick a managed agent, or connect this one so it can accept tasks.",
        )


def _tool_checks(definition: WorkflowDefinition, step: StepDefinition) -> Iterator[Finding]:
    tool = step.tool_name
    assert tool is not None

    agent = definition.agents.get(step.agent_key) if step.agent_key else None
    if agent is not None and tool not in agent.granted_tools:
        name = agent.display_name or agent.agent_key
        yield Finding(
            Severity.ERROR,
            Code.TOOL_NOT_GRANTED,
            f"/steps/{step.step_key}/tool_id",
            f"'{name}' has no connection granting {tool}.",
            "Add a tool grant for this agent, or let the builder add it.",
        )

    effect = definition.tool_effects.get(tool)
    if effect is SideEffect.CONSEQUENTIAL and not step.requires_approval:
        # A warning, not an error: gating it is the approval policy's call,
        # and refusing to publish would be this function overruling it.
        yield Finding(
            Severity.WARNING,
            Code.CONSEQUENTIAL_WITHOUT_APPROVAL,
            f"/steps/{step.step_key}",
            f"This step uses {tool}, whose effects reach outside AgentWorld and "
            "cannot be undone, and nothing gates it.",
            "Require approval for this step, or confirm it should run unattended.",
        )


# -------------------------------------------------------------- the approvals ---


def _approvals(definition: WorkflowDefinition) -> Iterator[Finding]:
    """A gate nobody can open is a workflow that can never finish.

    `requires_different_user` in a workspace with one eligible approver is a
    deadlock built by configuration, and the only moment it is cheap to
    notice is before publishing.
    """
    for step in definition.steps:
        if not step.approval_requires_different_user:
            continue
        if definition.eligible_approvers >= 2:
            continue
        yield Finding(
            Severity.ERROR,
            Code.APPROVAL_UNSATISFIABLE,
            f"/steps/{step.step_key}",
            "This step needs approval from somebody other than whoever runs it, "
            "and this workspace has only one member who can approve.",
            "Invite another member, or allow self-approval for this step in Settings → Approvals.",
        )


# ------------------------------------------------------------- the expressions ---


def _expressions(definition: WorkflowDefinition) -> Iterator[Finding]:
    """Every condition and mapping, parsed now rather than mid-execution.

    A condition that cannot parse should fail validation. Discovering it when
    an edge is evaluated would strand a running execution for a typo.
    """
    for step in definition.steps:
        if step.condition:
            yield from _parses(step.condition, f"/steps/{step.step_key}/condition")
        for field_name, expression in step.input_mapping.items():
            yield from _parses(expression, f"/steps/{step.step_key}/input_mapping/{field_name}")
    for edge in definition.edges:
        if edge.condition:
            yield from _parses(edge.condition, f"/edges/{edge.index}/condition")


def _parses(expression: str, pointer: str) -> Iterator[Finding]:
    try:
        parse(expression)
    except ValidationFailedError as error:
        yield Finding(
            Severity.ERROR,
            Code.INVALID_EXPRESSION,
            pointer,
            f"This expression cannot be read: {error.detail}",
            "Values come from `$`, as in `$.steps.research.output.count > 3`.",
        )

"""Compiling a `delegate` connection into a two-step workflow.

`03-API-AND-FRONTEND.md` §7.1 is the authority:

> **`create_team` compiles to a real workflow.** ... a "team" is not a new
> entity and not a special execution path. The builder emits a
> `workflow_version` with one `agent_task` step per member, wired in the
> order the delegation implies, and it runs on the same orchestrator as
> everything else.

So this module writes a graph in exactly the shape a person could have drawn
by hand, and nothing else. There is no team entity, no team execution, no
second `advance`. What comes out of here is handed to the ordinary draft,
the ordinary `validate`, the ordinary publish and the ordinary worker.

**A connection is not an edge.** `ConnectionKind.DELEGATE` says agent A *may*
hand work to agent B; it outlives any workflow and names no order, no input
and no mapping. A workflow edge says B runs after A and reads A's output.
Turning the first into the second is a decision, and the decisions are the
contract at the top of this module rather than something each caller invents.

**What this module judges, and what it does not.** It judges the *shape of
the delegation* - which `validate` can never see, because connections are not
part of a graph - and whether it can write a reference expression at all. It
does **not** re-judge the resulting graph: whether the downstream agent is
`observed`, whether an agent was archived between compiling and publishing,
whether an approval can be satisfied. Those are `validate`'s findings, and
asking the same question in two places is how a workflow comes to validate
at publish and decide differently at run time.

**Exactly one directed pair, in this release.** "The order the delegation
implies" is only well defined for one pair. Three agents in a fan-out have no
implied order at all, and a chain of four has one this release has not
specified an input contract for. Each unsupported shape is refused by name
with a finding that points at the connection responsible, because the one
thing worse than refusing is quietly compiling a guess.

Pure: no database, no clock, no exceptions. A caller that passes nonsense
gets findings, which is what lets the whole decision table be tested without
a Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from agentworld.domain.enums import ConnectionKind, StepKind
from agentworld.domain.errors import ValidationFailedError
from agentworld.domain.expressions import parse
from agentworld.domain.slugs import is_valid as valid_slug
from agentworld.domain.slugs import slugify

# ------------------------------------------------------------- the contract ---

#: The field name both steps receive their work under. One name, not two: the
#: downstream agent's prompt is rendered from `instruction["input"]` as
#: `"<field>: <value>"` lines, and a field called after the upstream step
#: would make the prompt read differently depending on who delegated.
HANDOFF_FIELD: Final = "input"

#: What the first step reads. A compiled team is run with
#: `{"input": {"input": "..."}}`, so every team takes the same request shape
#: whoever its members are.
ENTRY_EXPRESSION: Final = "$.input.input"

#: `agent_key` is the step key. It is unique per project
#: (`agents_project_key_key`), `citext`, validated as a slug at creation, and
#: absent from `AgentUpdate` - so it cannot change under a published version
#: whose `$.steps.<key>` references are frozen. A display name is none of
#: those things, which is why it names the workflow and nothing else.
STEP_KIND: Final = StepKind.AGENT_TASK

#: `Name` in the API schemas is 200 characters, and a compiled name is two
#: display names joined. Halving the budget keeps a compiled workflow
#: readable over the same API that created its agents.
_JOIN: Final = " then "
_LABEL_BUDGET: Final = (200 - len(_JOIN)) // 2


def handoff_expression(upstream_step_key: str) -> str:
    """What the downstream step reads: the upstream agent's text output.

    `.output.text` rather than the whole output object, because a managed
    turn's output is `{"text", "stop_reason", "model", "usage"}` and handing
    an agent the token counts as part of its instructions would be absurd.
    """
    return f"$.steps.{upstream_step_key}.output.text"


# -------------------------------------------------------------- the findings ---


class DelegationCode(StrEnum):
    """Every reason this release refuses to compile a delegation.

    A closed set, for the same reason `validate.Code` is one: the builder has
    to map each refusal onto something it can offer to fix, and it cannot do
    that for free text.
    """

    NOT_A_DELEGATION = "not_a_delegation"
    DELEGATION_DISABLED = "delegation_disabled"
    SELF_DELEGATION = "self_delegation"
    UNKNOWN_PARTICIPANT = "unknown_participant"
    DUPLICATE_DELEGATION = "duplicate_delegation"
    FAN_OUT = "fan_out"
    FAN_IN = "fan_in"
    CHAIN_TOO_LONG = "chain_too_long"
    CYCLE = "cycle"
    UNUSABLE_AGENT_KEY = "unusable_agent_key"


@dataclass(frozen=True, slots=True)
class DelegationFinding:
    """One reason, pointing at the connection responsible for it."""

    code: DelegationCode
    pointer: str
    detail: str
    remedy: str = ""


# ----------------------------------------------------------------- the input ---


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """An agent, in the only three respects compiling needs.

    Not `AgentFacts`: that one is keyed by `agent_key` and carries what
    *validation* needs. Compiling starts from connection endpoints, which are
    ids, so it needs the id too.
    """

    agent_id: UUID
    agent_key: str
    display_name: str = ""

    @property
    def label(self) -> str:
        """What a person calls this agent. The key is the fallback, never the
        identifier that leaks into a graph."""
        return (self.display_name or self.agent_key).strip()[:_LABEL_BUDGET].strip()


@dataclass(frozen=True, slots=True)
class DelegationFacts:
    """One connection row, as stored. No interpretation."""

    connection_id: UUID
    kind: ConnectionKind
    source_kind: str
    source_id: UUID
    target_kind: str
    target_id: UUID
    enabled: bool = True

    @property
    def between_agents(self) -> bool:
        return self.source_kind == "agent" and self.target_kind == "agent"


# ---------------------------------------------------------------- the output ---


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    """What to create. A draft's worth of fields and nothing more.

    `graph` is the jsonb a draft holds, in the shape
    `definition_for_execution` reads - the same shape a person drawing two
    steps on the island would save.
    """

    name: str
    slug: str
    description: str
    graph: Mapping[str, Any]
    upstream_step_key: str
    downstream_step_key: str


@dataclass(frozen=True, slots=True)
class Compilation:
    """Either a workflow to create or the reasons there is none.

    Never both, and never neither. The two constructors are the only way to
    build one, so "a refusal that also carries a graph" is not a state a
    caller has to defend against.
    """

    findings: tuple[DelegationFinding, ...] = ()
    workflow: CompiledWorkflow | None = None

    @classmethod
    def refused(cls, findings: Sequence[DelegationFinding]) -> Compilation:
        assert findings, "a refusal with no reason is not a refusal"
        return cls(findings=tuple(findings))

    @classmethod
    def ready(cls, workflow: CompiledWorkflow) -> Compilation:
        return cls(workflow=workflow)

    @property
    def compiled(self) -> bool:
        return self.workflow is not None

    def codes(self) -> frozenset[DelegationCode]:
        return frozenset(finding.code for finding in self.findings)


# -------------------------------------------------------------- the compiler ---


def compile_delegation(
    selected: DelegationFacts,
    *,
    delegations: Sequence[DelegationFacts] = (),
    agents: Sequence[AgentIdentity] = (),
) -> Compilation:
    """Compile one delegation, or say why this release will not.

    `delegations` is every delegate connection in the project, including
    `selected` - the caller passes what it has rather than deciding what is
    relevant, because deciding relevance *is* the fan-out check. `agents` is
    the project's live agents; an archived one is absent, so a delegation
    naming it is refused rather than compiled into a step nothing can run.
    """
    live = {agent.agent_id: agent for agent in agents}

    if refusals := list(_eligible(selected, live)):
        return Compilation.refused(refusals)

    upstream = live[selected.source_id]
    downstream = live[selected.target_id]

    refusals = list(_shape(selected, upstream, downstream, delegations, live))
    refusals.extend(_writable(selected, upstream, downstream))
    if refusals:
        return Compilation.refused(refusals)

    return Compilation.ready(_two_steps(upstream, downstream))


# --------------------------------------------------------------- the reasons ---


def _eligible(
    selected: DelegationFacts, live: Mapping[UUID, AgentIdentity]
) -> Iterator[DelegationFinding]:
    """Is this row even a delegation between two agents that exist?"""
    at = _pointer(selected.connection_id)

    if selected.kind is not ConnectionKind.DELEGATE or not selected.between_agents:
        yield DelegationFinding(
            DelegationCode.NOT_A_DELEGATION,
            at,
            f"This connection is a '{selected.kind.value}' between "
            f"{selected.source_kind} and {selected.target_kind}, not a delegation "
            "from one agent to another.",
            "Compile a delegate connection, or draw one between the two agents.",
        )
        return

    if not selected.enabled:
        yield DelegationFinding(
            DelegationCode.DELEGATION_DISABLED,
            at,
            "This delegation is turned off, so handing work over is not "
            "something it currently permits.",
            "Turn the delegation on, then compile it.",
        )
        return

    if selected.source_id == selected.target_id:
        yield DelegationFinding(
            DelegationCode.SELF_DELEGATION,
            at,
            "This delegation points an agent at itself, so there is no second "
            "agent for the work to go to.",
            "Point it at the agent that should take the work.",
        )
        return

    for role, agent_id in (("from", selected.source_id), ("to", selected.target_id)):
        if agent_id not in live:
            yield DelegationFinding(
                DelegationCode.UNKNOWN_PARTICIPANT,
                f"{at}/{'source_id' if role == 'from' else 'target_id'}",
                f"The agent this delegation points {role} is not an active agent "
                "in this project - it was archived, or it never existed here.",
                "Point it at an active agent, or restore the one it names.",
            )


def _shape(
    selected: DelegationFacts,
    upstream: AgentIdentity,
    downstream: AgentIdentity,
    delegations: Sequence[DelegationFacts],
    live: Mapping[UUID, AgentIdentity],
) -> Iterator[DelegationFinding]:
    """Every other delegation that touches these two agents.

    One directed pair is the whole of what this release supports, so any
    second delegation on either participant makes the implied order a guess.
    Each case is named separately because the repair differs: a fan-out wants
    a choice, a cycle wants a deletion, a chain wants a release that supports
    it.

    A delegation elsewhere in the project - between two agents that are
    neither of these - is not this compilation's business and is ignored.
    Unresolvable and disabled ones are ignored too: an agent that is archived
    or a grant that is off cannot hand work anywhere, so counting it would
    refuse a delegation that is in fact unambiguous.
    """
    a, b = upstream.agent_id, downstream.agent_id

    for other in delegations:
        if other.connection_id == selected.connection_id:
            continue
        if other.kind is not ConnectionKind.DELEGATE or not other.between_agents:
            continue
        if not other.enabled:
            continue
        if other.source_id not in live or other.target_id not in live:
            continue

        source, target = other.source_id, other.target_id
        at = _pointer(other.connection_id)

        if {source, target} == {a, b} and source == a:
            yield DelegationFinding(
                DelegationCode.DUPLICATE_DELEGATION,
                at,
                f"'{upstream.agent_key}' already hands work to "
                f"'{downstream.agent_key}' through another connection, so it is "
                "not clear which one a team should be built from.",
                "Delete the duplicate delegation, then compile the one that remains.",
            )
        elif source == b and target == a:
            yield DelegationFinding(
                DelegationCode.CYCLE,
                at,
                f"'{downstream.agent_key}' also hands work back to "
                f"'{upstream.agent_key}', so the two would never finish.",
                "Delete one of the two delegations so the work has a direction.",
            )
        elif source == target and source in (a, b):
            yield DelegationFinding(
                DelegationCode.CYCLE,
                at,
                f"'{live[source].agent_key}' hands work to itself, so a workflow "
                "built from these delegations would never finish.",
                "Delete the self-delegation.",
            )
        elif source == a:
            yield DelegationFinding(
                DelegationCode.FAN_OUT,
                at,
                f"'{upstream.agent_key}' hands work to more than one agent, and "
                "this release cannot say which of them should go first.",
                "Keep one delegation from this agent, or wait for workflows that branch.",
            )
        elif target == b:
            yield DelegationFinding(
                DelegationCode.FAN_IN,
                at,
                f"More than one agent hands work to '{downstream.agent_key}', and "
                "this release cannot say which of them it should wait for.",
                "Keep one delegation into this agent, or wait for workflows that join.",
            )
        elif a in (source, target) or b in (source, target):
            other_end = live[target if source in (a, b) else source]
            yield DelegationFinding(
                DelegationCode.CHAIN_TOO_LONG,
                at,
                f"This delegation extends the chain to '{other_end.agent_key}', and "
                "this release compiles two steps.",
                "Compile the two agents that should work together, and delete or "
                "disable the rest of the chain.",
            )


def _writable(
    selected: DelegationFacts, upstream: AgentIdentity, downstream: AgentIdentity
) -> Iterator[DelegationFinding]:
    """Can the handoff this module promises actually be written down?

    `agent_key` is a slug, and a slug may begin with a digit; an expression
    name may not (`[A-Za-z_][A-Za-z0-9_-]*`). So `2nd-writer` is a perfectly
    legal agent key that cannot appear in `$.steps.<key>.output.text`. The
    parser is asked rather than the rule restated, because a compiler that
    emitted an expression its own parser rejects would produce a draft that
    can never be published and blame the graph.
    """
    at = _pointer(selected.connection_id)

    for role, agent in (("source_id", upstream), ("target_id", downstream)):
        if not valid_slug(agent.agent_key):
            yield DelegationFinding(
                DelegationCode.UNUSABLE_AGENT_KEY,
                f"{at}/{role}",
                f"'{agent.agent_key}' cannot be used as a step name.",
                "Recreate the agent with a key of lowercase letters, digits and hyphens.",
            )

    try:
        parse(handoff_expression(upstream.agent_key))
    except ValidationFailedError:
        yield DelegationFinding(
            DelegationCode.UNUSABLE_AGENT_KEY,
            f"{at}/source_id",
            f"The output of '{upstream.agent_key}' cannot be referred to in an "
            "expression, so there is no way to hand it to the second agent. "
            "Step names used in expressions must begin with a letter.",
            "Recreate this agent with a key that begins with a letter.",
        )


# ----------------------------------------------------------------- the graph ---


def _two_steps(upstream: AgentIdentity, downstream: AgentIdentity) -> CompiledWorkflow:
    """The graph. Two `agent_task` steps, one edge, one mapping each."""
    up, down = upstream.agent_key, downstream.agent_key
    return CompiledWorkflow(
        name=f"{upstream.label}{_JOIN}{downstream.label}",
        # From the keys, never the display names: a slug is half of a URL and
        # of a project-unique identity, and a display name can be renamed,
        # emoji-only, or identical to another agent's.
        slug=slugify(f"{up}-then-{down}"),
        description=f"Compiled from the delegation from '{up}' to '{down}'.",
        graph={
            "steps": [
                {
                    "step_key": up,
                    "kind": STEP_KIND.value,
                    "agent_key": up,
                    "input_mapping": {HANDOFF_FIELD: ENTRY_EXPRESSION},
                },
                {
                    "step_key": down,
                    "kind": STEP_KIND.value,
                    "agent_key": down,
                    "input_mapping": {HANDOFF_FIELD: handoff_expression(up)},
                },
            ],
            "edges": [{"from": up, "to": down}],
        },
        upstream_step_key=up,
        downstream_step_key=down,
    )


def _pointer(connection_id: UUID) -> str:
    return f"/connections/{connection_id}"

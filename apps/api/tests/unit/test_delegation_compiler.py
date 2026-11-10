"""The delegation compiler: one supported shape, and every refusal by name.

Two kinds of test live here. The first pins the **contract** - the field the
handoff travels under, the expression each step reads, and the fact that the
step key is the agent key - because a published version freezes those and
every later `$.steps.<key>` reference depends on them. Changing one of these
assertions is not a refactor; it is a change to what already-published
workflows mean.

The second kind is the decision table. `compile_delegation` is pure and total,
so every unsupported graph shape can be stated in four lines and asserted on
the finding it produces. That is the point of the shape check living in the
domain rather than in the service: a fan-out needs three agents, two
connections and no database.

The last two tests are the ones that keep the rest honest - every member of
`DelegationCode` must appear above, and the compiled graph must be something
the *existing* validator and the *existing* `advance` accept, rather than a
shape only this module understands.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from agentworld.domain.entities import WorkflowDraft
from agentworld.domain.enums import (
    AgentInteractionMode,
    ConnectionKind,
    ExecutionStatus,
    StepKind,
    StepStatus,
)
from agentworld.domain.errors import ValidationFailedError
from agentworld.domain.expressions import evaluate, parse
from agentworld.domain.orchestration import (
    ENTRY_EXPRESSION,
    HANDOFF_FIELD,
    AgentFacts,
    AgentIdentity,
    Compilation,
    DelegationCode,
    DelegationFacts,
    DispatchStep,
    ExecutionState,
    StepState,
    advance,
    compile_delegation,
    handoff_expression,
    validate,
)
from agentworld.services.definitions import definition_for_execution
from agentworld.services.executions import PUBLISHABLE_KINDS

RESEARCHER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WRITER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
STRANGER = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def agent(agent_id: uuid.UUID, key: str, name: str = "") -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, agent_key=key, display_name=name)


def delegation(
    source: uuid.UUID,
    target: uuid.UUID,
    *,
    connection_id: uuid.UUID | None = None,
    kind: ConnectionKind = ConnectionKind.DELEGATE,
    source_kind: str = "agent",
    target_kind: str = "agent",
    enabled: bool = True,
) -> DelegationFacts:
    return DelegationFacts(
        connection_id=connection_id or uuid.uuid4(),
        kind=kind,
        source_kind=source_kind,
        source_id=source,
        target_kind=target_kind,
        target_id=target,
        enabled=enabled,
    )


#: The pair every test starts from: Researcher delegates to Writer.
TEAM = (
    agent(RESEARCHER, "researcher", "Researcher"),
    agent(WRITER, "writer", "Writer"),
)


def compiled(
    *, delegations: tuple[DelegationFacts, ...] = (), agents: tuple[AgentIdentity, ...] = TEAM
) -> Compilation:
    selected = delegation(RESEARCHER, WRITER, connection_id=_SELECTED)
    return compile_delegation(selected, delegations=(selected, *delegations), agents=agents)


_SELECTED = uuid.UUID("00000000-0000-0000-0000-00000000dddd")


# ------------------------------------------------------------- the contract ---


def test_one_delegation_compiles_to_the_two_step_graph() -> None:
    """The whole contract, in one assertion on the emitted graph.

    Written out in full rather than probed field by field, so that adding a
    key - a condition, an approval, a second mapping field - fails here and
    has to be justified against every published version that froze this
    shape.
    """
    result = compiled()
    assert result.compiled
    workflow = result.workflow
    assert workflow is not None
    assert workflow.graph == {
        "steps": [
            {
                "step_key": "researcher",
                "kind": "agent_task",
                "agent_key": "researcher",
                "input_mapping": {"input": "$.input.input"},
            },
            {
                "step_key": "writer",
                "kind": "agent_task",
                "agent_key": "writer",
                "input_mapping": {"input": "$.steps.researcher.output.text"},
            },
        ],
        "edges": [{"from": "researcher", "to": "writer"}],
    }


def test_the_step_key_is_the_agent_key() -> None:
    """`agent_key`, not the display name and not a generated name.

    It is unique per project, validated as a slug, and absent from
    `AgentUpdate` - so it cannot change under a published version whose
    expressions name it. A display name is none of those.
    """
    result = compiled(agents=(agent(RESEARCHER, "scout", "Scout 9000"), agent(WRITER, "scribe")))
    workflow = result.workflow
    assert workflow is not None
    assert (workflow.upstream_step_key, workflow.downstream_step_key) == ("scout", "scribe")
    assert [step["step_key"] for step in workflow.graph["steps"]] == ["scout", "scribe"]


def test_the_handoff_reads_the_upstream_text_output() -> None:
    """The downstream agent receives the upstream agent's text, under `input`.

    Asserted by evaluating the expression against the scope `advance` builds,
    rather than by comparing strings: the claim is that the handoff resolves,
    not that it is spelled a particular way.
    """
    assert HANDOFF_FIELD == "input"
    assert handoff_expression("researcher") == "$.steps.researcher.output.text"
    scope = {
        "input": {"input": "look into fintech"},
        "steps": {"researcher": {"output": {"text": "Acme raised a round.", "usage": {}}}},
    }
    assert evaluate(handoff_expression("researcher"), scope) == "Acme raised a round."
    assert evaluate(ENTRY_EXPRESSION, scope) == "look into fintech"


def test_the_persisted_entry_contract_cannot_be_renamed() -> None:
    """The same two literals, pinned here too, and for the same reason.

    A team's compiled graph is published like any other workflow and frozen
    the same way: `workflow_steps.input_mapping` holds `$.input.input`, and
    `advance` is given the execution's own frozen rows rather than a fresh
    compilation. Every version published before a rename would keep reading
    the old expression while callers sent the new field, and `_mapped` omits
    what does not resolve instead of failing - so an old team's first step
    would receive nothing at all, and its execution would succeed.

    Agent Test harnesses are not republished after creation, which is the
    sharper case and is why `test_agent_test_compiler.py` pins the identical
    pair: an authored workflow can be published again, a harness cannot.
    Changing either constant requires designing a supported harness
    republishing mechanism first.

    Asserted as literals rather than against `agent_test`'s constants,
    because two modules renamed together agree with each other while
    disagreeing with every graph already in the database.
    """
    assert HANDOFF_FIELD == "input"
    assert ENTRY_EXPRESSION == "$.input.input"

    workflow = compiled().workflow
    assert workflow is not None
    assert workflow.graph["steps"][0]["input_mapping"] == {"input": "$.input.input"}

    scope = {"input": {HANDOFF_FIELD: "look into fintech"}, "steps": {}}
    assert evaluate("$.input.input", scope) == "look into fintech"


def test_the_name_comes_from_display_names_and_the_slug_from_keys() -> None:
    """Identity and label are not the same thing.

    A slug is half of a project-unique identity and of a URL; a display name
    can be renamed to another agent's, folded to nothing, or be emoji. So the
    slug is built from the keys and the name from the names.
    """
    result = compiled(
        agents=(agent(RESEARCHER, "scout", "Scout"), agent(WRITER, "scribe", "Scribe"))
    )
    workflow = result.workflow
    assert workflow is not None
    assert workflow.name == "Scout then Scribe"
    assert workflow.slug == "scout-then-scribe"


def test_a_missing_display_name_falls_back_to_the_key_and_never_to_nothing() -> None:
    """An unnamed agent still produces a workflow somebody can find."""
    result = compiled(agents=(agent(RESEARCHER, "scout"), agent(WRITER, "scribe")))
    workflow = result.workflow
    assert workflow is not None
    assert workflow.name == "scout then scribe"


def test_a_long_pair_of_display_names_stays_within_the_api_name_limit() -> None:
    """`Name` is 200 characters at the API, and a compiled name is two of them."""
    result = compiled(
        agents=(agent(RESEARCHER, "scout", "S" * 300), agent(WRITER, "scribe", "W" * 300))
    )
    workflow = result.workflow
    assert workflow is not None
    assert len(workflow.name) <= 200


def test_the_description_names_the_delegation_it_came_from() -> None:
    """Somebody opening this workflow should not have to guess why it exists."""
    workflow = compiled().workflow
    assert workflow is not None
    assert "researcher" in workflow.description and "writer" in workflow.description


def test_the_agent_key_cannot_be_changed_through_the_api() -> None:
    """The guarantee the step key rests on, asserted where it is relied on.

    A published version freezes `$.steps.<agent_key>` references and a running
    execution is pinned to that version. If `agent_key` became editable,
    renaming an agent would break every compiled team naming it *silently*:
    the expression would resolve to `MISSING`, `_mapped` omits a field that
    does not resolve, and the second agent would be handed nothing with no
    error anywhere.

    `AgentUpdate` is where that would be introduced, because the repository's
    `update(**fields)` writes whatever it is given. So this asserts the
    request model rather than the column - the column is writable, and what
    makes the key immutable is that nothing offers to write it.
    """
    from agentworld.api.v1.agents import AgentUpdate

    assert "agent_key" not in AgentUpdate.model_fields, (
        "agent_key is now editable, so a published workflow's step references can be "
        "renamed out from under a running execution"
    )


# -------------------------------------------------------- unsupported shapes ---


def test_a_connection_that_is_not_a_delegation_is_refused() -> None:
    """The kind is checked here, not trusted from the caller."""
    grant = delegation(RESEARCHER, WRITER, kind=ConnectionKind.GRANT_TOOL)
    result = compile_delegation(grant, delegations=(grant,), agents=TEAM)
    assert not result.compiled
    assert result.codes() == {DelegationCode.NOT_A_DELEGATION}


def test_a_delegation_between_something_other_than_two_agents_is_refused() -> None:
    """`delegate` is agent -> agent. A row saying otherwise is not one."""
    odd = delegation(RESEARCHER, WRITER, target_kind="workflow")
    result = compile_delegation(odd, delegations=(odd,), agents=TEAM)
    assert result.codes() == {DelegationCode.NOT_A_DELEGATION}


def test_a_disabled_delegation_is_refused() -> None:
    """Disabled means handing work over is not permitted, so there is no team
    to compile - and compiling one anyway would make the toggle cosmetic."""
    off = delegation(RESEARCHER, WRITER, enabled=False)
    result = compile_delegation(off, delegations=(off,), agents=TEAM)
    assert result.codes() == {DelegationCode.DELEGATION_DISABLED}


def test_an_agent_delegating_to_itself_is_refused() -> None:
    loop = delegation(RESEARCHER, RESEARCHER)
    result = compile_delegation(loop, delegations=(loop,), agents=TEAM)
    assert result.codes() == {DelegationCode.SELF_DELEGATION}


@pytest.mark.parametrize("missing", ["source", "target"])
def test_a_delegation_naming_an_agent_that_is_not_live_is_refused(missing: str) -> None:
    """Archived, deleted, or never in this project - one finding for all three.

    The compiler is given only live agents, so "archived" and "belongs to
    another project" are the same fact to it. That is the same reason
    `facts_and_effects` excludes archived agents: archiving one is a decision
    its workflows should notice.
    """
    live = (TEAM[1],) if missing == "source" else (TEAM[0],)
    result = compiled(agents=live)
    assert result.codes() == {DelegationCode.UNKNOWN_PARTICIPANT}
    assert result.findings[0].pointer.endswith(f"/{missing}_id")


def test_a_duplicate_delegation_between_the_same_pair_is_refused() -> None:
    """Two rows saying the same thing: which one is the team built from?

    Harmless in effect - both compile to the same graph - and still refused,
    because the second compile would collide on the slug and the caller would
    learn about the duplicate as a conflict rather than as a duplicate.
    """
    result = compiled(delegations=(delegation(RESEARCHER, WRITER),))
    assert result.codes() == {DelegationCode.DUPLICATE_DELEGATION}


def test_a_delegation_back_to_the_first_agent_is_refused_as_a_cycle() -> None:
    result = compiled(delegations=(delegation(WRITER, RESEARCHER),))
    assert result.codes() == {DelegationCode.CYCLE}


def test_a_participant_delegating_to_itself_is_refused_as_a_cycle() -> None:
    """A self-delegation elsewhere on the pair, which the selected row is not."""
    result = compiled(delegations=(delegation(WRITER, WRITER),))
    assert result.codes() == {DelegationCode.CYCLE}


def test_an_agent_delegating_to_two_others_is_refused_as_fan_out() -> None:
    """The case §7.1's "the order the delegation implies" does not cover.

    Two targets imply no order at all, and picking one would be the compiler
    inventing a workflow nobody drew.
    """
    result = compiled(
        delegations=(delegation(RESEARCHER, STRANGER),),
        agents=(*TEAM, agent(STRANGER, "editor", "Editor")),
    )
    assert result.codes() == {DelegationCode.FAN_OUT}


def test_two_agents_delegating_into_one_is_refused_as_fan_in() -> None:
    result = compiled(
        delegations=(delegation(STRANGER, WRITER),),
        agents=(*TEAM, agent(STRANGER, "editor", "Editor")),
    )
    assert result.codes() == {DelegationCode.FAN_IN}


@pytest.mark.parametrize("extra", ["onward", "inbound"])
def test_a_chain_of_three_is_refused(extra: str) -> None:
    """Two steps is what this release compiles, in either direction.

    `researcher -> writer -> editor` and `editor -> researcher -> writer` are
    both chains of three; neither is two agents working together, and the
    input contract for a third step has not been decided.
    """
    third = delegation(WRITER, STRANGER) if extra == "onward" else delegation(STRANGER, RESEARCHER)
    result = compiled(delegations=(third,), agents=(*TEAM, agent(STRANGER, "editor", "Editor")))
    assert result.codes() == {DelegationCode.CHAIN_TOO_LONG}


def test_an_agent_key_that_cannot_be_referenced_in_an_expression_is_refused() -> None:
    """The edge that forces this check to exist, measured rather than assumed.

    `2nd-writer` is a valid slug and therefore a valid `agent_key`, but an
    expression name must begin with a letter, so `$.steps.2nd-writer.…` does
    not parse. Compiling it anyway would produce a draft that can never be
    published, and the finding would point at a mapping the user never wrote.
    """
    with pytest.raises(ValidationFailedError):
        parse("$.steps.2nd-writer.output.text")

    result = compiled(agents=(agent(RESEARCHER, "2nd-writer", "Second"), TEAM[1]))
    assert result.codes() == {DelegationCode.UNUSABLE_AGENT_KEY}


def test_a_downstream_key_beginning_with_a_digit_is_still_compilable() -> None:
    """Only the upstream key is written into an expression.

    Refusing the downstream one too would be the compiler enforcing a rule it
    does not depend on, and locking an agent out of a team for no reason.
    """
    result = compiled(agents=(TEAM[0], agent(WRITER, "2nd-writer", "Second")))
    assert result.compiled


# ------------------------------------------------------- what is *not* refused ---


def test_a_delegation_between_other_agents_entirely_is_ignored() -> None:
    """A second team elsewhere in the project is not this compilation's business."""
    other = uuid.UUID("00000000-0000-0000-0000-0000000000d4")
    result = compiled(
        delegations=(delegation(STRANGER, other),),
        agents=(*TEAM, agent(STRANGER, "editor"), agent(other, "proofer")),
    )
    assert result.compiled, result.findings


@pytest.mark.parametrize("why", ["disabled", "archived"])
def test_a_delegation_that_cannot_hand_work_anywhere_does_not_make_this_ambiguous(
    why: str,
) -> None:
    """A fan-out that cannot happen is not a fan-out.

    A disabled grant permits nothing and an archived agent can take nothing,
    so counting either would refuse a delegation that is in fact the only one
    in force.
    """
    extra = delegation(RESEARCHER, STRANGER, enabled=why != "disabled")
    agents = TEAM if why == "archived" else (*TEAM, agent(STRANGER, "editor"))
    result = compiled(delegations=(extra,), agents=agents)
    assert result.compiled, result.findings


# ---------------------------------------------------------- the result itself ---


def test_a_refusal_never_carries_a_workflow_and_a_success_never_carries_findings() -> None:
    """The invariant the two constructors exist to enforce."""
    refused = compiled(agents=())
    assert refused.workflow is None and refused.findings
    accepted = compiled()
    assert accepted.workflow is not None and accepted.findings == ()


def test_every_finding_carries_a_pointer_a_detail_and_a_remedy() -> None:
    """A refusal a caller cannot act on is as useless here as in `validate`."""
    result = compiled(
        delegations=(delegation(RESEARCHER, STRANGER), delegation(WRITER, RESEARCHER)),
        agents=(*TEAM, agent(STRANGER, "editor", "Editor")),
    )
    assert result.findings
    for finding in result.findings:
        assert finding.pointer.startswith("/connections/")
        assert finding.detail.endswith(".")
        assert finding.remedy


@pytest.mark.parametrize("code", list(DelegationCode))
def test_every_delegation_code_has_a_test(code: DelegationCode) -> None:
    """The test that keeps the rest honest.

    A new member of `DelegationCode` with no case above fails here, which is
    the only way "every refusal is covered" stays true rather than being true
    on the day it was written.
    """
    import pathlib

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    assert f"DelegationCode.{code.name}" in source, f"{code.value} has no test"


# ------------------------------------------- compatibility with what exists ---


def _definition(graph: Any) -> Any:
    """The compiled graph, read by the production draft reader.

    `definition_for_execution` is the function `validate`, `publish` and the
    execution path all use. Going through it rather than building a
    `WorkflowDefinition` by hand is the point: it proves the compiler emits
    jsonb that reader understands, keys and all.
    """
    facts = {
        key: AgentFacts(agent_key=key, interaction_mode=AgentInteractionMode.MANAGED)
        for key in ("researcher", "writer")
    }
    draft = WorkflowDraft(id=uuid.uuid4(), workflow_id=uuid.uuid4(), version=1, graph=graph)
    return definition_for_execution(draft, facts, {}, approvers=2)


def test_the_compiled_graph_validates_and_is_publishable_by_this_release() -> None:
    """The existing validator, not a copy of its rules.

    Two things at once: the graph has no finding at all - not one entry step
    warning, not a dangling edge - and its step kinds are ones this release
    can freeze. A compiler that emitted `tool_call` would pass validation and
    then be refused at publish.
    """
    workflow = compiled().workflow
    assert workflow is not None
    report = validate(_definition(workflow.graph))
    assert report.findings == (), report.findings
    assert report.publishable
    definition = _definition(workflow.graph)
    assert {step.kind for step in definition.steps} <= PUBLISHABLE_KINDS
    assert {step.kind for step in definition.steps} == {StepKind.AGENT_TASK}


def test_advance_dispatches_the_upstream_step_first_and_only_it() -> None:
    """The existing orchestrator, on the compiled graph.

    The downstream step has an incoming edge, so a fresh execution plans one
    dispatch. This is the same assertion the hand-written two-step test makes
    against rows; here it is the pure function, which is where the sequencing
    actually lives.
    """
    workflow = compiled().workflow
    assert workflow is not None
    definition = _definition(workflow.graph)
    state = ExecutionState(
        status=ExecutionStatus.RUNNING,
        now=dt.datetime(2026, 11, 8, tzinfo=dt.UTC),
        input={"input": "look into fintech"},
    )
    decisions = advance(definition, state)
    dispatches = [d for d in decisions if isinstance(d, DispatchStep)]
    assert [d.step_key for d in dispatches] == ["researcher"]
    assert dispatches[0].instruction["input"] == {"input": "look into fintech"}


def test_the_downstream_step_receives_the_upstream_output_once_it_succeeds() -> None:
    """The handoff, decided by `advance` rather than by this module's string.

    The researcher has succeeded with a known output, so the writer becomes
    ready and its instruction must carry that output's text - which is the
    sentence M1 asks for, at the only layer where it can be asserted without
    a database.
    """
    workflow = compiled().workflow
    assert workflow is not None
    definition = _definition(workflow.graph)
    state = ExecutionState(
        status=ExecutionStatus.RUNNING,
        now=dt.datetime(2026, 11, 8, tzinfo=dt.UTC),
        input={"input": "look into fintech"},
        steps={
            "researcher": StepState(
                status=StepStatus.SUCCEEDED,
                attempt=1,
                output={"text": "Acme raised a round.", "stop_reason": "end_turn"},
            )
        },
    )
    dispatches = [d for d in advance(definition, state) if isinstance(d, DispatchStep)]
    assert [d.step_key for d in dispatches] == ["writer"]
    assert dispatches[0].instruction["input"] == {"input": "Acme raised a round."}

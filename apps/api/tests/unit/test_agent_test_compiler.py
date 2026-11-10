"""The agent-test compiler: one graph, three refusals, and no database.

Three kinds of test live here.

The **contract**: the field the sample input travels under, the expression
the step reads it with, and the fact that the step key is the agent key.
A published version freezes all three, and the last one is also how the
agent's answer finds its way back out - `_final_output` keys an execution's
output by step key, so `GET /executions/{id}` returns
`{"<agent_key>": {"text": …}}` precisely because the step is named after the
agent. Changing one of these assertions is not a refactor.

The **decision table**: `compile_agent_test` is pure and total, so every
refusal is four lines and no fixture.

The **agreements**, which are what keep the rest honest: the compiled graph
must satisfy the *existing* validator and the *existing* `advance` rather
than a shape only this module understands; this module's entry contract must
be the same one a compiled team's first step gets; and the modes it refuses
must be exactly the modes the orchestrator cannot run.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid
from typing import Any

import pytest

from agentworld.domain import slugs
from agentworld.domain.entities import WorkflowDraft
from agentworld.domain.enums import (
    AgentInteractionMode,
    ExecutionStatus,
    StepKind,
    StepStatus,
)
from agentworld.domain.expressions import MISSING, evaluate
from agentworld.domain.orchestration import (
    AgentFacts,
    AgentTestCode,
    AgentTestSubject,
    CompleteExecution,
    DispatchStep,
    ExecutionState,
    FailExecution,
    StepState,
    advance,
    compile_agent_test,
    delegation,
    validate,
)
from agentworld.domain.orchestration import agent_test as module
from agentworld.services.definitions import definition_for_execution
from agentworld.services.executions import PUBLISHABLE_KINDS

WRITER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
STRANGER = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
SAMPLE = "Summarise the fintech funding news."

#: The slug's discriminator for `WRITER`, as a literal rather than as a call
#: to the function that produces it. A test that recomputed it would pass for
#: any scheme, including the `agent_id.hex[:8]` this started as - which these
#: two ids are what disproved.
WRITER_DISCRIMINATOR = "f5745a3b"
STRANGER_DISCRIMINATOR = "9c0997ee"


def subject(
    key: str = "writer",
    *,
    agent_id: uuid.UUID = WRITER,
    name: str = "",
    mode: AgentInteractionMode = AgentInteractionMode.MANAGED,
    archived: bool = False,
) -> AgentTestSubject:
    return AgentTestSubject(
        agent_id=agent_id,
        agent_key=key,
        interaction_mode=mode,
        display_name=name,
        is_archived=archived,
    )


def compiled(**kwargs: Any) -> Any:
    """The compiled workflow, or an assertion failure naming the refusal."""
    compilation = compile_agent_test(subject(**kwargs))
    assert compilation.workflow is not None, sorted(c.value for c in compilation.codes())
    return compilation.workflow


# ----------------------------------------------------------------- contract ---


def test_the_compiled_graph_is_exactly_this() -> None:
    """The whole graph in one assertion, because it is the contract.

    Pinned as a literal rather than field by field: a draft is jsonb, and the
    keys are what `definition_for_execution` reads. A test that checked the
    steps and forgot the mapping would pass for a graph that hands the agent
    nothing.
    """
    assert compiled().graph == {
        "steps": [
            {
                "step_key": "writer",
                "kind": "agent_task",
                "agent_key": "writer",
                "input_mapping": {"input": "$.input.input"},
            }
        ],
        "edges": [],
    }


def test_the_step_key_is_the_agent_key_and_so_is_the_output_key() -> None:
    """Why the step key matters twice.

    It is frozen into `workflow_steps.step_key` at publish, and it is the key
    the execution's own `output` comes back under - so the API's answer to
    "what did the agent say" is addressed by the agent's key and not by a
    generated step name.
    """
    workflow = compiled()
    assert workflow.step_key == "writer"
    assert workflow.graph["steps"][0]["step_key"] == "writer"
    assert workflow.agent_id == WRITER


def test_the_entry_mapping_is_the_contract_a_compiled_team_also_uses() -> None:
    """One contract for what a first step receives, stated in two modules.

    `delegation.py` decides it for a team's first step and this module
    decides it for a test. They are the same decision, and a caller that runs
    either with `{"input": {"input": "..."}}` must get the same behaviour -
    so the agreement is asserted rather than described in a comment.
    """
    assert module.INPUT_FIELD == delegation.HANDOFF_FIELD
    assert module.ENTRY_EXPRESSION == delegation.ENTRY_EXPRESSION


def test_the_persisted_entry_contract_cannot_be_renamed() -> None:
    """The frozen expression and the live envelope, pinned as literals.

    This pair is not an implementation detail of this module.

    **A published version freezes the expression.** `publish` writes
    `input_mapping` into `workflow_steps`, and `_definition_of` builds the
    definition from the execution's own frozen rows rather than recompiling,
    so the `$.input.input` a version was published with is what it reads for
    as long as it exists.

    **An Agent Test harness is not republished after creation.**
    `AgentTestService` publishes only while `current_version_id` is NULL, and
    both `save_draft` and `archive` refuse a harness - so no path, for a
    person or for the service, hands an existing harness a newly compiled
    graph. An authored workflow has a remedy this does not: somebody can
    publish it again.

    **The envelope is supplied live.** `AgentTestService.run` passes
    `inputs={INPUT_FIELD: sample}` from *this* release's constant into a
    version frozen by whichever release first tested that agent.

    So renaming this pair does not fail loudly. `_mapped` omits a field whose
    expression does not resolve, `_prompt` then falls back to "Begin the task
    you have been assigned.", and every harness published before the rename
    keeps running - spending money, succeeding - with the sample silently
    discarded.

    Changing either constant therefore requires designing a supported way to
    republish existing harnesses first. Until that exists, this test is the
    mechanism, which is why it asserts the literal persisted strings rather
    than comparing the two constants with each other: a coordinated rename
    satisfies that comparison and breaks every published harness.
    """
    assert module.INPUT_FIELD == "input"
    assert module.ENTRY_EXPRESSION == "$.input.input"
    assert compiled().graph["steps"][0]["input_mapping"] == {"input": "$.input.input"}

    # The frozen expression as a literal, resolved against the scope
    # `advance` builds from the envelope the service sends. The two halves
    # are written by different releases, so this pairing - not either half -
    # is the contract.
    assert evaluate("$.input.input", {"input": {module.INPUT_FIELD: SAMPLE}, "steps": {}}) == SAMPLE
    # And what a rename would produce instead, which is why it is silent:
    # not an error, just nothing where the sample was.
    assert evaluate("$.input.input", {"input": {"renamed": SAMPLE}, "steps": {}}) is MISSING


def test_the_entry_expression_resolves_to_the_sample_input() -> None:
    """The expression evaluated by the real evaluator, against the real scope.

    `advance._scope` builds `{"input": …, "steps": …}`, so this is the shape
    the expression will actually meet. Asserting the string alone would pass
    for `$.input.sample`, which resolves to nothing and renders a prompt of
    "Begin the task you have been assigned."
    """
    scope = {"input": {"input": SAMPLE}, "steps": {}}
    assert evaluate(module.ENTRY_EXPRESSION, scope) == SAMPLE


def test_the_name_comes_from_the_display_name_and_the_slug_from_the_key() -> None:
    """Separate, deliberately: a slug is identity, a name is for reading."""
    workflow = compiled(key="writer", name="Brief Writer")
    assert workflow.name == "Test: Brief Writer"
    assert workflow.slug.startswith("test-writer-")
    assert "brief" not in workflow.slug


def test_the_name_falls_back_to_the_key_when_there_is_no_display_name() -> None:
    assert compiled(key="writer").name == "Test: writer"


def test_the_name_fits_the_api_name_limit() -> None:
    """200 characters, the limit the API's own `Name` applies.

    A display name that long is already legal, so the prefix cannot be the
    thing that makes the compiled workflow unsaveable.
    """
    workflow = compiled(name="N" * 400)
    assert len(workflow.name) <= 200
    assert workflow.name.startswith("Test: N")


@pytest.mark.parametrize(
    "key",
    [
        "w",
        "writer",
        "2nd-writer",
        "a" * slugs.MAX_LENGTH,
        "a" * 33 + "-b",
        "a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p-q-r-s-t-u-v-w",
    ],
)
def test_the_slug_stays_a_valid_slug_for_awkward_keys(key: str) -> None:
    """The cases that break string concatenation.

    A key at the full 48 characters overflows the budget; truncating one with
    hyphens in it can end on a hyphen, which `domain/slugs.py` rejects; and a
    key may begin with a digit, which a slug permits and an expression name
    does not - harmless here, because no expression names a lone step. There
    is deliberately no refusal code for an unusable key, so this is the test
    that has to hold instead.
    """
    workflow = compiled(key=key)
    assert slugs.is_valid(workflow.slug), workflow.slug
    assert len(workflow.slug) <= slugs.MAX_LENGTH


def test_a_key_that_folds_to_nothing_still_produces_a_slug() -> None:
    """`slugify` of emoji is the empty string, and the discriminator saves it."""
    workflow = compiled(key="🙂🙂")
    assert slugs.is_valid(workflow.slug), workflow.slug
    assert workflow.slug == f"test-{WRITER_DISCRIMINATOR}"


def test_the_slug_carries_the_agent_id_so_an_authored_workflow_cannot_block_it() -> None:
    """Why the slug is not simply `test-<key>`.

    `(project_id, slug)` is unique, so a person who had already authored a
    workflow called `test-writer` would otherwise break the first test of the
    agent `writer` with a conflict naming a workflow they would have to go
    and find. Two agents with the same key cannot exist in one project, but a
    hand-written workflow with the obvious name certainly can.

    The second half of this is what the first draft of the compiler failed:
    these two ids differ only in their last characters, so a discriminator
    taken from the front of the id was the same for both.
    """
    workflow = compiled(key="writer")
    assert workflow.slug == f"test-writer-{WRITER_DISCRIMINATOR}"
    stranger = compiled(key="writer", agent_id=STRANGER)
    assert stranger.slug == f"test-writer-{STRANGER_DISCRIMINATOR}"
    assert stranger.slug != workflow.slug


def test_the_description_says_where_the_workflow_came_from() -> None:
    """The one place a person who finds this row is told what it is."""
    assert "agent.test" in compiled().description


# ----------------------------------------------------------- decision table ---


def test_an_archived_agent_is_refused() -> None:
    compilation = compile_agent_test(subject(archived=True))
    assert not compilation.compiled
    assert compilation.codes() == {AgentTestCode.AGENT_ARCHIVED}
    assert compilation.findings[0].pointer == f"/agents/{WRITER}"


def test_an_observed_agent_is_refused() -> None:
    """Permanent, not a phase gate: an observed agent is never given work."""
    compilation = compile_agent_test(subject(mode=AgentInteractionMode.OBSERVED))
    assert compilation.codes() == {AgentTestCode.OBSERVED_AGENT}


def test_a_dispatchable_agent_is_refused() -> None:
    """A phase gate, and the reason says so rather than blaming the agent."""
    compilation = compile_agent_test(subject(mode=AgentInteractionMode.DISPATCHABLE))
    assert compilation.codes() == {AgentTestCode.DISPATCHABLE_AGENT}
    assert "not available yet" in compilation.findings[0].detail


def test_being_archived_is_reported_before_the_mode() -> None:
    """One reason at a time, and the one the person can act on first.

    An archived observed agent is refused for being archived: restoring it is
    the step that makes the second reason worth reading.
    """
    compilation = compile_agent_test(subject(mode=AgentInteractionMode.OBSERVED, archived=True))
    assert compilation.codes() == {AgentTestCode.AGENT_ARCHIVED}


def test_every_refusal_carries_a_remedy() -> None:
    """A refusal with nothing to do about it is a dead end."""
    for mode in (AgentInteractionMode.OBSERVED, AgentInteractionMode.DISPATCHABLE):
        for finding in compile_agent_test(subject(mode=mode)).findings:
            assert finding.remedy
    for finding in compile_agent_test(subject(archived=True)).findings:
        assert finding.remedy


def test_the_compiler_cannot_see_a_credential_or_a_model_profile() -> None:
    """C19, enforced by the type rather than by remembering.

    A managed agent whose model profile carries no credential must compile,
    publish and dispatch, and fail only where a runtime would be selected -
    the roadmap asserts that path must not rot before Phase 7 needs it. The
    compiler cannot refuse what it cannot be told, so the guarantee is that
    `AgentTestSubject` has no field for either.
    """
    fields = set(AgentTestSubject.__dataclass_fields__)
    assert fields == {
        "agent_id",
        "agent_key",
        "interaction_mode",
        "display_name",
        "is_archived",
    }
    assert not {name for name in fields if "credential" in name or "profile" in name}


def test_every_refusal_code_has_a_test() -> None:
    """The guard that stops a code being added without a case for it."""
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    for code in AgentTestCode:
        assert f"AgentTestCode.{code.name}" in source, f"{code.value} has no test"


# ------------------------------------------- compatibility with what exists ---


def _definition(graph: Any, *, mode: AgentInteractionMode = AgentInteractionMode.MANAGED) -> Any:
    """The compiled graph, read by the production draft reader.

    `definition_for_execution` is the function `validate`, `publish` and the
    execution path all use. Going through it rather than building a
    `WorkflowDefinition` by hand proves the compiler emits jsonb that reader
    understands, keys and all.
    """
    facts = {"writer": AgentFacts(agent_key="writer", interaction_mode=mode)}
    draft = WorkflowDraft(id=uuid.uuid4(), workflow_id=uuid.uuid4(), version=1, graph=graph)
    return definition_for_execution(draft, facts, {}, approvers=2)


def _running(**kwargs: Any) -> ExecutionState:
    return ExecutionState(
        status=ExecutionStatus.RUNNING,
        now=dt.datetime(2026, 11, 9, tzinfo=dt.UTC),
        input={"input": SAMPLE},
        **kwargs,
    )


def test_the_compiled_graph_validates_with_no_findings_at_all() -> None:
    """The existing validator, not a copy of its rules.

    Not one finding: no unreachable step for the missing edges, and no
    multiple-entry warning either. `validate` says a workflow of one step and
    no edges is complete rather than disconnected, and this is the test that
    holds that sentence to it - if it ever stops being true, a test run would
    start failing at publish with a finding about a graph nobody drew.
    """
    report = validate(_definition(compiled().graph))
    assert report.findings == (), report.findings
    assert report.publishable


def test_the_one_step_is_a_kind_this_release_can_publish() -> None:
    """`tool_call` would validate and then be refused at publish."""
    definition = _definition(compiled().graph)
    assert {step.kind for step in definition.steps} == {StepKind.AGENT_TASK}
    assert {step.kind for step in definition.steps} <= PUBLISHABLE_KINDS


def test_advance_dispatches_the_one_step_carrying_the_sample_input() -> None:
    """The existing orchestrator, on the compiled graph.

    One dispatch, and its instruction carries the sample text under the field
    `_prompt` renders - which is the whole of what a test run is.
    """
    decisions = advance(_definition(compiled().graph), _running())
    dispatches = [d for d in decisions if isinstance(d, DispatchStep)]
    assert [d.step_key for d in dispatches] == ["writer"]
    assert dispatches[0].instruction["input"] == {"input": SAMPLE}
    assert dispatches[0].instruction["agent_key"] == "writer"


def test_the_finished_execution_output_is_keyed_by_the_agent_key() -> None:
    """How the agent's answer reaches the user, decided by `advance`.

    The step is a leaf, so `_final_output` puts its output on the execution
    and the reducer folds it onto `workflow_executions.output`, which
    `ExecutionView` already exposes. That is the whole of decision 6 - no
    second result API - and this is where it can be asserted without a
    database.
    """
    state = _running(
        steps={
            "writer": StepState(
                status=StepStatus.SUCCEEDED,
                attempt=0,
                output={"text": "Acme raised a round.", "stop_reason": "end_turn"},
            )
        }
    )
    decisions = advance(_definition(compiled().graph), state)
    completions = [d for d in decisions if isinstance(d, CompleteExecution)]
    assert len(completions) == 1
    assert completions[0].output == {
        "writer": {"text": "Acme raised a round.", "stop_reason": "end_turn"}
    }


@pytest.mark.parametrize("mode", list(AgentInteractionMode))
def test_the_compiler_refuses_exactly_the_modes_the_orchestrator_cannot_run(
    mode: AgentInteractionMode,
) -> None:
    """The duplication, checked rather than hoped.

    This module judges the interaction mode although `advance` judges it too,
    because a refusal is a better answer than a published workflow and a
    failed execution. The risk is that the two drift - most likely when Phase
    7 makes `dispatchable` executable and nobody remembers this module
    exists. So: compile, and run the real `advance` over the graph the
    compiler would have produced. Whatever it refuses, `advance` must refuse;
    whatever it compiles, `advance` must dispatch.
    """
    compilation = compile_agent_test(subject(mode=mode))
    graph = module.compile_agent_test(subject(mode=AgentInteractionMode.MANAGED)).workflow
    assert graph is not None
    decisions = advance(_definition(graph.graph, mode=mode), _running())

    orchestrator_refuses = any(isinstance(d, FailExecution) for d in decisions)
    assert compilation.compiled is not orchestrator_refuses, (
        f"{mode.value}: compiler compiled={compilation.compiled}, "
        f"orchestrator refused={orchestrator_refuses}"
    )
    if compilation.compiled:
        assert [d.step_key for d in decisions if isinstance(d, DispatchStep)] == ["writer"]

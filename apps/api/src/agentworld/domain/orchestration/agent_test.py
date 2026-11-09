"""Compiling an agent's test harness into a one-step workflow.

`03-API-AND-FRONTEND.md` §3 is the authority:

> `POST /…/test` runs the agent once against a sample input without a
> workflow: the single most useful affordance when building one, and it is a
> **real execution producing real events, not a preview**.

"Without a workflow" cannot mean without a `workflow_version`:
`workflow_executions.workflow_id` and `workflow_version_id` are both NOT
NULL, and a preview is exactly what the sentence rules out. It means without
the user authoring one. So this module writes the graph they would have drawn
if they had bothered - one `agent_task` step reading the execution's input -
and hands it to the ordinary draft, the ordinary `validate`, the ordinary
publish and the ordinary worker. There is no test execution path, no test
dispatch kind, and nothing here that a hand-written single-step workflow does
not also do.

**One step and no edges is a complete graph, not a disconnected one.** That
is `validate`'s own rule, stated where it is implemented: "A workflow of one
step and no edges is complete, not disconnected." So there is no entry edge
here. One would also validate - `from_step IS NULL` is a legal declared
entry - but it would be a `workflow_edges` row that says nothing about
sequencing, frozen into every published version, and the only way to keep
the orchestrator's behaviour honest is to emit the graph a person would have
drawn rather than one that looks defensive.

**What this module judges, and what it refuses to judge.** It judges whether
*this agent* can be given work at all, because that is the question the
affordance exists to answer and the only one a single agent raises. It judges
nothing about credentials or model profiles: a managed agent whose profile
carries no `credential_id` must still compile, publish and dispatch, and fail
where a runtime would be selected (C19). `AgentTestSubject` therefore cannot
express a credential, which is a stronger guarantee than remembering not to
look at one.

**The interaction-mode refusals duplicate a judgement `advance` also makes,
deliberately, and the duplication is checked.** `_not_executable_yet` refuses
an `observed` agent permanently and a `dispatchable` one as a phase gate, and
`validate` has a finding for the first but not the second - so without the
refusals here, testing a dispatchable agent would publish a workflow and
produce a failed execution with no provider call. Refusing earlier is the
better answer for an interactive affordance, and the risk it creates is that
the two disagree when the phase gate lifts. So a unit test runs the real
`advance` over the compiled graph for every mode and asserts that this module
refuses exactly the modes the orchestrator cannot run. When Phase 7 makes
`dispatchable` executable, that test fails here rather than leaving a
refusal nobody can explain.

Pure: no database, no clock, no exceptions. Every refusal is a finding.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from agentworld.domain.enums import AgentInteractionMode, StepKind
from agentworld.domain.slugs import MAX_LENGTH as SLUG_LENGTH
from agentworld.domain.slugs import slugify

# ------------------------------------------------------------- the contract ---

#: The field the sample input travels under, and what the step reads it
#: with. The same pair a compiled team's first step gets, because "what the
#: first step of an execution receives" is one contract and not one per
#: compiler - `tests/unit/test_agent_test_compiler.py` asserts the two agree
#: rather than leaving it to a comment.
INPUT_FIELD: Final = "input"
ENTRY_EXPRESSION: Final = "$.input.input"

#: `agent_key` is the step key: unique per project
#: (`agents_project_key_key`), `citext`, validated as a slug at creation and
#: absent from `AgentUpdate`, so it cannot change under a published version.
#: It is also the key the execution's `output` comes back under, since
#: `_final_output` keys leaf outputs by step key - which is how `GET
#: /executions/{id}` ends up carrying what the agent actually said.
STEP_KIND: Final = StepKind.AGENT_TASK

#: A test workflow's slug is `test-<agent key>-<discriminator>`.
#:
#: The discriminator is not decoration. Identity lives in
#: `workflows.test_for_agent_id`, but `(project_id, slug)` is still unique,
#: so a slug of `test-writer` would let somebody who had already authored a
#: workflow called `test-writer` break the first test of the agent `writer` -
#: a 409 naming a workflow they would have to go and find. It derives from
#: the agent id, which is already in the row, so it reveals nothing new; it
#: only makes a collision somebody's deliberate act rather than an accident.
#:
#: It is a digest of the whole id rather than a slice of it, which was
#: measured rather than reasoned about: the first write of this took
#: `agent_id.hex[:8]` and two agents in the test suite collided, because ids
#: written by hand share their leading characters. `gen_random_uuid()` is v4
#: and would have hidden that in production - until a time-ordered id, where
#: the leading half is a timestamp, made every agent created in the same
#: minute share a prefix. A digest does not depend on which end of an id
#: happens to be random.
SLUG_PREFIX: Final = "test-"
_DISCRIMINATOR: Final = 8
#: What is left of the 48-character slug budget for the key itself.
_KEY_BUDGET: Final = SLUG_LENGTH - len(SLUG_PREFIX) - 1 - _DISCRIMINATOR

#: `Name` in the API schemas is 200 characters, and this one is a prefix
#: plus a display name the API itself accepted at that length.
_NAME_PREFIX: Final = "Test: "
_NAME_BUDGET: Final = 200 - len(_NAME_PREFIX)


# ------------------------------------------------------------- the findings ---


class AgentTestCode(StrEnum):
    """Every reason this release refuses to compile an agent's test.

    A closed set, like `validate.Code` and `DelegationCode`: a refusal the UI
    cannot map onto something to do about it is a dead end for the person
    holding it.

    There is deliberately no code for an unusable agent key. A delegation
    needs one because `$.steps.<key>` must be writable as an expression and a
    slug may begin with a digit; a lone step is named by no expression, and
    the slug is normalised by `slugify` rather than composed by hand, so
    there is no key this module cannot compile. A refusal nobody can reach
    would read like a protection and be none.
    """

    AGENT_ARCHIVED = "agent_archived"
    OBSERVED_AGENT = "observed_agent"
    DISPATCHABLE_AGENT = "dispatchable_agent"


@dataclass(frozen=True, slots=True)
class AgentTestFinding:
    """One reason, pointing at the agent responsible for it."""

    code: AgentTestCode
    pointer: str
    detail: str
    remedy: str = ""


# --------------------------------------------------------------- the input ---


@dataclass(frozen=True, slots=True)
class AgentTestSubject:
    """The agent to test, in the only respects compiling needs.

    Not `AgentFacts`, which is keyed by `agent_key` and carries what
    *validation* needs; and not the `Agent` entity, because what this module
    may read is itself part of the contract. There is no field for a model
    profile or a credential, so the compiler cannot refuse a managed agent
    for lacking either - that failure belongs to the runtime, where it is
    `misconfigured` and visible.
    """

    agent_id: UUID
    agent_key: str
    interaction_mode: AgentInteractionMode
    display_name: str = ""
    is_archived: bool = False

    @property
    def label(self) -> str:
        """What a person calls this agent. The key is the fallback, never
        the identifier that leaks into a graph."""
        return (self.display_name or self.agent_key).strip()[:_NAME_BUDGET].strip()


# -------------------------------------------------------------- the output ---


@dataclass(frozen=True, slots=True)
class CompiledTestWorkflow:
    """What to create. A draft's worth of fields, and the column's value.

    `agent_id` is here so the writer sets `workflows.test_for_agent_id` from
    the compilation rather than from a second lookup: the row that marks a
    workflow as a harness and the graph that makes it one come from the same
    decision.
    """

    name: str
    slug: str
    description: str
    graph: Mapping[str, Any]
    step_key: str
    agent_id: UUID


@dataclass(frozen=True, slots=True)
class AgentTestCompilation:
    """Either a workflow to create or the reasons there is none.

    Never both, and never neither: the two constructors are the only way to
    build one.
    """

    findings: tuple[AgentTestFinding, ...] = ()
    workflow: CompiledTestWorkflow | None = None

    @classmethod
    def refused(cls, findings: Sequence[AgentTestFinding]) -> AgentTestCompilation:
        assert findings, "a refusal with no reason is not a refusal"
        return cls(findings=tuple(findings))

    @classmethod
    def ready(cls, workflow: CompiledTestWorkflow) -> AgentTestCompilation:
        return cls(workflow=workflow)

    @property
    def compiled(self) -> bool:
        return self.workflow is not None

    def codes(self) -> frozenset[AgentTestCode]:
        return frozenset(finding.code for finding in self.findings)


# ------------------------------------------------------------ the compiler ---


def compile_agent_test(subject: AgentTestSubject) -> AgentTestCompilation:
    """Compile one agent's test harness, or say why this release will not."""
    if refusals := list(_eligible(subject)):
        return AgentTestCompilation.refused(refusals)
    return AgentTestCompilation.ready(_one_step(subject))


# -------------------------------------------------------------- the reasons ---


def _eligible(subject: AgentTestSubject) -> Iterator[AgentTestFinding]:
    """Can this agent be given work at all?"""
    at = _pointer(subject.agent_id)

    if subject.is_archived:
        yield AgentTestFinding(
            AgentTestCode.AGENT_ARCHIVED,
            at,
            f"'{subject.agent_key}' is archived, so it is not an agent this project still runs.",
            "Restore the agent, then test it.",
        )
        return

    if subject.interaction_mode is AgentInteractionMode.OBSERVED:
        yield AgentTestFinding(
            AgentTestCode.OBSERVED_AGENT,
            at,
            f"'{subject.agent_key}' only reports its own activity and is never given "
            "work, so there is nothing here to run.",
            "Watch its runs instead, or change it to a managed agent.",
        )
        return

    if subject.interaction_mode is AgentInteractionMode.DISPATCHABLE:
        yield AgentTestFinding(
            AgentTestCode.DISPATCHABLE_AGENT,
            at,
            f"'{subject.agent_key}' runs somewhere else, and pushing work to an "
            "external agent is not available yet.",
            "Test it from where it runs, for now.",
        )


# ----------------------------------------------------------------- the graph ---


def _one_step(subject: AgentTestSubject) -> CompiledTestWorkflow:
    """The graph. One `agent_task` step, one mapping, no edges."""
    key = subject.agent_key
    return CompiledTestWorkflow(
        name=f"{_NAME_PREFIX}{subject.label}",
        slug=_slug(subject),
        description=(
            f"Runs '{key}' once against a sample input. Created by agent.test, not authored."
        ),
        graph={
            "steps": [
                {
                    "step_key": key,
                    "kind": STEP_KIND.value,
                    "agent_key": key,
                    "input_mapping": {INPUT_FIELD: ENTRY_EXPRESSION},
                }
            ],
            "edges": [],
        },
        step_key=key,
        agent_id=subject.agent_id,
    )


def _slug(subject: AgentTestSubject) -> str:
    """`test-<key>-<id>`, normalised rather than assembled.

    The key is truncated to what the 48-character budget leaves, which can
    end it on a hyphen, and a key of only emoji folds to nothing at all.
    Running the whole composition back through `slugify` is what makes those
    cases a shorter slug instead of an invalid one - the alternative is three
    string rules here that have to stay in step with the ones in
    `domain/slugs.py`.
    """
    stem = slugify(subject.agent_key)[:_KEY_BUDGET]
    return slugify(f"{SLUG_PREFIX}{stem}-{_discriminator(subject.agent_id)}")


def _discriminator(agent_id: UUID) -> str:
    """Eight hex characters derived from the whole id.

    Not a secret and not a checksum - a short, stable, uniformly distributed
    name for one agent, so that the slug of its test harness cannot collide
    with another agent's by sharing part of an id.
    """
    return hashlib.blake2b(agent_id.bytes, digest_size=_DISCRIMINATOR // 2).hexdigest()


def _pointer(agent_id: UUID) -> str:
    return f"/agents/{agent_id}"

"""What a workflow is, to anything that reasons about it.

Plain frozen dataclasses, assembled by the service layer from rows and handed
to pure functions. The repository layer has its own SQLAlchemy models and the
API its own pydantic schemas; this is the third definition of "workflow", and
that is the price of `validate` and `advance` being testable with no database
and no network.

One shape is worth explaining. `AgentFacts` carries `granted_tools` as a set
of names rather than a reference to the connections that grant them. The
question validation asks is "can this agent use this tool?", and answering it
from a precomputed set keeps the pure function free of lookups it could not
perform. Assembling that set is the service's job, and it is where tenant
scoping is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from agentworld.domain.enums import AgentInteractionMode, SideEffect, StepKind


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """One node of the graph, as the author configured it."""

    step_key: str
    kind: StepKind
    #: `agent_task` names the agent that does the work.
    agent_key: str | None = None
    #: `tool_call` names the tool, and `agent_task` may name one it is
    #: expected to reach for.
    tool_name: str | None = None
    #: An expression that must be true for this step to run at all.
    condition: str | None = None
    #: Field name -> expression, evaluated against prior outputs.
    input_mapping: Mapping[str, str] = field(default_factory=dict)
    #: Set by the author, or implied by the tool's side effect.
    requires_approval: bool = False
    #: The approval may not be decided by whoever the execution is
    #: attributed to - the four-eyes rule, which can deadlock.
    approval_requires_different_user: bool = False


@dataclass(frozen=True, slots=True)
class EdgeDefinition:
    """A directed edge. `from_step` is None for an entry edge."""

    #: Position in the author's list, so a finding can point at `/edges/4`.
    index: int
    to_step: str
    from_step: str | None = None
    condition: str | None = None


@dataclass(frozen=True, slots=True)
class AgentFacts:
    """What validation needs to know about an agent it cannot look up."""

    agent_key: str
    interaction_mode: AgentInteractionMode
    display_name: str = ""
    #: Tool names this agent can actually reach, from its enabled
    #: connections. Precomputed by the service, which is the layer that can
    #: apply tenant scoping.
    granted_tools: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """A draft or a frozen version, in the form the pure functions want."""

    slug: str
    steps: tuple[StepDefinition, ...] = ()
    edges: tuple[EdgeDefinition, ...] = ()
    agents: Mapping[str, AgentFacts] = field(default_factory=dict)
    #: Side effect per tool name, for the "consequential and ungated" check.
    tool_effects: Mapping[str, SideEffect] = field(default_factory=dict)
    #: How many people in this workspace could decide an approval. Used only
    #: for `approval_unsatisfiable`, and supplied rather than counted here
    #: because counting needs the database.
    eligible_approvers: int = 0

    def step(self, step_key: str) -> StepDefinition | None:
        return next((s for s in self.steps if s.step_key == step_key), None)

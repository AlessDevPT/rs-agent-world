"""Deciding what a workflow should do next, and whether it could run at all.

`validate` answers the second question, `advance` the first, and `definition`
holds the shapes both of them read. `state` is what `advance` is told about an
execution and `decisions` is what it may answer; neither has a field that
could only be filled by a query, which is what keeps both functions pure.
"""

from agentworld.domain.orchestration.advance import advance, dispatchable_effects
from agentworld.domain.orchestration.decisions import (
    CompleteExecution,
    CompleteStep,
    Decision,
    DispatchStep,
    FailExecution,
    SkipStep,
)
from agentworld.domain.orchestration.definition import (
    AgentFacts,
    EdgeDefinition,
    StepDefinition,
    WorkflowDefinition,
)
from agentworld.domain.orchestration.state import ExecutionState, StepState
from agentworld.domain.orchestration.validate import (
    Code,
    Finding,
    Severity,
    ValidationReport,
    validate,
)

__all__ = [
    "AgentFacts",
    "Code",
    "CompleteExecution",
    "CompleteStep",
    "Decision",
    "DispatchStep",
    "EdgeDefinition",
    "ExecutionState",
    "FailExecution",
    "Finding",
    "Severity",
    "SkipStep",
    "StepDefinition",
    "StepState",
    "ValidationReport",
    "WorkflowDefinition",
    "advance",
    "dispatchable_effects",
    "validate",
]

"""Deciding what a workflow should do next, and whether it could run at all.

`validate` answers the second question, `advance` the first, and `definition`
holds the shapes both of them read. `state` is what `advance` is told about an
execution and `decisions` is what it may answer; neither has a field that
could only be filled by a query, which is what keeps both functions pure.

`delegation` is a third kind of question - what graph does a delegate
connection imply? - and it answers it by producing a graph the other two
already understand, rather than a shape of its own.
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
from agentworld.domain.orchestration.delegation import (
    ENTRY_EXPRESSION,
    HANDOFF_FIELD,
    AgentIdentity,
    Compilation,
    CompiledWorkflow,
    DelegationCode,
    DelegationFacts,
    DelegationFinding,
    compile_delegation,
    handoff_expression,
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
    "ENTRY_EXPRESSION",
    "HANDOFF_FIELD",
    "AgentFacts",
    "AgentIdentity",
    "Code",
    "Compilation",
    "CompiledWorkflow",
    "CompleteExecution",
    "CompleteStep",
    "Decision",
    "DelegationCode",
    "DelegationFacts",
    "DelegationFinding",
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
    "compile_delegation",
    "dispatchable_effects",
    "handoff_expression",
    "validate",
]

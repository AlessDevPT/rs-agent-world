"""Deciding what a workflow should do next, and whether it could run at all.

`validate` answers the second question, `advance` the first, and `definition`
holds the shapes both of them read. `state` is what `advance` is told about an
execution and `decisions` is what it may answer; neither has a field that
could only be filled by a query, which is what keeps both functions pure.

`delegation` is a third kind of question - what graph does a delegate
connection imply? - and it answers it by producing a graph the other two
already understand, rather than a shape of its own. `agent_test` is the same
kind of question about one agent, and answers it the same way.

Both compilers decide what a first step receives, and each states that
contract in its own module. The two values are identical and a unit test
asserts they stay identical, which is why only `delegation`'s pair is
re-exported here: two names called `ENTRY_EXPRESSION` in one namespace would
have to be renamed to coexist, and a reader would then have to work out which
one a service meant.
"""

from agentworld.domain.orchestration.advance import advance, dispatchable_effects
from agentworld.domain.orchestration.agent_test import (
    AgentTestCode,
    AgentTestCompilation,
    AgentTestFinding,
    AgentTestSubject,
    CompiledTestWorkflow,
    compile_agent_test,
)
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
    "AgentTestCode",
    "AgentTestCompilation",
    "AgentTestFinding",
    "AgentTestSubject",
    "Code",
    "Compilation",
    "CompiledTestWorkflow",
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
    "compile_agent_test",
    "compile_delegation",
    "dispatchable_effects",
    "handoff_expression",
    "validate",
]

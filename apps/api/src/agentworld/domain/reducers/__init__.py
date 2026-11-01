"""Pure reducers: an event in, the state changes it implies out.

Nothing here writes anything. `mutations` is the vocabulary a reducer may ask
for, and what it deliberately lacks - any way to cause work - is what makes
the protocol's seventh promise structural rather than a convention.
"""

from agentworld.domain.reducers.fold import fold
from agentworld.domain.reducers.mutations import (
    AgentRef,
    AgentStatusRef,
    Append,
    ExecutionRef,
    Increment,
    Mutation,
    MutationTarget,
    Patch,
    RunRef,
    RunStepRef,
    Selector,
    StepRef,
    TaskRef,
)

__all__ = [
    "AgentRef",
    "AgentStatusRef",
    "Append",
    "ExecutionRef",
    "Increment",
    "Mutation",
    "MutationTarget",
    "Patch",
    "RunRef",
    "RunStepRef",
    "Selector",
    "StepRef",
    "TaskRef",
    "fold",
]

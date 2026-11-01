"""A small expression language for workflow conditions and input mappings.

Pure, bounded, and unable to reach anything the orchestrator did not put in
scope. `parser` has the grammar and why it is this joyless; `evaluate` has
the semantics, including what `missing` means and why truthiness is narrow.
"""

from agentworld.domain.expressions.evaluate import (
    MISSING,
    Value,
    evaluate,
    evaluate_condition,
    truthy,
)
from agentworld.domain.expressions.parser import (
    FUNCTIONS,
    MAX_DEPTH,
    MAX_SOURCE_LENGTH,
    MAX_TOKENS,
    BinaryOp,
    Call,
    Constant,
    Node,
    Not,
    Reference,
    parse,
)

__all__ = [
    "FUNCTIONS",
    "MAX_DEPTH",
    "MAX_SOURCE_LENGTH",
    "MAX_TOKENS",
    "MISSING",
    "BinaryOp",
    "Call",
    "Constant",
    "Node",
    "Not",
    "Reference",
    "Value",
    "evaluate",
    "evaluate_condition",
    "parse",
    "truthy",
]

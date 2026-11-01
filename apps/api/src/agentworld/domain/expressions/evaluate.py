"""Evaluating a parsed expression against a scope.

Pure, total, and incapable of reaching anything the caller did not pass in.
The scope is a plain mapping of plain data - what a step produced, what the
execution was given - and a `Reference` walks it with dictionary and list
indexing. There is no `getattr` anywhere in this module, which is what makes
the security property structural rather than careful.

**Missing is a value, not an error.** `$.steps.research.output.count` on an
execution where `research` has not run yet is `MISSING`, and the comparisons
below treat it as equal to nothing and less than nothing. The alternative -
raising - would mean every condition on a parallel branch had to be written
defensively, and the defensive version of "did we find enough competitors?"
is where the real bugs would live. `exists()` is how you ask the question
deliberately.

**Truthiness is narrow.** Only `true` is true. An empty string, zero and an
empty list are *not* false - they are not booleans at all, and a condition
that depends on which of those a provider returned is a condition nobody can
review. `length($.x) > 0` says what was meant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from agentworld.domain.errors import ValidationFailedError
from agentworld.domain.expressions.parser import (
    BinaryOp,
    Call,
    Constant,
    Node,
    Not,
    Reference,
    invalid,
    parse,
)


class _Missing:
    """The value of a reference that names nothing.

    A singleton with a `repr` worth reading, because it turns up in error
    messages when a condition that should have been guarded by `exists` was
    not.
    """

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<missing>"


MISSING: Final = _Missing()

Value = str | float | bool | None | Mapping[str, Any] | Sequence[Any] | _Missing


def evaluate(source: str | Node, scope: Mapping[str, Any]) -> Value:
    """Evaluate `source` against `scope`.

    Accepts a parsed node as well as text, so a workflow version that was
    validated at publish time does not pay to re-parse every condition on
    every edge of every execution.
    """
    node = parse(source) if isinstance(source, str) else source
    return _eval(node, scope)


def truthy(value: Value) -> bool:
    """Whether a condition passes. Only `True` does.

    Deliberately not Python's truthiness. A step that runs or does not run
    depending on whether a provider returned `""` or `0` is a step whose
    behaviour nobody can predict from reading the workflow.
    """
    return value is True


def evaluate_condition(source: str | Node, scope: Mapping[str, Any]) -> bool:
    """Evaluate and reduce to the yes/no an edge needs."""
    return truthy(evaluate(source, scope))


# --------------------------------------------------------------- internals ---


def _eval(node: Node, scope: Mapping[str, Any]) -> Value:
    if isinstance(node, Constant):
        return node.value
    if isinstance(node, Reference):
        return _resolve(node, scope)
    if isinstance(node, Not):
        return not truthy(_eval(node.operand, scope))
    if isinstance(node, Call):
        return _call(node, scope)
    return _binary(node, scope)


def _resolve(node: Reference, scope: Mapping[str, Any]) -> Value:
    """Walk the path. Mappings by key, sequences by index, nothing else.

    A string is a `Sequence`, so it would otherwise be indexable - which
    would let `$.steps.research.output.summary[0]` return a character and
    make a typo look like it worked. Strings are terminal here.
    """
    current: Any = scope
    for segment in node.path:
        if isinstance(segment, int):
            if isinstance(current, str) or not isinstance(current, Sequence):
                return MISSING
            if not -len(current) <= segment < len(current):
                return MISSING
            current = current[segment]
        else:
            if not isinstance(current, Mapping):
                return MISSING
            if segment not in current:
                return MISSING
            current = current[segment]
    return current  # type: ignore[no-any-return]


def _call(node: Call, scope: Mapping[str, Any]) -> Value:
    if node.name == "exists":
        # The one place MISSING is visible to an author. `exists` deliberately
        # takes the whole expression rather than only a reference, so
        # `exists($.a) and exists($.b)` reads the way it is meant.
        return _eval(node.argument, scope) is not MISSING

    value = _eval(node.argument, scope)
    if value is MISSING:
        # Not zero. The length of something that is not there is not a
        # number, and answering 0 would make `length($.typo) == 0` true.
        return MISSING
    if isinstance(value, str | Mapping | Sequence):
        return float(len(value))
    raise invalid(
        f"length() needs a string, list or object, not {type(value).__name__}",
        function="length",
    )


def _binary(node: BinaryOp, scope: Mapping[str, Any]) -> Value:
    operator = node.operator

    # Short-circuit, so `exists($.x) and $.x > 3` is safe to write and reads
    # the way every other language has taught people it reads.
    if operator == "and":
        return truthy(_eval(node.left, scope)) and truthy(_eval(node.right, scope))
    if operator == "or":
        return truthy(_eval(node.left, scope)) or truthy(_eval(node.right, scope))

    left = _eval(node.left, scope)
    right = _eval(node.right, scope)

    if operator == "==":
        return _equal(left, right)
    if operator == "!=":
        return not _equal(left, right)
    if operator == "in":
        return _contains(right, left)
    return _ordered(operator, left, right)


def _equal(left: Value, right: Value) -> bool:
    """Equality, with two narrowings.

    Missing equals nothing, including itself: two references that both name
    nothing are not thereby "the same". And `True == 1.0` is false here,
    though Python says otherwise - a workflow where a boolean matched a
    number would be a workflow whose author was surprised.
    """
    if left is MISSING or right is MISSING:
        return False
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return bool(left == right)


def _contains(haystack: Value, needle: Value) -> bool:
    if haystack is MISSING or needle is MISSING:
        return False
    if isinstance(haystack, str):
        return isinstance(needle, str) and needle in haystack
    if isinstance(haystack, Mapping):
        return isinstance(needle, str) and needle in haystack
    if isinstance(haystack, Sequence):
        return any(_equal(needle, entry) for entry in haystack)
    return False


def _ordered(operator: str, left: Value, right: Value) -> bool:
    """`<`, `<=`, `>`, `>=` - numbers against numbers, strings against strings.

    Anything else is false rather than an error. An ordering between a number
    and a missing value has no answer, and a condition that raises at three
    in the morning because a step produced nothing is worse than one that
    simply does not fire.
    """
    comparable = (
        isinstance(left, float | int)
        and isinstance(right, float | int)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    ) or (isinstance(left, str) and isinstance(right, str))
    if not comparable:
        return False

    if operator == "<":
        return bool(left < right)  # type: ignore[operator]
    if operator == "<=":
        return bool(left <= right)  # type: ignore[operator]
    if operator == ">":
        return bool(left > right)  # type: ignore[operator]
    return bool(left >= right)  # type: ignore[operator]


__all__ = [
    "MISSING",
    "ValidationFailedError",
    "Value",
    "evaluate",
    "evaluate_condition",
    "truthy",
]

"""The expression language: what it does, and what it refuses to be.

The adversarial section is the important one. This language exists in a
multi-tenant product and its inputs are written by whoever can edit a
workflow, so the claim being tested is not "it computes conditions correctly"
but "there is nothing in here to escape from".
"""

from __future__ import annotations

import contextlib
import random
import string
from typing import Any

import pytest

from agentworld.domain.errors import ValidationFailedError
from agentworld.domain.expressions import (
    MAX_DEPTH,
    MAX_SOURCE_LENGTH,
    MAX_TOKENS,
    MISSING,
    evaluate,
    evaluate_condition,
    parse,
)

SCOPE: dict[str, Any] = {
    "input": {"sector": "fintech", "limit": 3},
    "steps": {
        "research": {
            "output": {
                "competitors": [
                    {"name": "Acme", "score": 9.5},
                    {"name": "Globex", "score": 4.0},
                ],
                "summary": "6 competitors, 14 sources",
                "count": 6,
                "complete": True,
            }
        }
    },
}


def value(source: str) -> Any:
    return evaluate(source, SCOPE)


# -------------------------------------------------------------- the basics ---


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("$.input.sector", "fintech"),
        ("$.steps.research.output.count", 6),
        ("$.steps.research.output.competitors[0].name", "Acme"),
        ("$.steps.research.output.competitors[-1].name", "Globex"),
        ("'a literal'", "a literal"),
        ("42", 42),
        ("-7", -7),
        ("1.5", 1.5),
        ("true", True),
        ("false", False),
        ("null", None),
    ],
)
def test_references_and_literals(source: str, expected: Any) -> None:
    assert value(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("$.steps.research.output.count > $.input.limit", True),
        ("$.steps.research.output.count < $.input.limit", False),
        ("$.input.sector == 'fintech'", True),
        ("$.input.sector != 'retail'", True),
        ("$.steps.research.output.complete == true", True),
        ("'Acme' in $.steps.research.output.summary", False),
        ("'competitors' in $.steps.research.output.summary", True),
        ("'count' in $.steps.research.output", True),
        ("$.steps.research.output.count > 3 and $.input.sector == 'fintech'", True),
        ("$.steps.research.output.count > 99 or $.input.sector == 'fintech'", True),
        ("not ($.steps.research.output.count > 99)", True),
        ("length($.steps.research.output.competitors) == 2", True),
        ("length($.input.sector) == 7", True),
        ("exists($.steps.research.output.count)", True),
        ("exists($.steps.writing.output.count)", False),
    ],
)
def test_operators(source: str, expected: Any) -> None:
    assert value(source) == expected


def test_parentheses_group() -> None:
    assert value("($.input.limit > 1 or false) and true") is True


def test_a_parsed_expression_can_be_evaluated_without_reparsing() -> None:
    """A published version pays the parse once, not per edge per execution."""
    node = parse("$.input.limit > 1")
    assert evaluate(node, SCOPE) is True


# ------------------------------------------------------------- what missing is ---


def test_a_reference_to_nothing_is_missing_rather_than_an_error() -> None:
    """Otherwise every condition on a parallel branch has to be defensive,
    and the defensive version is where the real bugs live."""
    assert value("$.steps.writing.output.count") is MISSING
    assert value("$.input.nope.deeper.still") is MISSING
    assert value("$.steps.research.output.competitors[99]") is MISSING


def test_missing_equals_nothing_including_itself() -> None:
    """Two references that both name nothing are not thereby the same."""
    assert value("$.a == $.b") is False
    assert value("$.a != $.b") is True
    assert value("$.a == null") is False


def test_missing_compares_false_in_both_directions() -> None:
    """So a condition on a step that has not run simply does not fire."""
    assert value("$.steps.writing.output.count > 3") is False
    assert value("$.steps.writing.output.count < 3") is False


def test_the_length_of_something_absent_is_not_zero() -> None:
    """`length($.typo) == 0` reading as true is how a typo passes review."""
    assert value("length($.steps.writing.output.items)") is MISSING
    assert value("length($.steps.writing.output.items) == 0") is False


def test_a_string_is_not_indexable() -> None:
    """Otherwise `...summary[0]` returns a character and a typo looks right."""
    assert value("$.steps.research.output.summary[0]") is MISSING


# ------------------------------------------------------- truthiness is narrow ---


@pytest.mark.parametrize("source", ["''", "0", "null", "'anything'", "1"])
def test_only_true_passes_a_condition(source: str) -> None:
    """A step that runs or not depending on whether a provider returned `""`
    or `0` is a step nobody can predict from reading the workflow."""
    assert evaluate_condition(source, SCOPE) is False


def test_true_passes() -> None:
    assert evaluate_condition("true", SCOPE) is True
    assert evaluate_condition("$.steps.research.output.complete", SCOPE) is True


def test_a_boolean_does_not_equal_a_number() -> None:
    """Python says `True == 1`. A workflow author would not."""
    assert value("true == 1") is False
    assert value("false == 0") is False


def test_and_short_circuits() -> None:
    """So `exists($.x) and $.x > 3` is safe to write."""
    assert value("exists($.steps.writing) and $.steps.writing.output.n > 3") is False


# ------------------------------------------------------------- adversarial ---


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os')",
        "().__class__",
        "open('/etc/passwd')",
        "eval('1')",
        "exec('x=1')",
        "self",
        "globals()",
        "$.input|attr('x')",
        "{{ 7*7 }}",
        "${jndi:ldap://x}",
        "1; import os",
        "lambda: 1",
        "[x for x in range(10)]",
    ],
)
def test_nothing_that_looks_like_an_escape_parses(source: str) -> None:
    """Every one of these is a refusal at parse time, not a sandboxed call.

    There is no identifier namespace, no attribute access and no call that is
    not `exists` or `length`, so these fail for the same boring reason a
    typo does.
    """
    with pytest.raises(ValidationFailedError):
        parse(source)


def test_a_dunder_path_segment_reaches_no_attribute() -> None:
    """These *do* parse, and the security claim for them is different.

    `$.input.__class__` is a path whose last segment happens to spell a
    Python dunder. Refusing it at parse time would be security theatre - the
    name is not special to anything here. What matters is where it resolves:
    a dictionary key if the data has one, and missing if it does not. It
    never reaches an attribute, because `_resolve` has no `getattr` to reach
    one with.
    """
    assert evaluate("$.input.__class__", {"input": {}}) is MISSING
    assert evaluate("$.input.__class__", {"input": {"__class__": "a string"}}) == "a string"
    assert evaluate("$.input.sector.__class__.__bases__", SCOPE) is MISSING
    assert evaluate("$.__builtins__.open", SCOPE) is MISSING


def test_no_function_beyond_the_documented_two() -> None:
    for name in ("print", "min", "max", "sum", "range", "type", "getattr"):
        with pytest.raises(ValidationFailedError):
            parse(f"{name}($.input.limit)")


def test_an_expression_that_is_too_long_is_refused() -> None:
    with pytest.raises(ValidationFailedError) as caught:
        parse("true" + " or true" * (MAX_SOURCE_LENGTH // 8 + 10))
    assert "characters" in caught.value.detail


def test_an_expression_with_too_many_tokens_is_refused() -> None:
    with pytest.raises(ValidationFailedError) as caught:
        parse(" or ".join(["true"] * (MAX_TOKENS + 10)))
    assert "characters" in caught.value.detail or "tokens" in caught.value.detail


def test_deep_nesting_is_refused_rather_than_crashing_the_interpreter() -> None:
    """ "No recursion limit to blow" is a promise this module keeps, not one
    Python makes: the depth is counted, so the refusal is a 422 and not a
    RecursionError that takes the worker with it."""
    with pytest.raises(ValidationFailedError) as caught:
        parse("(" * (MAX_DEPTH + 5) + "true" + ")" * (MAX_DEPTH + 5))
    assert "deep" in caught.value.detail


def test_deep_nesting_through_not_is_also_bounded() -> None:
    """The bound has to cover every recursive production, not just parens."""
    with pytest.raises(ValidationFailedError):
        parse("not " * (MAX_DEPTH + 5) + "true")


def test_comparisons_do_not_chain() -> None:
    """The two plausible readings of `a < b < c` differ, so neither is given."""
    with pytest.raises(ValidationFailedError) as caught:
        parse("1 < 2 < 3")
    assert "chain" in caught.value.detail


@pytest.mark.parametrize(
    "source",
    ["", "   ", "$.", "$.input.", "(", ")", "true and", "and true", "'unterminated", "$[a]"],
)
def test_malformed_expressions_are_refused_with_a_reason(source: str) -> None:
    with pytest.raises(ValidationFailedError) as caught:
        parse(source)
    assert caught.value.context["field"] == "expression"
    assert caught.value.detail


# ------------------------------------------------------------------- fuzzing ---

_ALPHABET = string.ascii_letters + string.digits + "$.[]()'\"<>=!+-*/&| \t\n_,:;#@%^~`{}?"


def test_random_input_never_escapes_a_validation_error() -> None:
    """The roadmap asks for this one fuzzed, and this is the property that
    matters: whatever a person pastes in, the failure is a message about
    their expression - never a traceback, a hang, or a RecursionError.
    """
    rng = random.Random(20261101)
    for _ in range(4_000):
        length = rng.randint(0, 60)
        source = "".join(rng.choice(_ALPHABET) for _ in range(length))
        try:
            node = parse(source)
        except ValidationFailedError:
            continue
        # If it parsed, evaluating it must not raise either - except for the
        # one documented case, `length()` on something that has no length.
        with contextlib.suppress(ValidationFailedError):
            evaluate(node, SCOPE)


def test_fuzzing_actually_reaches_the_parser() -> None:
    """A fuzz test that rejects everything at the first character proves
    nothing, so the corpus is checked to contain things that parse."""
    rng = random.Random(7)
    parsed = 0
    for _ in range(4_000):
        source = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 12)))
        try:
            parse(source)
        except ValidationFailedError:
            continue
        parsed += 1
    assert parsed > 0, "the fuzz corpus never produced a valid expression"


def test_structured_fuzzing_of_reference_paths() -> None:
    """Paths are the part an author actually varies, so they get their own
    corpus rather than relying on random characters finding them."""
    rng = random.Random(99)
    segments = ["input", "steps", "research", "output", "count", "__class__", "0", "-1"]
    for _ in range(2_000):
        path = "".join(
            f"[{segment}]" if segment.lstrip("-").isdigit() else f".{segment}"
            for segment in rng.choices(segments, k=rng.randint(1, 6))
        )
        try:
            result = evaluate(f"${path}", SCOPE)
        except ValidationFailedError:
            continue
        assert not callable(result), f"${path} resolved to something callable"
        assert not hasattr(result, "__globals__"), f"${path} reached a function"

"""A model that answers with nothing must not produce a successful step.

The live acceptance run found an agent task recorded as `succeeded` with
`output.text == ""`. `claude-opus-5` had spent its whole output budget on an
extended-thinking block, the adapter discards thinking blocks as an unknown
type - deliberately, and documented - and nothing downstream objected to an
empty answer. Measured at the time:

    max_tokens=1   content []                  stop max_tokens  text ""
    max_tokens=4   content ['thinking']        stop max_tokens  text ""
    max_tokens=20  content ['thinking','text'] stop max_tokens  text "p"

`_unusable` is the gate. It reads the **neutral** `Completion`, so these
cases are decided once for every provider rather than in the one adapter
that happens to exist today, and a second adapter cannot forget them.

The harder half of this file is the cases that must still *pass*: a tool
call with no prose is a legitimate turn, and a truncated answer that carries
real content is the caller's problem to judge, not this function's.
"""

from __future__ import annotations

import pytest

from agentworld.domain.enums import DispatchStatus
from agentworld.infra.providers import Completion, StopReason, ToolCall, Usage
from agentworld.services.dispatch import (
    EMPTY_MODEL_RESPONSE,
    INCOMPLETE_MODEL_RESPONSE,
    _unusable,
)


def _completion(
    text: str = "",
    *,
    stop_reason: StopReason = StopReason.COMPLETED,
    tool_calls: tuple[ToolCall, ...] = (),
    output_tokens: int = 0,
    model: str = "test-model",
) -> Completion:
    return Completion(
        text=text,
        usage=Usage(input_tokens=11, output_tokens=output_tokens),
        stop_reason=stop_reason,
        model=model,
        tool_calls=tool_calls,
    )


# ------------------------------------------------------- what must be refused ---


def test_no_content_at_all_is_not_a_success() -> None:
    """`content: []` - what a one-token budget produced against the real API."""
    outcome = _unusable(_completion(""))

    assert outcome is not None
    assert outcome.status is DispatchStatus.FAILED
    assert outcome.error_code == EMPTY_MODEL_RESPONSE


def test_thinking_only_content_is_not_a_success() -> None:
    """A thinking block the adapter dropped leaves exactly this.

    By the time a completion reaches here the thinking block is already
    gone - `_completion` logs `provider_block_type_ignored` and drops it -
    so thinking-only arrives indistinguishable from no content at all. That
    is the point: the gate does not need to know about thinking, only that
    nothing usable survived.
    """
    outcome = _unusable(_completion("", stop_reason=StopReason.MAX_TOKENS, output_tokens=4))

    assert outcome is not None
    assert outcome.status is DispatchStatus.FAILED


@pytest.mark.parametrize("blank", ["", " ", "\n", "\t\n  "])
def test_blank_text_counts_as_nothing(blank: str) -> None:
    """Whitespace is not an answer.

    A step whose recorded output is a newline is as useless to the next
    step's input mapping as an empty string, and treating them differently
    would make the guarantee depend on whether a model emitted a space.
    """
    assert _unusable(_completion(blank)) is not None


def test_a_truncated_empty_answer_is_reported_as_incomplete() -> None:
    """Two codes, because the remedy differs.

    `max_tokens` says the budget caused the silence, so an operator reading
    `last_error_code` knows to raise `max_output_tokens`. Any other stop
    reason with no content is a model that simply said nothing.
    """
    truncated = _unusable(_completion("", stop_reason=StopReason.MAX_TOKENS))
    silent = _unusable(_completion("", stop_reason=StopReason.COMPLETED))

    assert truncated is not None and truncated.error_code == INCOMPLETE_MODEL_RESPONSE
    assert silent is not None and silent.error_code == EMPTY_MODEL_RESPONSE


def test_the_failure_is_not_retryable() -> None:
    """Retrying buys the same silence at the same price.

    The same request against the same budget produces the same result, so a
    retry spends money to learn nothing; where the budget is the cause the
    remedy is a configuration change, not a repetition. A human can still
    resolve the step through the T6b review route.
    """
    for stop in (StopReason.COMPLETED, StopReason.MAX_TOKENS, StopReason.OTHER):
        outcome = _unusable(_completion("", stop_reason=stop))
        assert outcome is not None
        assert outcome.retryable is False, stop


def test_the_diagnostics_name_the_stop_reason_and_carry_no_content() -> None:
    """Enough to act on, and nothing that could leak.

    The stop reason is the actionable part. There is no model text in these
    cases by definition, but the error string is written to carry none
    regardless - a later caller copying this shape should not learn to put
    model output in an error message.
    """
    outcome = _unusable(
        _completion("", stop_reason=StopReason.MAX_TOKENS, output_tokens=16, model="opus-x")
    )

    assert outcome is not None
    assert outcome.error is not None
    assert "max_tokens" in outcome.error
    assert "opus-x" in outcome.error
    assert "16" in outcome.error
    assert outcome.output is None, "a failure must not carry an output"


def test_a_model_that_reported_no_name_still_produces_a_readable_error() -> None:
    outcome = _unusable(_completion("", model=""))

    assert outcome is not None
    assert outcome.error is not None
    assert "unreported" in outcome.error


# -------------------------------------------------------- what must still pass ---


def test_real_text_is_usable() -> None:
    assert _unusable(_completion("Four competitors.")) is None


def test_a_tool_call_without_text_is_usable() -> None:
    """A tool-use turn is prose-free by nature, and must not be refused.

    `advance()` cannot emit a `tool_call` dispatch in this release, so no
    such completion reaches here through the approved path yet. The rule is
    written for when it can, and this test is what stops the empty-text gate
    from quietly breaking tool use the day it arrives.
    """
    call = ToolCall(id="toolu_01", name="search", arguments={"q": "competitors"})

    assert _unusable(_completion("", stop_reason=StopReason.TOOL_USE, tool_calls=(call,))) is None


def test_a_truncated_answer_that_carries_content_is_not_refused() -> None:
    """The deliberate limit of this check.

    `stop_reason = max_tokens` with real text is a fragment - the vocabulary
    says as much - but whether a fragment is useful is the caller's
    question. A summary cut short at a thousand tokens may be exactly what
    was wanted; a sentence cut mid-word may not be. The provider gives no
    signal that separates them, and inventing one - a length floor, a
    sentence-boundary check - would be this layer guessing at intent. The
    stop reason is preserved in the successful output instead.
    """
    assert (
        _unusable(_completion("A partial answer that was cut", stop_reason=StopReason.MAX_TOKENS))
        is None
    )


def test_a_single_character_truncated_answer_is_still_not_refused() -> None:
    """The uncomfortable case, asserted on purpose.

    `"p"` from a truncated `"pong"` is almost certainly useless, and this
    check lets it through. Rejecting it would need a rule about how much
    text is enough, which is the invention this function refuses to make.
    Recorded here so the behaviour is a decision rather than an oversight.
    """
    assert _unusable(_completion("p", stop_reason=StopReason.MAX_TOKENS)) is None

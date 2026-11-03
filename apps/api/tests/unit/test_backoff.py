"""When the next attempt becomes due.

Mostly asserted on the *window* rather than on a draw. `_backoff` asks
`random.uniform(low, high)` for a number, so a test that records `low` and
`high` states the policy exactly - equal jitter is `(ceiling / 2, ceiling)` -
with no clock, no sampling and no stubbed arithmetic standing in for the real
thing. A draw can only ever be a point inside a window; the window is the
decision.

These started as integration assertions, and CI failed them. They measured
`run_after - now()` - a *remaining* duration, which shrinks while the test
runs - against a lower bound, leaving one second for a settle commit plus a
SELECT on a shared runner. `_backoff` is a pure function, so the arithmetic
belongs here instead.

What stays in the integration tests is what only they can show: that a
retryable failure returns to `pending` with a future `run_after`, and that a
backed-off row is not claimable until it is due.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from agentworld.domain.enums import DispatchKind, DispatchStatus, SideEffect
from agentworld.repositories.dispatches import ClaimedDispatch
from agentworld.services import dispatch as dispatch_service
from agentworld.services.dispatch import (
    RETRY_AFTER_MAX_SECONDS,
    Outcome,
    _backoff,
    _will_retry,
)

#: The base ceiling for the first retry. Not a knob this file may turn - it is
#: stated here so a change to it shows up as a failure with a reason.
BASE_CEILING = 2.0


def _claimed(*, claim_count: int, attempt: int = 0, max_attempts: int = 3) -> ClaimedDispatch:
    return ClaimedDispatch(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        execution_step_id=uuid.uuid4(),
        step_key="research",
        kind=DispatchKind.MANAGED_AGENT_TURN,
        side_effect_class=SideEffect.READ,
        instruction={},
        attempt=attempt,
        max_attempts=max_attempts,
        claim_count=claim_count,
        idempotency_token=uuid.uuid4(),
        leased_until=dt.datetime.now(dt.UTC),
    )


def _failed(**kwargs: Any) -> Outcome:
    return Outcome(
        status=DispatchStatus.FAILED,
        error="the provider said no",
        error_code="unavailable",
        **kwargs,
    )


class _Jitter:
    """Records the window `_backoff` asked for, and answers predictably.

    `draw` picks where in the window the answer falls, so one fixture serves
    both "what window was chosen" and "is the returned timestamp that far
    ahead". `calls` is what proves `Retry-After` precedence: when the header
    is used, the jitter is never consulted at all.
    """

    def __init__(self, draw: str = "high") -> None:
        self.calls: list[tuple[float, float]] = []
        self._draw = draw

    def uniform(self, low: float, high: float) -> float:
        self.calls.append((low, high))
        return low if self._draw == "low" else high

    @property
    def window(self) -> tuple[float, float]:
        assert len(self.calls) == 1, f"expected one jitter call, got {self.calls}"
        return self.calls[0]


@pytest.fixture
def jitter(monkeypatch: pytest.MonkeyPatch) -> _Jitter:
    recorder = _Jitter()
    monkeypatch.setattr(dispatch_service, "random", recorder)
    return recorder


def _due_in(claim_count: int, **outcome: Any) -> float:
    before = dt.datetime.now(dt.UTC)
    due = _backoff(_claimed(claim_count=claim_count), _failed(**outcome))
    return (due - before).total_seconds()


# ------------------------------------------------- the equal-jitter window ---


def test_the_first_retry_waits_between_one_and_two_seconds(jitter: _Jitter) -> None:
    """Equal jitter: half the ceiling to the ceiling, never from zero.

    Full jitter - `uniform(0, ceiling)` - put a measured 4.95% of first
    retries inside 100 ms, which is a delay named "backoff" that did not
    delay anything. The lower bound is the whole change.
    """
    _backoff(_claimed(claim_count=1), _failed())

    assert jitter.window == (BASE_CEILING / 2, BASE_CEILING)


def test_the_second_retry_waits_between_two_and_four_seconds(jitter: _Jitter) -> None:
    _backoff(_claimed(claim_count=2), _failed())

    assert jitter.window == (2.0, 4.0)


@pytest.mark.parametrize(
    ("claim_count", "expected"),
    [
        (1, (1.0, 2.0)),
        (2, (2.0, 4.0)),
        (3, (4.0, 8.0)),
        (4, (8.0, 16.0)),
        (5, (16.0, 32.0)),
    ],
)
def test_the_window_doubles_with_each_claim(
    jitter: _Jitter, claim_count: int, expected: tuple[float, float]
) -> None:
    """The ceiling grows, and the floor grows with it.

    It did not grow at all before the Phase 3 acceptance review: the ceiling
    read `claimed.attempt`, which migration 0019 makes immutable, so every
    retry of a row drew from the same two-second window.
    """
    _backoff(_claimed(claim_count=claim_count), _failed())

    assert jitter.window == expected


def test_the_window_is_capped_at_fifteen_minutes(jitter: _Jitter) -> None:
    """Doubling cannot run away, and half a capped ceiling is still capped."""
    _backoff(_claimed(claim_count=40), _failed())

    low, high = jitter.window
    assert high == RETRY_AFTER_MAX_SECONDS
    assert low == RETRY_AFTER_MAX_SECONDS / 2


@pytest.mark.parametrize("claim_count", [1, 5, 10, 20, 40, 100])
def test_no_part_of_the_window_exceeds_the_cap(jitter: _Jitter, claim_count: int) -> None:
    """The property, over a range that includes absurd claim counts."""
    _backoff(_claimed(claim_count=claim_count), _failed())

    low, high = jitter.window
    assert 0 < low <= high <= RETRY_AFTER_MAX_SECONDS


def test_the_returned_timestamp_is_the_draw_ahead_of_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window reaches `run_after`, rather than being computed and dropped.

    Both ends, because pinning one of them would not notice a floor that is
    recorded in the window and then ignored.
    """
    for draw, expected in (("low", 1.0), ("high", 2.0)):
        recorder = _Jitter(draw=draw)
        monkeypatch.setattr(dispatch_service, "random", recorder)
        assert expected <= _due_in(1) < expected + 0.5, f"the {draw} end did not reach run_after"


# ------------------------------------------------------- `Retry-After` wins ---


def test_a_usable_retry_after_bypasses_the_jitter_entirely(jitter: _Jitter) -> None:
    """Precedence asserted by absence: the jitter is never consulted.

    Stronger than comparing durations. The provider named a time, so there is
    nothing to guess and nothing to add noise to.
    """
    assert 30.0 <= _due_in(1, retry_after_seconds=30.0) < 30.5
    assert jitter.calls == [], "the header was honoured but the jitter still ran"


def test_a_zero_retry_after_is_an_explicit_zero_delay(jitter: _Jitter) -> None:
    """Zero is a statement, not a missing value.

    A provider saying "now" is saying something about its own readiness, and
    the floor is for *our* guesses. This is the one immediate-retry path left
    after the floor, and it is deliberate - so it is pinned rather than left
    to be discovered.
    """
    assert _due_in(1, retry_after_seconds=0.0) < 0.5
    assert jitter.calls == []


def test_an_outrageous_retry_after_is_clamped(jitter: _Jitter) -> None:
    """A provider cannot park a tenant's queue for a day."""
    delay = _due_in(1, retry_after_seconds=86_400.0)

    assert RETRY_AFTER_MAX_SECONDS <= delay < RETRY_AFTER_MAX_SECONDS + 0.5
    assert jitter.calls == []


def test_just_over_the_cap_is_clamped_too(jitter: _Jitter) -> None:
    delay = _due_in(1, retry_after_seconds=RETRY_AFTER_MAX_SECONDS + 1)

    assert RETRY_AFTER_MAX_SECONDS <= delay < RETRY_AFTER_MAX_SECONDS + 0.5


def test_a_negative_retry_after_falls_back_to_equal_jitter(jitter: _Jitter) -> None:
    """Not a delay, so not usable. The fallback applies, floor included."""
    _backoff(_claimed(claim_count=1), _failed(retry_after_seconds=-5.0))

    assert jitter.window == (1.0, 2.0)


def test_no_header_at_all_falls_back_to_equal_jitter(jitter: _Jitter) -> None:
    """The ordinary case: the adapter saw no usable header."""
    _backoff(_claimed(claim_count=1), _failed(retry_after_seconds=None))

    assert jitter.window == (1.0, 2.0)


# ----------------------------------- what this change must leave untouched ---


def test_an_uncertain_outcome_is_never_retried() -> None:
    """A backoff existing does not make an unknown outcome retryable.

    `needs_review` means nobody knows whether the external effect landed.
    `_will_retry` refuses anything that is not a plain `FAILED`, so no jitter
    policy can reach it. Asserted here because this file is where someone
    will come to change retry timing.
    """
    uncertain = Outcome(
        status=DispatchStatus.NEEDS_REVIEW,
        error="the request was in flight when the worker died",
        error_code="timeout",
        uncertain=True,
    )

    assert _will_retry(_claimed(claim_count=1), uncertain) is False


def test_a_terminal_classification_is_never_retried() -> None:
    """A 400 cannot be fixed by waiting, whatever the wait is."""
    assert _will_retry(_claimed(claim_count=1), _failed(retryable=False)) is False


@pytest.mark.parametrize(
    ("claim_count", "retries"),
    [(1, True), (2, True), (3, False), (4, False)],
)
def test_the_attempt_budget_still_bounds_at_max_attempts(claim_count: int, retries: bool) -> None:
    """Three claims, three provider calls, then terminal - unchanged."""
    assert _will_retry(_claimed(claim_count=claim_count), _failed()) is retries

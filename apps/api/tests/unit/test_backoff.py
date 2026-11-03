"""When the next attempt becomes due.

These were integration assertions first, and CI failed them. They measured
`run_after - now()` - a *remaining* duration - against a lower bound, and a
remaining duration shrinks while the test is running. A two-second ceiling
with a one-second lower bound allows one second for a database round trip on
a shared runner, which is not enough, and the failure says "the ceiling
moved" when nothing about the ceiling moved at all.

`_backoff` is a pure function of a claimed dispatch and an outcome, so the
claim belongs here, where there is no database between computing the value
and reading it. The integration tests keep what only they can show: that a
retryable failure returns to `pending` with a future `run_after`, and that a
row is not claimable until it is due.
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest

from agentworld.domain.enums import DispatchKind, DispatchStatus, SideEffect
from agentworld.repositories.dispatches import ClaimedDispatch
from agentworld.services import dispatch as dispatch_service
from agentworld.services.dispatch import RETRY_AFTER_MAX_SECONDS, Outcome, _backoff


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


def _failed(**kwargs: object) -> Outcome:
    return Outcome(status=DispatchStatus.FAILED, error="nope", error_code="unavailable", **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _pin_the_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Draw the ceiling every time.

    The fallback backoff is `random.uniform(0, ceiling)`, so an unpinned draw
    says nothing about the ceiling - and can be microseconds, which is its
    own problem (see `test_the_fallback_has_no_floor`).
    """
    monkeypatch.setattr(
        dispatch_service, "random", SimpleNamespace(uniform=lambda _low, high: high)
    )


def _seconds(claim_count: int, **outcome: object) -> float:
    before = dt.datetime.now(dt.UTC)
    due = _backoff(_claimed(claim_count=claim_count), _failed(**outcome))
    return (due - before).total_seconds()


def test_the_ceiling_doubles_with_each_claim() -> None:
    """2, 4, 8 - and the point is that it moves at all.

    It did not. The ceiling read `claimed.attempt`, which migration 0019
    makes immutable, so every retry of a row drew from the same two-second
    window: a failing call re-sent about once a second for ever. The Phase 3
    acceptance review measured five such calls against a `max_attempts` of 3.
    """
    first, second, third = (_seconds(1), _seconds(2), _seconds(3))

    assert 2.0 <= first < 2.5, f"the first ceiling is no longer two seconds: {first}"
    assert 4.0 <= second < 4.5, f"the second ceiling did not double: {second}"
    assert 8.0 <= third < 8.5, f"the third ceiling did not double: {third}"


def test_the_ceiling_is_capped() -> None:
    """Doubling cannot run away: the cap is the same 900 the header gets."""
    assert _seconds(40) <= RETRY_AFTER_MAX_SECONDS + 0.5


def test_a_usable_retry_after_wins_over_the_ceiling() -> None:
    """The provider knows when it will serve the key again; we are guessing."""
    assert 30.0 <= _seconds(1, retry_after_seconds=30.0) < 30.5


def test_an_outrageous_retry_after_is_clamped() -> None:
    delay = _seconds(1, retry_after_seconds=86_400.0)
    assert RETRY_AFTER_MAX_SECONDS <= delay < RETRY_AFTER_MAX_SECONDS + 0.5


def test_an_unusable_retry_after_falls_back_to_the_ceiling() -> None:
    """Negative is not a delay. The ceiling is used rather than the header."""
    assert 2.0 <= _seconds(1, retry_after_seconds=-5.0) < 2.5


def test_the_fallback_has_no_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorded, not asserted as desirable: the jitter can draw ~zero.

    `random.uniform(0, ceiling)` is full jitter, so the first retry of a
    failing call can be immediate. That is a real strategy - it spreads a
    thundering herd - but it means "backoff" does not guarantee any delay,
    and it is what made
    `test_a_backed_off_retry_is_not_claimable_until_it_is_due` flaky: the row
    was already claimable before the test could look. Pinned here so the
    behaviour is visible in a test rather than discovered in CI. Whether the
    fallback should have a floor is a T8 question.
    """
    monkeypatch.setattr(dispatch_service, "random", SimpleNamespace(uniform=lambda low, _high: low))
    assert _seconds(1) < 0.5, "the fallback grew a floor; update this test and T8's note"

"""Every dispatch status is classified, and the two classifications agree.

`settle` and `resolve_review` both decide whether to stamp `finished_at` from
`DispatchStatus.is_terminal`, so an unclassified status would silently get
the wrong answer in two places at once. The Phase 3 acceptance run found
exactly that shape of bug by hand - `failed` omitted from a list of terminal
statuses - and these tests are what make a list unnecessary.
"""

from __future__ import annotations

import pytest

from agentworld.domain.enums import DispatchStatus

#: Pinned deliberately. A status added to the enum fails this test until
#: somebody decides which side it belongs on, which is the point: the two
#: writers of `finished_at` read the predicate, not a literal.
TERMINAL = {
    DispatchStatus.SUCCEEDED,
    DispatchStatus.FAILED,
    DispatchStatus.DEAD,
    DispatchStatus.CANCELLED,
}
UNSETTLED = {
    DispatchStatus.PENDING,
    DispatchStatus.LEASED,
    DispatchStatus.NEEDS_REVIEW,
}


def test_the_two_classifications_are_exact_complements() -> None:
    """One is defined as the negation of the other; this holds them to it."""
    for status in DispatchStatus:
        assert status.is_terminal is not status.is_unsettled, status.value


def test_every_status_is_classified_and_none_is_both() -> None:
    """A partition, not two overlapping opinions."""
    terminal = {s for s in DispatchStatus if s.is_terminal}
    unsettled = {s for s in DispatchStatus if s.is_unsettled}

    assert terminal | unsettled == set(DispatchStatus)
    assert terminal & unsettled == set()


def test_the_terminal_set_is_what_it_should_be() -> None:
    """Fails when a status is added, which is how the decision gets made.

    `failed` is in here and that is the correction: a retryable failure is
    settled as `pending`, so by the time a row reads `failed` the work is
    over and the row deserves a finish time.
    """
    assert {s for s in DispatchStatus if s.is_terminal} == TERMINAL


def test_the_unsettled_set_is_what_it_should_be() -> None:
    """`needs_review` is unsettled, not terminal.

    It is waiting for a person, so it is not finished - and `finished_at`
    must stay NULL until a human resolution moves it somewhere terminal.
    """
    assert {s for s in DispatchStatus if s.is_unsettled} == UNSETTLED


@pytest.mark.parametrize("status", sorted(TERMINAL, key=lambda s: s.value))
def test_a_terminal_status_is_not_claimable_in_spirit(status: DispatchStatus) -> None:
    """The claim query filters on `pending`, so nothing terminal is claimed.

    Asserted as a property of the classification rather than of the SQL: if
    a terminal status were ever also unsettled, the claim predicate and the
    finish-time rule would disagree about the same row.
    """
    assert not status.is_unsettled
    assert status is not DispatchStatus.PENDING

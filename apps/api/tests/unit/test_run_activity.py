"""D4.2a: the twelve rows of the activity truth table.

`RunActivity` is pure, so each row below is a question about arithmetic
rather than about timing, and every case asserts all three derived values
together - `activity_evidence`, `last_activity_at` and
`has_untimed_activity`. Asserting them one at a time would let a rule change
be half-applied: the clock could move without the evidence following it, and
nothing would notice.

The two decisions worth naming, because both are easy to get backwards:

**A tie goes to the run-level clock.** A `run.started` and the first
`run.step_started` posted in one batch share one `occurred_at`, so the tie is
the ordinary case for a run's first moment rather than a curiosity. Resolving
it to the run is what makes `trace_step` mean "the trace knows something the
run row does not".

**`has_untimed_activity` is not "this run made tool calls".**
`tool_call_count` says that. The flag says counted tool work is the newest
kind of evidence held and cannot be dated, which is why a dated trace step
outranks it (rows 11-12 below).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from agentworld.domain.entities import RunActivity
from agentworld.domain.enums import ActivityEvidence, RunStatus
from agentworld.domain.reducers.fold import _SETTLED_RUNS

NOW = dt.datetime(2026, 11, 13, 12, 0, 0, tzinfo=dt.UTC)
STARTED = NOW - dt.timedelta(hours=2)
#: Three distinct instants, so "newer", "equal" and "older" are unambiguous.
EARLY = NOW - dt.timedelta(minutes=30)
MIDDLE = NOW - dt.timedelta(minutes=20)
LATE = NOW - dt.timedelta(minutes=10)


def run(
    *,
    run_clock: dt.datetime | None,
    step_clock: dt.datetime | None,
    tool_calls: int = 0,
    steps: int = 0,
    started_at: dt.datetime = STARTED,
) -> RunActivity:
    return RunActivity(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        started_at=started_at,
        run_ref="run-1",
        step_count=steps,
        tool_call_count=tool_calls,
        last_event_at=run_clock,
        last_step_event_at=step_clock,
    )


# ------------------------------------------------- the run clock is the newest ---


def test_row_1_no_trace_and_no_tool_calls() -> None:
    """The plainest case: a run that opened and said nothing since."""
    row = run(run_clock=MIDDLE, step_clock=None)
    assert row.activity_evidence is ActivityEvidence.RUN_EVENT
    assert row.last_activity_at == MIDDLE
    assert row.has_untimed_activity is False


def test_row_2_tool_only_activity_is_flagged() -> None:
    """**The case the flag exists for.**

    This agent has invoked forty tools and reported no steps, so every
    clock reads as old while it is demonstrably working. `run_event` is
    literally true and reads as "nothing since it started", which is
    exactly the misreading the boolean is here to prevent.
    """
    row = run(run_clock=MIDDLE, step_clock=None, tool_calls=40)
    assert row.activity_evidence is ActivityEvidence.RUN_EVENT
    assert row.last_activity_at == MIDDLE
    assert row.has_untimed_activity is True


def test_row_5_a_tie_goes_to_the_run_clock() -> None:
    """Equal timestamps, which is a batch's first moment and not an edge case."""
    row = run(run_clock=MIDDLE, step_clock=MIDDLE, steps=1)
    assert row.trace_is_newer is False
    assert row.activity_evidence is ActivityEvidence.RUN_EVENT
    assert row.last_activity_at == MIDDLE
    assert row.has_untimed_activity is False


def test_row_6_a_tie_with_tool_calls_is_still_flagged() -> None:
    row = run(run_clock=MIDDLE, step_clock=MIDDLE, steps=1, tool_calls=3)
    assert row.activity_evidence is ActivityEvidence.RUN_EVENT
    assert row.has_untimed_activity is True


def test_row_7_steps_older_than_the_run_clock_are_still_run_event() -> None:
    """**`trace_step` does not mean "has steps".**

    This run has a trace, and its newest step is older than the run's own
    clock - a late step, or a `run.finished` that has not landed. The
    evidence names the newest timestamp, not the existence of the trace,
    and a client reading it the other way will be wrong.
    """
    row = run(run_clock=LATE, step_clock=EARLY, steps=5)
    assert row.step_count == 5
    assert row.activity_evidence is ActivityEvidence.RUN_EVENT
    assert row.last_activity_at == LATE
    assert row.has_untimed_activity is False


def test_row_8_older_steps_with_tool_calls_are_flagged() -> None:
    row = run(run_clock=LATE, step_clock=EARLY, steps=5, tool_calls=2)
    assert row.activity_evidence is ActivityEvidence.RUN_EVENT
    assert row.last_activity_at == LATE
    assert row.has_untimed_activity is True


# ---------------------------------------------- the trace clock is the newest ---


def test_row_3_a_newer_step_wins() -> None:
    row = run(run_clock=EARLY, step_clock=LATE, steps=2)
    assert row.trace_is_newer is True
    assert row.activity_evidence is ActivityEvidence.TRACE_STEP
    assert row.last_activity_at == LATE
    assert row.has_untimed_activity is False


def test_row_4_a_dated_trace_outranks_an_undated_counter() -> None:
    """Tool calls *and* a newer step: the flag stays False, deliberately.

    The boolean is not "there are tool calls" - `tool_call_count` is still
    2 here and says so. It is "counted tool work is the newest thing we
    know", and a step timestamp newer than the run's own is better evidence
    than a counter, so there is nothing for the flag to add.
    """
    row = run(run_clock=EARLY, step_clock=LATE, steps=2, tool_calls=2)
    assert row.tool_call_count == 2
    assert row.activity_evidence is ActivityEvidence.TRACE_STEP
    assert row.last_activity_at == LATE
    assert row.has_untimed_activity is False


# ------------------------------------------------- the seeded half of the table ---
#
# Every row below needs `last_event_at` to be NULL, which no ingested event
# can produce: `_append_run` writes it in the same INSERT that creates the
# row. They are reachable only by direct SQL - a seeded fixture, or a run
# from before the producer landed - and the integration suite proves that
# claim against the real database rather than restating it here.


def test_row_9_neither_clock_falls_back_to_the_start() -> None:
    row = run(run_clock=None, step_clock=None)
    assert row.activity_evidence is ActivityEvidence.NONE
    assert row.last_activity_at == STARTED, "no timestamp is invented"
    assert row.has_untimed_activity is False


def test_row_10_neither_clock_with_tool_calls_is_flagged() -> None:
    """`none` and the flag are independent, and both are honest.

    Nothing is dated, so the fallback is `started_at`; tool calls were
    counted, so the flag is true. Neither fabricates a timestamp for the
    other.
    """
    row = run(run_clock=None, step_clock=None, tool_calls=7)
    assert row.activity_evidence is ActivityEvidence.NONE
    assert row.last_activity_at == STARTED
    assert row.has_untimed_activity is True


def test_row_11_a_trace_beats_a_missing_run_clock() -> None:
    """A step is strictly newer than a run-level timestamp that is absent."""
    row = run(run_clock=None, step_clock=LATE, steps=1)
    assert row.trace_is_newer is True
    assert row.activity_evidence is ActivityEvidence.TRACE_STEP
    assert row.last_activity_at == LATE
    assert row.has_untimed_activity is False


def test_row_12_a_trace_beats_a_missing_run_clock_with_tool_calls() -> None:
    row = run(run_clock=None, step_clock=LATE, steps=1, tool_calls=4)
    assert row.activity_evidence is ActivityEvidence.TRACE_STEP
    assert row.last_activity_at == LATE
    assert row.has_untimed_activity is False


# --------------------------------------------------------------- the whole table ---


def test_every_evidence_value_is_reachable() -> None:
    """A member added to the enum fails this until something produces it."""
    produced = {
        run(run_clock=MIDDLE, step_clock=None).activity_evidence,
        run(run_clock=EARLY, step_clock=LATE).activity_evidence,
        run(run_clock=None, step_clock=None).activity_evidence,
    }
    assert produced == set(ActivityEvidence)


@pytest.mark.parametrize(
    ("run_clock", "step_clock"),
    [
        (MIDDLE, None),
        (MIDDLE, MIDDLE),
        (LATE, EARLY),
        (EARLY, LATE),
        (None, LATE),
        (None, None),
    ],
)
def test_the_clock_and_the_evidence_always_agree(
    run_clock: dt.datetime | None, step_clock: dt.datetime | None
) -> None:
    """`last_activity_at` is always the timestamp the evidence names.

    The repository filters and orders on
    `COALESCE(GREATEST(run, trace), started_at)`, so if these two ever
    disagreed the rows a query selected and the ages reported for them
    would describe different runs.
    """
    row = run(run_clock=run_clock, step_clock=step_clock)
    expected = {
        ActivityEvidence.TRACE_STEP: step_clock,
        ActivityEvidence.RUN_EVENT: run_clock,
        ActivityEvidence.NONE: row.started_at,
    }[row.activity_evidence]
    assert row.last_activity_at == expected
    assert row.last_activity_at == max(
        [clock for clock in (run_clock, step_clock) if clock is not None] or [row.started_at]
    ), "the same answer GREATEST ... COALESCE gives in SQL"


# ---------------------------------------------------------------- elapsed time ---


def test_elapsed_seconds_is_floored_and_never_negative() -> None:
    """Clamped for the reason `Agent.seconds_since_heartbeat` is clamped.

    `occurred_at` is the producer's clock and ingest bounds its forward
    skew rather than correcting it, so a run whose newest event is dated
    slightly ahead of this server is a legitimate stored row.
    """
    assert (
        run(run_clock=NOW - dt.timedelta(seconds=90), step_clock=None).seconds_since_activity(NOW)
        == 90
    )
    assert (
        run(run_clock=NOW - dt.timedelta(seconds=1.9), step_clock=None).seconds_since_activity(NOW)
        == 1
    )
    assert (
        run(run_clock=NOW + dt.timedelta(seconds=4), step_clock=None).seconds_since_activity(NOW)
        == 0
    )


# ------------------------------------------------------------- what is excluded ---


def test_running_is_the_only_status_the_listing_may_include() -> None:
    """What makes `status == RUNNING` safe to write in the query.

    The repository filters on the one member rather than on "not settled",
    which is the same set today. A status added later without being
    classified would silently join a listing of quiet runs, so this fails
    until somebody decides which side it belongs on.
    """
    _, settled = _SETTLED_RUNS
    assert set(RunStatus) - {RunStatus(value) for value in settled} == {RunStatus.RUNNING}


# ------------------------------------------------- the gaps this stage records ---


def test_a_denied_tool_call_is_not_counted_and_the_api_says_so() -> None:
    """**A recorded gap, not an endorsement.**

    `domain/protocol/taxonomy.py` argues that `tool.denied` earns its place
    because "a tool call refused by a grant or a policy is information the
    island should show, and silence would make a blocked agent look like an
    idle one". The reducer disagrees: only `tool.invoked` increments
    `tool_call_count`, so an agent whose every call was refused reports no
    tool activity and this endpoint shows it as silent.

    Fixing that is a reducer change D4.2a excludes. What this stage does is
    stop the API claiming otherwise - `RunView.tool_call_count` used to say
    "refused ones included" - and pin the behaviour here, so the fix cannot
    land without this test and those descriptions being corrected together.
    """
    from agentworld.domain.protocol import parse_envelope
    from agentworld.domain.reducers import fold

    def mutations(event_type: str) -> tuple[object, ...]:
        return fold(
            parse_envelope(
                {
                    "dedup_key": "d",
                    "type": event_type,
                    "protocol_version": "1.0",
                    "occurred_at": NOW.isoformat(),
                    "subject": {"kind": "tool_call", "ref": "run-1:t1"},
                    "correlation": {"run_ref": "run-1"},
                }
            )
        )

    assert len(mutations("tool.invoked")) == 1, "the only tool event that counts"
    assert mutations("tool.denied") == (), "recorded as an event, folded into nothing"
    assert mutations("tool.failed") == ()
    assert mutations("tool.returned") == ()

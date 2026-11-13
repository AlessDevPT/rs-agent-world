"""Folding events into state changes.

Three kinds of test here. The behavioural ones check what each namespace
folds to. The structural ones check the two properties the design rests on -
that a reducer cannot cause work, and that it cannot read a clock - by
inspecting the code rather than by trusting it. And the trace test folds the
proposal's own eleven-event example and asserts what comes out, because an
end-to-end claim made in a document is worth executing.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from pathlib import Path
from typing import Any

import pytest

from agentworld.domain.enums import (
    AgentStatus,
    ExecutionStatus,
    RunStatus,
    StepStatus,
    TaskStatus,
)
from agentworld.domain.protocol import KNOWN_EVENTS, PROTOCOL_VERSION, parse_envelope
from agentworld.domain.reducers import (
    AgentRef,
    AgentStatusRef,
    Append,
    ExecutionRef,
    Increment,
    MutationTarget,
    Patch,
    RunRef,
    RunStepRef,
    StepRef,
    fold,
)
from agentworld.domain.reducers import fold as fold_module
from agentworld.domain.reducers.fold import (
    _SETTLED_EXECUTIONS,
    _SETTLED_RUN_STEPS,
    _SETTLED_RUNS,
    _SETTLED_STEPS,
)

AT = "2026-11-04T07:00:11.204Z"
WHEN = dt.datetime(2026, 11, 4, 7, 0, 11, 204000, tzinfo=dt.UTC)

#: The one mutation stage A1 added, which six event types now carry.
#: Guarded on the column it writes, so a late event cannot move it back,
#: and deliberately not `last_event_at` - see `test_the_activity_clock_
#: is_not_the_state_clock` below for what that separation protects.
STIRRED = Patch(
    RunRef("run-1"),
    {"last_activity_at": WHEN},
    not_before=("last_activity_at", WHEN),
)


def folded(
    event_type: str,
    kind: str,
    ref: str,
    payload: dict[str, Any] | None = None,
    **correlation: str,
) -> tuple[Any, ...]:
    return fold(
        parse_envelope(
            {
                "dedup_key": f"{ref}:{event_type}",
                "type": event_type,
                "protocol_version": PROTOCOL_VERSION,
                "occurred_at": AT,
                "subject": {"kind": kind, "ref": ref},
                "correlation": correlation,
                "payload": payload or {},
            }
        )
    )


# -------------------------------------------------------- promise 7, read off ---


def test_no_mutation_can_cause_work() -> None:
    """The seventh promise, as a property of the vocabulary.

    Not "the reducers do not write dispatches" - there is no mutation that
    could. If this ever fails because a target was added, the promise stopped
    being structural and became a convention, which is the thing it exists
    not to be.
    """
    targets = {member.value for member in MutationTarget}
    assert targets == {
        "agents",
        "agent_status_history",
        "tasks",
        "runs",
        "run_steps",
        "workflow_executions",
        "execution_steps",
    }
    forbidden = {"dispatches", "outbox", "credentials", "connections", "workflows"}
    assert targets & forbidden == set()


def test_no_known_event_folds_into_a_table_outside_that_vocabulary() -> None:
    """The taxonomy and the mutation vocabulary have to agree.

    They are written in different modules and could drift; a type declaring
    it folds into `dispatches` would be the drift that matters.
    """
    declared = {table for spec in KNOWN_EVENTS.values() for table in spec.folds_into}
    assert declared <= {member.value for member in MutationTarget}


def test_no_reducer_reads_a_clock() -> None:
    """Every timestamp written comes from the event's own `occurred_at`.

    A reducer that called `now()` would produce different state on replay
    than it did live, which would silently break the one property the event
    log exists to provide. Checked by parsing the module, because a test that
    folded an event and compared timestamps would pass for a reducer that
    read the clock only on an error path.
    """
    source = Path(inspect.getfile(fold_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = {"now", "utcnow", "today", "time", "monotonic", "time_ns"}
    found = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
    ]
    assert found == [], f"a reducer reads the clock: {found}"

    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name in {"time", "datetime", "random", "uuid", "secrets"}
    ]
    assert imported == [], f"a reducer imports a source of nondeterminism: {imported}"


def test_folding_the_same_event_twice_gives_the_same_mutations() -> None:
    """Pure, so replay is a function of the log and nothing else."""
    first = folded("workflow.step_succeeded", "execution_step", "exec-1:research")
    second = folded("workflow.step_succeeded", "execution_step", "exec-1:research")
    assert first == second


# ----------------------------------------------------------- folds to nothing ---


@pytest.mark.parametrize(
    "event_type",
    ["log.emitted", "metric.recorded", "custom.quota_reached", "galaxy.collapsed"],
)
def test_recorded_but_not_folded(event_type: str) -> None:
    """A log line, a metric and anything unknown change no state.

    Folding a log line would make the log its own derived state, and the
    distinction between "what happened" and "what is true now" is the only
    reason there are two of them.
    """
    assert folded(event_type, "agent", "researcher") == ()


def test_an_unresolvable_step_ref_folds_to_nothing_but_is_still_valid() -> None:
    """A bare step key names no execution, so there is nothing to fold onto.

    The event is not rejected: a log that drops what it cannot interpret is
    not a log.
    """
    assert folded("workflow.step_succeeded", "execution_step", "research") == ()


def test_a_correlation_that_disagrees_with_the_ref_folds_to_nothing() -> None:
    """Guessing which of the two the producer meant would open a gate on the
    wrong work."""
    assert (
        folded(
            "workflow.step_succeeded",
            "execution_step",
            "exec-1:research",
            execution_ref="exec-2",
        )
        == ()
    )


def test_an_unknown_agent_status_folds_to_nothing() -> None:
    """`agents.status` is a native enum; coercing would record a value nobody
    sent."""
    mutations = folded(
        "agent.status_changed", "agent", "researcher", {"status": "interpretive-dance"}
    )
    assert mutations == (
        Patch(
            AgentRef("researcher"),
            {"last_event_at": WHEN},
            not_before=("last_event_at", WHEN),
        ),
    )


# ------------------------------------------------------------------ the agent ---


def test_a_heartbeat_touches_two_timestamps_and_nothing_else() -> None:
    assert folded("agent.heartbeat", "agent", "researcher") == (
        Patch(
            AgentRef("researcher"),
            {"last_event_at": WHEN, "last_heartbeat_at": WHEN},
            # Guarded, so a backlog flushed by a worker that was offline
            # cannot drag the heartbeat backwards.
            not_before=("last_event_at", WHEN),
        ),
    )


def test_registration_announces_a_worker_and_does_not_create_an_agent() -> None:
    """The row already exists: someone configured it over the API. A
    registration that could create one would let a replay re-create a
    definition."""
    mutations = folded(
        "agent.registered",
        "agent",
        "researcher",
        {"capabilities": ["search"], "heartbeat_ttl_seconds": 90, "display_name": "Nope"},
    )
    assert len(mutations) == 1
    patch = mutations[0]
    assert patch.target is MutationTarget.AGENTS
    assert patch.fields["status"] is AgentStatus.IDLE
    assert patch.fields["capabilities"] == ["search"]
    assert patch.fields["heartbeat_ttl_seconds"] == 90
    assert "display_name" not in patch.fields, "configuration is control plane"


def test_an_absurd_heartbeat_ttl_is_ignored() -> None:
    mutations = folded(
        "agent.registered", "agent", "researcher", {"heartbeat_ttl_seconds": 99_999_999}
    )
    assert "heartbeat_ttl_seconds" not in mutations[0].fields


def test_a_status_change_appends_history_then_patches_the_status() -> None:
    """The history row is what makes "when did it break?" answerable.

    **The append comes first, and the order is load-bearing.** The repository
    fills `from_status` by reading the agent's current status, which is the
    status being left only while the patch below has not yet run. Stage D1.1
    swapped these two: for as long as the patch came first, every history row
    recorded the status it had moved to as the one it came from.
    """
    mutations = folded("agent.paused", "agent", "researcher", {"reason": "operator stopped it"})
    assert mutations == (
        Append(
            AgentStatusRef("researcher"),
            {
                "to_status": AgentStatus.PAUSED,
                "reason": "operator stopped it",
                "changed_at": WHEN,
            },
            not_before=("last_event_at", WHEN),
        ),
        Patch(
            AgentRef("researcher"),
            {
                "last_event_at": WHEN,
                "status": AgentStatus.PAUSED,
                "status_reason": "operator stopped it",
            },
            not_before=("last_event_at", WHEN),
        ),
    )


def test_the_history_append_leaves_from_status_to_the_repository() -> None:
    """It is the only layer that can read what the status was a moment ago."""
    append = folded("agent.deregistered", "agent", "researcher")[0]
    assert append.target is MutationTarget.AGENT_STATUS_HISTORY
    assert "from_status" not in append.fields


def test_a_history_append_carries_the_same_guard_as_its_status_patch() -> None:
    """All or nothing, for every event that moves a status.

    A late event has its patch skipped - a newer one already decided what the
    agent looks like - and a history row appended anyway would record a
    transition the agent never made. The two mutations therefore carry the
    same `not_before`, and this asserts it for every status event rather than
    for the one above.
    """
    unpaired: list[str] = []
    for event_type in KNOWN_EVENTS:
        if not event_type.startswith("agent."):
            continue
        mutations = folded(event_type, "agent", "researcher", {"status": "working"})
        appends = [m for m in mutations if isinstance(m, Append)]
        patches = [m for m in mutations if isinstance(m, Patch)]
        if not appends:
            continue
        if len(appends) != 1 or len(patches) != 1:
            unpaired.append(f"{event_type}: {len(appends)} appends, {len(patches)} patches")
        elif appends[0].not_before != patches[0].not_before:
            unpaired.append(f"{event_type}: {appends[0].not_before} != {patches[0].not_before}")
        elif mutations.index(appends[0]) > mutations.index(patches[0]):
            unpaired.append(f"{event_type}: the patch runs before the append")

    assert unpaired == [], f"these history appends are not paired with their patch: {unpaired}"


# ------------------------------------------------------------------- the task ---


def test_completion_sets_progress_to_a_hundred() -> None:
    """Otherwise a finished task renders a part-full bar forever."""
    patch = folded("task.completed", "task", "t-1", {"result": {"ok": True}})[0]
    assert patch.fields["status"] is TaskStatus.COMPLETED
    assert patch.fields["progress_percent"] == 100
    assert patch.fields["finished_at"] == WHEN
    assert patch.fields["result"] == {"ok": True}


def test_progress_does_not_start_a_task() -> None:
    """Progress on a task that is not running is a producer bug, and quietly
    starting it here would hide it."""
    patch = folded("task.progress", "task", "t-1", {"percent": 40, "message": "reading"})[0]
    assert patch.fields == {
        "progress_percent": 40,
        "progress_message": "reading",
        "updated_at": WHEN,
        "last_event_at": WHEN,
    }
    assert patch.not_before == ("last_event_at", WHEN)


@pytest.mark.parametrize("percent", [-1, 101, 1000, "40", True])
def test_out_of_range_progress_is_ignored(percent: Any) -> None:
    """The column has a check constraint; folding a bad value would turn a
    producer's mistake into a failed transaction for the whole batch."""
    assert folded("task.progress", "task", "t-1", {"percent": percent}) == ()


def test_a_string_error_is_wrapped_rather_than_dropped() -> None:
    """`"error": "timed out"` is what a producer writes first."""
    patch = folded("task.failed", "task", "t-1", {"error": "timed out"})[0]
    assert patch.fields["error"] == {"code": "unspecified", "message": "timed out"}


# -------------------------------------------------------------------- the run ---


def test_a_run_is_created_then_patched_by_the_event_that_starts_it() -> None:
    """R1. `run.started` is the only event that brings a run into being.

    The append comes first, as the status history's does, because the patch
    can only change a row that already exists - and for a run reported by an
    external agent, nothing else ever creates one. Both carry the same
    values, read from the event: `runs.started_at` defaults to `now()`, so
    leaving it out would make a replay disagree with the live fold about when
    the run began.
    """
    opened = {"status": RunStatus.RUNNING, "started_at": WHEN, "last_event_at": WHEN}

    assert folded("run.started", "run", "run-1", {}, agent_key="researcher") == (
        Append(RunRef("run-1"), opened),
        Patch(RunRef("run-1"), opened, not_before=("last_event_at", WHEN), unless=_SETTLED_RUNS),
    )


def test_a_run_append_is_deliberately_unguarded() -> None:
    """The one place an append does *not* share its patch's guard.

    A history row records a transition that may turn out to be stale, so
    appending one a late event did not earn would record something false -
    which is why D1.1 paired the two. A run either happened or it did not,
    and `run.started` is the only evidence either way: creating it late
    records something true late. The patch keeps both guards regardless, so
    a stale event still cannot move a settled run's status.
    """
    append, patch = folded("run.started", "run", "run-1", {}, agent_key="researcher")

    assert isinstance(append, Append)
    assert append.not_before is None, "a run that started did start"
    assert isinstance(patch, Patch)
    assert patch.not_before == ("last_event_at", WHEN)
    assert patch.unless == _SETTLED_RUNS


def test_the_agent_that_owns_a_run_is_not_in_the_mutation() -> None:
    """`agent_id` is the repository's to fill, like `from_status` before it.

    A reducer sees `agent_key="researcher"` and never a uuid - it has no way
    to resolve one - so the owner reaches the row through the refs the ingest
    transaction already resolved. Nothing here names the agent at all.
    """
    append = folded("run.started", "run", "run-1", {}, agent_key="researcher")[0]

    assert "agent_id" not in append.fields
    assert "agent_key" not in append.fields


def test_a_trace_step_appends_then_patches_and_counts() -> None:
    started = folded("run.step_started", "run", "run-1", {"seq": 1, "kind": "llm"})
    assert started == (
        Append(
            RunStepRef("run-1", 1),
            {
                "name": "step-1",
                "kind": "llm",
                "status": StepStatus.RUNNING,
                "input": None,
                "started_at": WHEN,
                "last_event_at": WHEN,
            },
        ),
        STIRRED,
    )

    finished = folded("run.step_finished", "run", "run-1", {"seq": 1, "output": {"tokens": 240}})
    assert finished[0].target is MutationTarget.RUN_STEPS
    assert finished[0].fields["status"] is StepStatus.SUCCEEDED
    assert finished[1] == Increment(RunRef("run-1"), {"step_count": 1})


def test_a_trace_step_without_a_sequence_cannot_be_placed() -> None:
    assert folded("run.step_started", "run", "run-1", {"kind": "llm"}) == ()


def test_a_step_that_reports_an_error_finished_failed() -> None:
    patch = folded("run.step_finished", "run", "run-1", {"seq": 2, "error": "429"})[0]
    assert patch.fields["status"] is StepStatus.FAILED


def test_a_failed_run_records_when_and_why() -> None:
    patch = folded("run.failed", "run", "run-1", {"error": {"code": "rate_limited"}})[0]
    assert patch.fields == {
        "status": RunStatus.FAILED,
        "finished_at": WHEN,
        "error": {"code": "rate_limited"},
        "last_event_at": WHEN,
    }
    assert patch.not_before == ("last_event_at", WHEN)


# ------------------------------------------------------------------- the tool ---


def test_an_invoked_tool_call_counts_and_moves_the_clock() -> None:
    assert folded("tool.invoked", "tool_call", "run-1:tool-1", run_ref="run-1") == (
        Increment(RunRef("run-1"), {"tool_call_count": 1}),
        STIRRED,
    )


def test_a_denied_tool_call_moves_the_clock_and_not_the_counter() -> None:
    """**The gap this test used to hide.**

    It was called `test_a_denied_tool_call_counts_as_an_attempt` and its
    body folded `tool.invoked` - so it asserted nothing at all about
    `tool.denied`, while its docstring repeated the claim `_tool`'s own
    docstring made: that leaving denials out of the count would make a
    blocked agent look like an idle one. The count never included them.

    Stage A1 makes that argument true by the clock rather than the
    counter, which is the honest half of it. A refused call is evidence
    the run is alive, so it moves `last_activity_at` and a wholly blocked
    run stops reading as quiet. It is still not a tool call - a denied
    call never ran - so `tool_call_count` is untouched, and whether
    denials deserve a counter of their own is a separate decision.
    """
    assert folded("tool.denied", "tool_call", "run-1:tool-1", run_ref="run-1") == (STIRRED,)


def test_a_tool_return_does_not_count_a_second_time() -> None:
    """`invoked` and `returned` are two events about one call.

    One call, one count - and two moments, both of which are evidence the
    run was alive, so both move the clock.
    """
    assert folded("tool.returned", "tool_call", "run-1:tool-1", run_ref="run-1") == (STIRRED,)
    assert folded("tool.failed", "tool_call", "run-1:tool-1", run_ref="run-1") == (STIRRED,)


def test_a_tool_call_with_no_run_folds_to_nothing() -> None:
    assert folded("tool.invoked", "tool_call", "tool-1") == ()


def test_a_tool_type_this_release_does_not_know_folds_to_nothing() -> None:
    """Including the clock. Storing what we do not understand is the promise;
    changing state from it is not, and a timestamp is state."""
    assert folded("tool.rate_limited", "tool_call", "run-1:tool-1", run_ref="run-1") == ()


def test_the_activity_clock_is_not_the_state_clock() -> None:
    """**The regression test for the obvious wrong implementation.**

    A1 could have been built by advancing `runs.last_event_at` on these
    six events instead of adding a column, and everything would have
    looked right until a run could not be closed: `_patch` compares
    `last_event_at` with `<=` against the incoming `occurred_at` on the
    same column it writes, so a step dated after a `run.finished` would
    make the finish fail its own guard and leave the run running for ever.

    So no mutation any of the six produces may name `last_event_at` on the
    run, and the three run-state events must still name it. Asserted over
    the fields rather than by eye, because this is exactly the kind of
    thing a later edit reintroduces.
    """
    activity = [
        folded("run.step_started", "run", "run-1", {"seq": 1}),
        folded("run.step_finished", "run", "run-1", {"seq": 1}),
        *(
            folded(f"tool.{name}", "tool_call", "run-1:tool-1", run_ref="run-1")
            for name in ("invoked", "returned", "failed", "denied")
        ),
    ]
    assert len(activity) == 6
    for mutations in activity:
        on_the_run = [m for m in mutations if m.selector == RunRef("run-1")]
        assert on_the_run, "each of the six says something about the run"
        for mutation in on_the_run:
            fields = (
                mutation.deltas if isinstance(mutation, Increment) else mutation.fields
            ).keys()
            assert "last_event_at" not in fields, (
                f"{mutation} moved the state clock, which is the bug this test exists for"
            )

    for event_type, payload in (
        ("run.started", {}),
        ("run.finished", {}),
        ("run.failed", {}),
    ):
        state = folded(event_type, "run", "run-1", payload)
        assert all("last_event_at" in m.fields for m in state), (
            f"{event_type} still moves the state clock, and must"
        )
        assert all("last_activity_at" not in m.fields for m in state), (
            f"{event_type} is a state change, not an activity report"
        )


def test_the_activity_clock_is_guarded_on_itself() -> None:
    """Monotonic by construction: a backlog cannot move it backwards.

    `not_before` on the column being written means a late event's patch
    matches no row, so the newest timestamp stands rather than the last one
    delivered. `unless` is deliberately absent - a late event about a
    settled run is still a true fact about when we heard it, and the status
    it must not touch is on a different patch entirely.
    """
    for mutations in (
        folded("run.step_started", "run", "run-1", {"seq": 1}),
        folded("tool.denied", "tool_call", "run-1:tool-1", run_ref="run-1"),
    ):
        (clock,) = [m for m in mutations if getattr(m, "fields", {}).get("last_activity_at")]
        assert clock.not_before == ("last_activity_at", WHEN)
        assert clock.unless is None


# --------------------------------------------------------------- the workflow ---


def test_an_execution_records_its_own_life_cycle() -> None:
    started = folded("workflow.execution_started", "workflow_execution", "exec-1")[0]
    assert started.fields == {
        "status": ExecutionStatus.RUNNING,
        "started_at": WHEN,
        "last_event_at": WHEN,
    }

    completed = folded(
        "workflow.execution_completed", "workflow_execution", "exec-1", {"output": {"n": 4}}
    )[0]
    assert completed.target is MutationTarget.WORKFLOW_EXECUTIONS
    assert completed.fields["status"] is ExecutionStatus.COMPLETED
    assert completed.fields["finished_at"] == WHEN
    assert completed.fields["output"] == {"n": 4}


def test_pausing_an_execution_does_not_finish_it() -> None:
    """`paused` is live, so writing `finished_at` would make a resumable
    execution look over."""
    patch = folded("workflow.execution_paused", "workflow_execution", "exec-1")[0]
    assert patch.fields == {"status": ExecutionStatus.PAUSED, "last_event_at": WHEN}
    # And `paused` is not in the settled set, so a resume can still move it.
    assert "paused" not in patch.unless[1]


def test_a_step_ref_splits_on_the_first_colon() -> None:
    """A step key is author-chosen and may contain a colon; an execution ref
    is server-generated and will not."""
    patch = folded("workflow.step_started", "execution_step", "exec-1:fan:out:3")[0]
    assert patch.selector == StepRef("exec-1", "fan:out:3")


def test_a_retry_clears_the_previous_failure() -> None:
    """A retried step is the same step. Leaving the old error on it would
    make a step that later succeeds still look broken."""
    patch = folded("workflow.step_retried", "execution_step", "exec-1:post", {"attempt": 2})[0]
    assert patch.fields == {
        "status": StepStatus.READY,
        "error": None,
        "finished_at": None,
        "attempt": 2,
        "last_event_at": WHEN,
    }


# --------------------------------------------------------------- the approval ---


def test_an_approval_needs_the_step_it_gates_to_be_named() -> None:
    """Its own ref identifies the approval, not the step. Guessing would open
    a gate on the wrong work."""
    assert folded("approval.granted", "approval", "appr-1", execution_ref="exec-1") == ()


def test_granting_an_approval_readies_the_step_and_dispatches_nothing() -> None:
    """The gate stays structural: `ready` is an invitation for the
    orchestrator to decide, not a decision."""
    mutations = folded(
        "approval.granted",
        "approval",
        "appr-1",
        execution_step_ref="exec-1:post",
        execution_ref="exec-1",
    )
    assert mutations == (
        Patch(
            StepRef("exec-1", "post"),
            {"status": StepStatus.READY, "last_event_at": WHEN},
            not_before=("last_event_at", WHEN),
            unless=_SETTLED_STEPS,
        ),
    )
    assert all(m.target is not MutationTarget.WORKFLOW_EXECUTIONS for m in mutations)


def test_a_requested_approval_shows_which_step_is_waiting() -> None:
    patch = folded("approval.requested", "approval", "appr-1", execution_step_ref="exec-1:post")[0]
    assert patch.fields == {"status": StepStatus.WAITING_APPROVAL, "last_event_at": WHEN}


@pytest.mark.parametrize("decision", ["denied", "expired"])
def test_a_refused_approval_fails_the_step_with_a_reason(decision: str) -> None:
    patch = folded(
        f"approval.{decision}",
        "approval",
        "appr-1",
        {"reason": "not this quarter"},
        execution_step_ref="exec-1:post",
    )[0]
    assert patch.fields["status"] is StepStatus.FAILED
    assert patch.fields["error"] == {
        "code": f"approval_{decision}",
        "message": "not this quarter",
    }


# ------------------------------------------------- the proposal's own example ---


def test_the_proposals_eleven_event_trace_folds_as_documented() -> None:
    """The MVP workflow, end to end, from `01-DOMAIN-AND-PROTOCOL.md` §9.

    Two claims are made about this trace in the proposal. The first is that
    re-folding it reproduces the final state - so the last mutation for each
    thing is asserted below. The second is that it sends no Slack message,
    which is why the assertion at the end is about what is *absent* from the
    whole fold rather than about any one event.
    """
    trace: list[tuple[str, str, str, dict[str, Any], dict[str, str]]] = [
        ("workflow.execution_started", "workflow_execution", "exec-9f2a", {}, {}),
        (
            "workflow.step_dispatched",
            "execution_step",
            "exec-9f2a:research",
            {"attempt": 0},
            {"execution_ref": "exec-9f2a"},
        ),
        (
            "workflow.step_started",
            "execution_step",
            "exec-9f2a:research",
            {},
            {"execution_ref": "exec-9f2a", "run_ref": "run-3f9a"},
        ),
        ("run.step_finished", "run", "run-3f9a", {"seq": 1, "kind": "llm"}, {}),
        (
            "tool.returned",
            "tool_call",
            "run-3f9a:tool-1",
            {"tool": "web_search"},
            {"run_ref": "run-3f9a"},
        ),
        (
            "workflow.step_succeeded",
            "execution_step",
            "exec-9f2a:research",
            {"tokens": 8421},
            {"execution_ref": "exec-9f2a"},
        ),
        (
            "approval.requested",
            "approval",
            "appr-21c8",
            {"title": "Post the brief?"},
            {"execution_ref": "exec-9f2a", "execution_step_ref": "exec-9f2a:post"},
        ),
        (
            "approval.granted",
            "approval",
            "appr-21c8",
            {"decided_by": "ada@example.com"},
            {"execution_ref": "exec-9f2a", "execution_step_ref": "exec-9f2a:post"},
        ),
        (
            "workflow.step_succeeded",
            "execution_step",
            "exec-9f2a:post",
            {"tool": "send_slack_message"},
            {"execution_ref": "exec-9f2a"},
        ),
        (
            "workflow.execution_completed",
            "workflow_execution",
            "exec-9f2a",
            {"steps_succeeded": 4},
            {},
        ),
    ]

    everything = [
        mutation
        for event_type, kind, ref, payload, correlation in trace
        for mutation in folded(event_type, kind, ref, payload, **correlation)
    ]

    def last_for(selector: Any) -> Any:
        return [m for m in everything if m.selector == selector][-1]

    assert last_for(ExecutionRef("exec-9f2a")).fields["status"] is ExecutionStatus.COMPLETED
    assert last_for(StepRef("exec-9f2a", "research")).fields["status"] is StepStatus.SUCCEEDED
    assert last_for(StepRef("exec-9f2a", "post")).fields["status"] is StepStatus.SUCCEEDED
    run_mutations = [m for m in everything if m.selector == RunRef("run-3f9a")]
    assert Increment(RunRef("run-3f9a"), {"step_count": 1}) in run_mutations
    # Since A1 the trace's last word about the run is the activity clock:
    # `tool.returned` moves it and counts nothing, which is the whole of
    # that event's contribution.
    assert last_for(RunRef("run-3f9a")) == Patch(
        RunRef("run-3f9a"),
        {"last_activity_at": WHEN},
        not_before=("last_activity_at", WHEN),
    )

    # The approval really gated: before it was granted, the `post` step was
    # waiting, and nothing had readied it.
    post_history = [m for m in everything if m.selector == StepRef("exec-9f2a", "post")]
    assert [m.fields["status"] for m in post_history] == [
        StepStatus.WAITING_APPROVAL,
        StepStatus.READY,
        StepStatus.SUCCEEDED,
    ]

    # And promise 7, on the whole trace: folding it asks for no dispatch, no
    # provider call and no Slack message, because the vocabulary has no way
    # to express one.
    assert {mutation.target for mutation in everything} <= {
        MutationTarget.WORKFLOW_EXECUTIONS,
        MutationTarget.EXECUTION_STEPS,
        MutationTarget.RUNS,
        MutationTarget.RUN_STEPS,
    }


def test_every_folding_event_type_produces_something_for_a_plausible_event() -> None:
    """A type declared as folding, that folds to nothing for a well-formed
    event, is a reducer somebody forgot to write."""
    plausible: dict[str, dict[str, Any]] = {
        "agent.status_changed": {"status": "working"},
        "run.step_started": {"seq": 1},
        "run.step_finished": {"seq": 1},
        "task.progress": {"percent": 10},
    }
    refs = {
        "execution_step": "exec-1:post",
        "tool_call": "run-1:tool-1",
    }
    correlations = {
        "approval": {"execution_step_ref": "exec-1:post"},
        "tool_call": {"run_ref": "run-1"},
    }

    missing = []
    for event_type, spec in KNOWN_EVENTS.items():
        if not spec.folds_into:
            continue
        kind = spec.subjects[0].value
        if not folded(
            event_type,
            kind,
            refs.get(kind, "r-1"),
            plausible.get(event_type, {}),
            **correlations.get(kind, {}),
        ):
            missing.append(event_type)

    # Empty since A1. These three declared `runs` in the taxonomy and folded
    # into nothing, which this assertion recorded rather than fixed: only
    # `tool.invoked` changed the counter and nothing else about the run. They
    # now move the activity clock, so every type that says it folds does.
    assert missing == []


def test_every_patch_that_moves_current_state_is_guarded() -> None:
    """A patch with no guard is one a stale event can win.

    Checked over every folding type rather than per reducer, because the
    reducer most likely to forget is the one written next.

    This test used to exempt `runs`, `run_steps`, `workflow_executions` and
    `execution_steps` on the grounds that their events come from the
    orchestrator rather than from a worker flushing a backlog. The exemption
    was wrong twice over: a `dispatchable` agent reports its own step
    outcomes, so those events *do* come over ingest; and `advance()` reads a
    step's status to decide what happens next, so a reverted status is a
    re-dispatch rather than a wrong colour. Migration 0017 gave all four the
    column, and the list below is now empty rather than populated.
    """
    unguarded: list[str] = []
    for event_type, spec in KNOWN_EVENTS.items():
        if not spec.folds_into:
            continue
        kind = spec.subjects[0].value
        refs = {"execution_step": "exec-1:post", "tool_call": "run-1:tool-1"}
        correlations = {
            "approval": {"execution_step_ref": "exec-1:post"},
            "tool_call": {"run_ref": "run-1"},
        }
        plausible = {
            "agent.status_changed": {"status": "working"},
            "run.step_started": {"seq": 1},
            "run.step_finished": {"seq": 1},
            "task.progress": {"percent": 10},
        }
        for mutation in folded(
            event_type,
            kind,
            refs.get(kind, "r-1"),
            plausible.get(event_type, {}),
            **correlations.get(kind, {}),
        ):
            if isinstance(mutation, Patch) and mutation.not_before is None:
                unguarded.append(f"{event_type} -> {mutation.target.value}")

    assert unguarded == [], f"these patches can be overwritten by a stale event: {unguarded}"


def test_only_a_retry_may_reopen_a_settled_step() -> None:
    """The monotonicity guard, and its one deliberate exception.

    A timestamp guard is not enough on its own: `not_before` compares with
    `<=`, so two events stamped with the same `occurred_at` both apply and the
    last one wins. `Patch.unless` closes that by refusing to touch a status
    that is already final - except for `workflow.step_retried`, which exists
    to reopen one.
    """
    unprotected: list[str] = []
    for event_type, spec in KNOWN_EVENTS.items():
        if "execution_steps" not in spec.folds_into:
            continue
        kind = spec.subjects[0].value
        for mutation in folded(
            event_type,
            kind,
            "exec-1:post",
            {},
            **({"execution_step_ref": "exec-1:post"} if kind == "approval" else {}),
        ):
            if (
                isinstance(mutation, Patch)
                and mutation.target is MutationTarget.EXECUTION_STEPS
                and mutation.unless is None
            ):
                unprotected.append(event_type)

    assert unprotected == ["workflow.step_retried"], (
        "exactly one step patch may reopen a settled step, and it is the retry"
    )


def test_a_retry_keeps_its_timestamp_guard() -> None:
    """Dropping `unless` must not have dropped the other guard with it."""
    (patch,) = folded("workflow.step_retried", "execution_step", "exec-1:post", {"attempt": 2})
    assert patch.unless is None
    assert patch.not_before == ("last_event_at", WHEN)


def test_the_settled_sets_name_the_right_enum() -> None:
    """`run_steps.status` is `run_status`, not `step_status`.

    A status string from the wrong enum would never match, so the guard would
    be silently inert - the failure mode this whole work item exists to stop.
    """
    assert _SETTLED_RUN_STEPS == _SETTLED_RUNS
    assert "skipped" not in _SETTLED_RUN_STEPS[1]
    assert "skipped" in _SETTLED_STEPS[1]
    assert "paused" not in _SETTLED_EXECUTIONS[1]
    assert "completed" in _SETTLED_EXECUTIONS[1]

"""D4.3 A1: `runs.last_activity_at`, through real ingest.

Six event types now say "something was heard about this run" without saying
anything about its state. The separation is the whole stage, and these tests
check both halves of it: the new column moves, and the old one does not.

**The test that explains why there are two columns is
`test_a_step_dated_after_the_finish_cannot_stop_the_run_settling`.** `_patch`
compares `last_event_at` with `<=` against the incoming `occurred_at` on the
same column it writes, so had the six advanced that column instead, a step
dated after a `run.finished` would make the finish fail its own guard and
leave the run `running` for ever - the exact condition stage D4 exists to
detect. That test fails against the one-column implementation and passes
against this one.

Instants come from the `times` fixture rather than module scope, for the
reason `test_quiet_runs_api.py` records: a constant evaluated at import
drifts by however long the suite takes to reach this file.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_ingest import Fixture, event, post

from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Times:
    """Four instants in order, so "earlier" and "later" are never in doubt."""

    opened: dt.datetime
    early: dt.datetime
    middle: dt.datetime
    late: dt.datetime


@pytest.fixture
def times() -> Times:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    return Times(
        opened=now - dt.timedelta(hours=3),
        early=now - dt.timedelta(hours=2),
        middle=now - dt.timedelta(hours=1),
        late=now - dt.timedelta(minutes=30),
    )


@pytest.fixture
def ingest(client: TestClient, actor: Actor) -> Fixture:
    return Fixture(client, actor)


@pytest.fixture
def agent(client: TestClient, actor: Actor, ingest: Fixture) -> Any:
    created = client.post(
        f"/v1/projects/{ingest.project_id}/agents",
        json={
            "display_name": "Researcher",
            "agent_key": "researcher",
            "interaction_mode": "dispatchable",
            "dispatch": {"transport": "webhook", "url": "https://example.test/hook"},
        },
        headers=actor.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


# ------------------------------------------------------------------ helpers ---

REF = "run-1"


def run_event(
    name: str, *, when: dt.datetime, dedup_key: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    return event(
        dedup_key=dedup_key,
        type=f"run.{name}",
        subject={"kind": "run", "ref": REF},
        correlation={"agent_key": "researcher"},
        occurred_at=when.isoformat(),
        payload=payload or {},
    )


def tool_event(name: str, *, when: dt.datetime, dedup_key: str) -> dict[str, Any]:
    return event(
        dedup_key=dedup_key,
        type=f"tool.{name}",
        subject={"kind": "tool_call", "ref": f"{REF}:{dedup_key}"},
        correlation={"agent_key": "researcher", "run_ref": REF},
        occurred_at=when.isoformat(),
    )


def opened(times: Times) -> dict[str, Any]:
    return run_event("started", when=times.opened, dedup_key="opened")


#: The six, each as a one-event factory. `run.step_finished` needs its step to
#: exist for the step patch to land, but the clock patch beside it does not
#: care - which is itself worth knowing, so the pair is sent together only
#: where the step's own state is under test.
ACTIVITY: dict[str, Any] = {
    "run.step_started": lambda when: run_event(
        "step_started", when=when, dedup_key="act", payload={"seq": 0, "name": "turn"}
    ),
    "run.step_finished": lambda when: run_event(
        "step_finished", when=when, dedup_key="act", payload={"seq": 0}
    ),
    "tool.invoked": lambda when: tool_event("invoked", when=when, dedup_key="act"),
    "tool.returned": lambda when: tool_event("returned", when=when, dedup_key="act"),
    "tool.failed": lambda when: tool_event("failed", when=when, dedup_key="act"),
    "tool.denied": lambda when: tool_event("denied", when=when, dedup_key="act"),
}


async def stored(database: Database, project_id: Any) -> dict[str, Any]:
    async with database.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT run_ref, status, last_event_at, last_activity_at, "
                    "       started_at, finished_at, step_count, tool_call_count "
                    "  FROM runs WHERE project_id = :p"
                ),
                {"p": project_id},
            )
        ).mappings()
        rows = [dict(one) for one in row]
    assert len(rows) == 1, f"expected exactly one run, got {len(rows)}"
    return rows[0]


# ----------------------------------------------------------- the six move it ---


@pytest.mark.parametrize("event_type", sorted(ACTIVITY))
async def test_each_of_the_six_events_moves_the_clock(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
    event_type: str,
) -> None:
    """One run, one activity event, one assertion - six times over.

    The value is the event's own `occurred_at` and not the moment it was
    ingested, which matters for a backlog: a worker that was offline reports
    when things happened, not when it managed to say so.
    """
    assert post(client, ingest, opened(times)).status_code == 202
    assert post(client, ingest, ACTIVITY[event_type](times.middle)).status_code == 202

    row = await stored(owner_database, ingest.project_id)
    assert row["last_activity_at"] == times.middle, f"{event_type} should move the clock"


async def test_a_run_nobody_has_reported_on_has_no_activity_clock(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """Nullable, and null means exactly what it says.

    `run.started` is a state change rather than activity, so it leaves this
    column alone - and migration 0025 backfilled nothing, so every row
    written before it looks the same way. Readers coalesce.
    """
    assert post(client, ingest, opened(times)).status_code == 202

    row = await stored(owner_database, ingest.project_id)
    assert row["last_activity_at"] is None
    assert row["last_event_at"] == times.opened


# -------------------------------------------------- the counters are untouched ---


async def test_a_denied_tool_call_moves_the_clock_and_leaves_the_counter_alone(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """A refused call is evidence the run is alive, and is not a call.

    `tool_call_count` names calls that were made; a denied call never ran.
    Stage A1 makes "a blocked agent is not an idle one" true by the clock
    instead of by the counter, which is the half of it the data supports.
    """
    assert (
        post(
            client,
            ingest,
            opened(times),
            tool_event("denied", when=times.middle, dedup_key="d1"),
            tool_event("denied", when=times.late, dedup_key="d2"),
        ).status_code
        == 202
    )

    row = await stored(owner_database, ingest.project_id)
    assert row["last_activity_at"] == times.late
    assert row["tool_call_count"] == 0, "denials are not calls, and this stage does not count them"
    assert row["step_count"] == 0


async def test_a_return_and_a_failure_move_the_clock_without_counting_again(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """One call, one count, three moments.

    `tool.invoked`, `tool.returned` and `tool.failed` are events about calls
    that `invoked` already counted - counting the other two would count one
    call three times.
    """
    assert (
        post(
            client,
            ingest,
            opened(times),
            tool_event("invoked", when=times.early, dedup_key="i1"),
            tool_event("returned", when=times.middle, dedup_key="r1"),
            tool_event("failed", when=times.late, dedup_key="f1"),
        ).status_code
        == 202
    )

    row = await stored(owner_database, ingest.project_id)
    assert row["tool_call_count"] == 1, "one invocation, one count"
    assert row["last_activity_at"] == times.late


async def test_the_counters_are_what_they_were(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """A mixed stream, and the two counters unchanged by the new mutation.

    Two steps finished and two calls invoked, among six activity events. If
    the clock patch had been written as a counter, or beside one, this is
    where it would show.
    """
    assert (
        post(
            client,
            ingest,
            opened(times),
            run_event("step_started", when=times.early, dedup_key="s0a", payload={"seq": 0}),
            run_event("step_finished", when=times.early, dedup_key="s0b", payload={"seq": 0}),
            run_event("step_started", when=times.middle, dedup_key="s1a", payload={"seq": 1}),
            run_event("step_finished", when=times.middle, dedup_key="s1b", payload={"seq": 1}),
            tool_event("invoked", when=times.middle, dedup_key="i1"),
            tool_event("invoked", when=times.late, dedup_key="i2"),
            tool_event("denied", when=times.late, dedup_key="d1"),
        ).status_code
        == 202
    )

    row = await stored(owner_database, ingest.project_id)
    assert row["step_count"] == 2
    assert row["tool_call_count"] == 2
    assert row["last_activity_at"] == times.late


# ------------------------------------------------- the state clock is untouched ---


async def test_step_events_do_not_touch_the_state_clock(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """**Half of the regression test for the wrong implementation.**

    Had A1 advanced `last_event_at`, this run's state clock would read an
    hour ago instead of three. It is the guard `run.finished` compares
    against, and moving it is what makes a run unclosable.
    """
    assert (
        post(
            client,
            ingest,
            opened(times),
            run_event("step_started", when=times.middle, dedup_key="s0a", payload={"seq": 0}),
            run_event("step_finished", when=times.middle, dedup_key="s0b", payload={"seq": 0}),
        ).status_code
        == 202
    )

    row = await stored(owner_database, ingest.project_id)
    assert row["last_event_at"] == times.opened, "the state clock has not moved"
    assert row["last_activity_at"] == times.middle, "and the activity clock has"


async def test_tool_events_do_not_touch_the_state_clock(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """The other half, for the namespace that moves no step row either."""
    assert (
        post(
            client,
            ingest,
            opened(times),
            tool_event("invoked", when=times.middle, dedup_key="i1"),
            tool_event("denied", when=times.late, dedup_key="d1"),
        ).status_code
        == 202
    )

    row = await stored(owner_database, ingest.project_id)
    assert row["last_event_at"] == times.opened
    assert row["last_activity_at"] == times.late


# ------------------------------------------------------------ order and finality ---


async def test_a_late_event_cannot_move_the_clock_backwards(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """A backlog arrives in send order, not in the order things happened.

    The newest timestamp stands rather than the last one delivered, which is
    what `not_before` on the column being written buys.
    """
    assert (
        post(
            client,
            ingest,
            opened(times),
            tool_event("invoked", when=times.late, dedup_key="newest"),
        ).status_code
        == 202
    )
    assert await stored(owner_database, ingest.project_id) is not None

    assert (
        post(
            client, ingest, tool_event("returned", when=times.early, dedup_key="stale")
        ).status_code
        == 202
    )

    row = await stored(owner_database, ingest.project_id)
    assert row["last_activity_at"] == times.late, "the stale event changed nothing"


async def test_a_step_dated_after_the_finish_cannot_stop_the_run_settling(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """**The reason `last_activity_at` is a second column.**

    This run's newest step is dated an hour ago and its `run.finished` two
    hours ago - a producer whose step reporter and run reporter disagree
    about the clock, which is all it takes. `_patch` compares
    `last_event_at` with `<=` against the incoming `occurred_at` on the same
    column it writes, so if the step had advanced `last_event_at` to
    `times.middle`, the finish carrying `times.early` would match no row and
    this run would be `running` for ever - the condition stage D4 exists to
    find.

    Against the one-column implementation this test fails. Against this one
    the finish lands, and the activity clock keeps the later timestamp
    because it is a different question.
    """
    assert (
        post(
            client,
            ingest,
            opened(times),
            run_event("step_started", when=times.middle, dedup_key="s0a", payload={"seq": 0}),
            run_event("step_finished", when=times.middle, dedup_key="s0b", payload={"seq": 0}),
            run_event("finished", when=times.early, dedup_key="done"),
        ).status_code
        == 202
    )

    row = await stored(owner_database, ingest.project_id)
    assert row["status"] == "succeeded", "the run settled, which is the whole point"
    assert row["finished_at"] == times.early
    assert row["last_event_at"] == times.early
    assert row["last_activity_at"] == times.middle, (
        "and the activity clock still holds the newer step, which is a different question"
    )


async def test_late_activity_on_a_settled_run_moves_only_the_clock(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """A settled status is final; hearing something later is still a fact.

    The clock patch carries no `unless`, deliberately: it says when we last
    heard about the run, not what the run is. The status and the state clock
    the run-level patches protect are untouched by it.

    The counter moving here is pre-existing behaviour, not something A1
    introduced: `Increment` has no guards at all, so a settled run's
    counters have always been able to rise. Asserted rather than quietly
    tolerated, so a change to it is visible.
    """
    assert (
        post(
            client,
            ingest,
            opened(times),
            run_event("finished", when=times.early, dedup_key="done"),
        ).status_code
        == 202
    )

    assert (
        post(
            client, ingest, tool_event("invoked", when=times.late, dedup_key="afterwards")
        ).status_code
        == 202
    )

    row = await stored(owner_database, ingest.project_id)
    assert row["status"] == "succeeded", "still settled"
    assert row["last_event_at"] == times.early, "and its state clock has not moved"
    assert row["last_activity_at"] == times.late, "but we did hear something, and say so"
    assert row["tool_call_count"] == 1, "pre-existing: `Increment` carries no guard"

"""D4.2a over HTTP: runs that have reported nothing recently.

Everything here goes through real ingest, because the whole point of the
stage is which clock the query trusts and only real events move the real
clocks. Two exceptions are seeded with direct SQL and say so: a `cancelled`
run, which no event type can produce, and a run with no clock at all, which
the R1 producer cannot produce either - that is what the test beside it
proves.

**The test that justifies the stage is the first one.** Three runs opened
three hours ago, each reporting a step a minute ago. `runs.last_event_at`
still says three hours for all three, so a query trusting that column alone
would call every one of them quiet. The audit measured that shape at 290
false candidates out of 300 running runs; this asserts zero.

**Every instant comes from the `times` fixture, never from module scope.**
The endpoint judges against the server's clock at the moment of the request,
so a constant computed at import time drifts by however long the suite takes
to reach this file - half an hour in a full run, which is enough to make a
"busy" run look quiet and the headline test fail for a reason that has
nothing to do with the code. It did, once. The fixture is evaluated per
test, so the gap between the events and the request is always seconds.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_ingest import KEY_HEADER, Fixture, event, post, post_with

from agentworld.domain.enums import ActivityEvidence
from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration

#: Ten minutes. Long enough that `times.just_now` is never quiet, short
#: enough that `times.quiet_since` always is.
THRESHOLD = 600


@dataclass(frozen=True)
class Times:
    """One instant per test, and the three offsets every case is built from."""

    now: dt.datetime
    #: Comfortably past any threshold here, so "it opened long ago" is never
    #: the reason a run is or is not listed.
    opened: dt.datetime
    #: Long silent: listed under every threshold these tests use.
    quiet_since: dt.datetime
    #: Inside every threshold: a run whose newest evidence is this recent is
    #: working, whatever its own clock says.
    just_now: dt.datetime


@pytest.fixture
def times() -> Times:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    return Times(
        now=now,
        opened=now - dt.timedelta(hours=3),
        quiet_since=now - dt.timedelta(minutes=90),
        just_now=now - dt.timedelta(minutes=1),
    )


@pytest.fixture
def ingest(client: TestClient, actor: Actor) -> Fixture:
    return Fixture(client, actor)


@pytest.fixture
def agent(client: TestClient, actor: Actor, ingest: Fixture) -> Any:
    """One dispatchable agent, created through the API as a person would."""
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


def run_event(
    name: str, *, ref: str, when: dt.datetime, dedup_key: str, **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "dedup_key": dedup_key,
        "type": f"run.{name}",
        "subject": {"kind": "run", "ref": ref},
        "correlation": {"agent_key": "researcher"},
        "occurred_at": when.isoformat(),
    }
    body.update(overrides)
    return event(**body)


def tool_call(*, ref: str, when: dt.datetime, dedup_key: str) -> dict[str, Any]:
    """One `tool.invoked`, which moves a counter and no clock at all."""
    return event(
        dedup_key=dedup_key,
        type="tool.invoked",
        subject={"kind": "tool_call", "ref": f"{ref}:{dedup_key}"},
        correlation={"agent_key": "researcher", "run_ref": ref},
        occurred_at=when.isoformat(),
    )


def open_run(ref: str, *, at: dt.datetime) -> dict[str, Any]:
    return run_event("started", ref=ref, when=at, dedup_key=f"{ref}-open")


def step(ref: str, *, at: dt.datetime, seq: int = 0) -> list[dict[str, Any]]:
    """A started-and-finished step, so the counter moves as well as the clock."""
    return [
        run_event(
            "step_started",
            ref=ref,
            when=at,
            dedup_key=f"{ref}-s{seq}-start",
            payload={"seq": seq, "name": f"turn-{seq}"},
        ),
        run_event(
            "step_finished",
            ref=ref,
            when=at,
            dedup_key=f"{ref}-s{seq}-end",
            payload={"seq": seq},
        ),
    ]


def quiet_runs(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    *,
    seconds: int | None = THRESHOLD,
    limit: int | None = None,
    project_id: Any = None,
) -> Any:
    params: dict[str, Any] = {}
    if seconds is not None:
        params["quiet_for_seconds"] = seconds
    if limit is not None:
        params["limit"] = limit
    return client.get(
        f"/v1/projects/{project_id or ingest.project_id}/runs",
        params=params,
        headers=actor.headers,
    )


def listed(response: Any) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    return [dict(row) for row in response.json()]


async def run_clocks(database: Database, project_id: Any) -> list[dict[str, Any]]:
    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT run_ref, status, last_event_at, step_count, tool_call_count "
                    "  FROM runs WHERE project_id = :p ORDER BY run_ref"
                ),
                {"p": project_id},
            )
        ).mappings()
        return [dict(row) for row in rows]


# ------------------------------------------------------- the case for the stage ---


async def test_a_busy_run_is_not_quiet(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """**Three busy runs, and none of them is listed.**

    Each opened three hours ago and reported a step a minute ago. The SQL
    assertion in the middle is the point: `runs.last_event_at` is still
    three hours old for every one of them, because that column moves on
    `run.started`, `run.finished` and `run.failed` and on nothing else. A
    query trusting it alone would call all three quiet.
    """
    events: list[dict[str, Any]] = []
    for index in range(3):
        ref = f"busy-{index}"
        events.append(open_run(ref, at=times.opened))
        events.extend(step(ref, at=times.just_now))
    assert post(client, ingest, *events).status_code == 202

    stored = await run_clocks(owner_database, ingest.project_id)
    assert len(stored) == 3
    assert all(row["last_event_at"] == times.opened for row in stored), (
        "the run-level clock has not moved since they opened, which is the trap"
    )

    assert listed(quiet_runs(client, actor, ingest)) == [], "all three are working"


async def test_a_run_that_stopped_reporting_is_quiet(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times
) -> None:
    """Steps, then ninety minutes of silence."""
    assert (
        post(
            client,
            ingest,
            open_run("stalled", at=times.opened),
            *step("stalled", at=times.quiet_since),
        ).status_code
        == 202
    )

    (row,) = listed(quiet_runs(client, actor, ingest))
    assert row["run_ref"] == "stalled"
    assert row["status"] == "running"
    assert row["agent_id"] == agent["id"]
    assert row["activity_evidence"] == ActivityEvidence.TRACE_STEP
    assert row["last_activity_at"].startswith(times.quiet_since.isoformat()[:16])
    assert row["seconds_since_activity"] >= 90 * 60
    assert row["step_count"] == 1
    assert row["has_untimed_activity"] is False


# ---------------------------------------------------------------- what is excluded ---


@pytest.mark.parametrize("ending", ["finished", "failed"])
async def test_a_settled_run_is_never_quiet(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times, ending: str
) -> None:
    """It ended three hours ago and is not waiting for anything."""
    assert (
        post(
            client,
            ingest,
            open_run("done", at=times.opened),
            run_event(ending, ref="done", when=times.opened, dedup_key=f"done-{ending}"),
        ).status_code
        == 202
    )

    assert listed(quiet_runs(client, actor, ingest)) == []


async def test_a_cancelled_run_is_never_quiet(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """Seeded, because no event type produces `cancelled`.

    `run.started`, `run.finished` and `run.failed` are the whole run
    namespace, so this status can only arrive by another route - which is
    exactly why the filter is on status rather than on "did we see an
    ending event".
    """
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO runs (id, workspace_id, project_id, agent_id, run_ref, "
                "status, started_at, last_event_at) VALUES "
                "(:i, :w, :p, :a, 'cancelled-one', 'cancelled', :t, :t)"
            ),
            {
                "i": uuid.uuid4(),
                "w": ingest.workspace_id,
                "p": ingest.project_id,
                "a": uuid.UUID(str(agent["id"])),
                "t": times.opened,
            },
        )

    assert listed(quiet_runs(client, actor, ingest)) == []


# ----------------------------------------------------------------- the evidence ---


async def test_a_run_with_no_steps_is_quiet_on_its_own_clock(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times
) -> None:
    """Opened and never heard from again."""
    assert post(client, ingest, open_run("opened-only", at=times.opened)).status_code == 202

    (row,) = listed(quiet_runs(client, actor, ingest))
    assert row["activity_evidence"] == ActivityEvidence.RUN_EVENT
    assert row["step_count"] == 0
    assert row["tool_call_count"] == 0
    assert row["has_untimed_activity"] is False


async def test_a_tool_only_run_is_flagged_rather_than_presented_as_silent(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times
) -> None:
    """**The honesty this endpoint owes a reader.**

    This agent invoked three tools a minute ago and reported no steps.
    `tool.invoked` moves a counter and no clock, so every timestamp here is
    three hours old and the run is listed as quiet - which is as much as the
    data supports. What must not happen is the row reading as proof that
    nothing happened, and `has_untimed_activity` is what prevents it.
    """
    assert (
        post(
            client,
            ingest,
            open_run("tools-only", at=times.opened),
            *[
                tool_call(ref="tools-only", when=times.just_now, dedup_key=f"t{index}")
                for index in range(3)
            ],
        ).status_code
        == 202
    )

    (row,) = listed(quiet_runs(client, actor, ingest))
    assert row["tool_call_count"] == 3
    assert row["activity_evidence"] == ActivityEvidence.RUN_EVENT, "tool calls move no clock"
    assert row["has_untimed_activity"] is True, (
        "counted work that cannot be dated, and the row says so"
    )


async def test_a_dated_trace_outranks_an_undated_counter(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times
) -> None:
    """Tool calls and a newer step: listed, but not flagged.

    The flag is not "this run made tool calls" - `tool_call_count` is 2
    here and says so. It is "counted tool work is the newest thing we
    know", and a step timestamp beats a counter.
    """
    earlier = times.quiet_since - dt.timedelta(minutes=5)
    assert (
        post(
            client,
            ingest,
            open_run("tools-and-steps", at=times.opened),
            tool_call(ref="tools-and-steps", when=earlier, dedup_key="ta"),
            tool_call(ref="tools-and-steps", when=earlier, dedup_key="tb"),
            *step("tools-and-steps", at=times.quiet_since),
        ).status_code
        == 202
    )

    (row,) = listed(quiet_runs(client, actor, ingest))
    assert row["tool_call_count"] == 2
    assert row["activity_evidence"] == ActivityEvidence.TRACE_STEP
    assert row["has_untimed_activity"] is False


async def test_steps_older_than_the_run_clock_are_still_run_event(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times
) -> None:
    """**`trace_step` does not mean "has steps".**

    A second `run.started`, sent later, patches the run's own clock past
    everything its trace knows. The run has a completed step and the
    evidence is `run_event`, because the field names the newest timestamp
    rather than the existence of a trace. A client reading it the other way
    round will be wrong about this row.
    """
    later = times.quiet_since + dt.timedelta(minutes=10)
    assert (
        post(
            client,
            ingest,
            open_run("restarted", at=times.opened),
            *step("restarted", at=times.quiet_since),
            run_event("started", ref="restarted", when=later, dedup_key="restarted-again"),
        ).status_code
        == 202
    )

    (row,) = listed(quiet_runs(client, actor, ingest))
    assert row["step_count"] == 1, "it has a trace"
    assert row["activity_evidence"] == ActivityEvidence.RUN_EVENT, "and the trace is not newest"
    assert row["last_activity_at"].startswith(later.isoformat()[:16])


async def test_an_older_trace_with_tool_calls_is_run_event_and_flagged(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times
) -> None:
    """The one combination the other cases leave out: row 8 of the table.

    A trace *and* tool calls *and* a run-level clock newer than both. Each
    of those three is pinned elsewhere - `trace_step` does not mean "has
    steps" in the test above, the flag is pinned by the tool-only case, and
    a newer trace outranking a counter by the case before that - but their
    intersection is where the two rules could quietly contradict each
    other. The evidence has to name the newest timestamp (the run's own)
    *and* the flag has to fire (nothing dated is newer than it), and
    nothing in the implementation couples them.

    Kept separate from the test above rather than folded into it, so that
    one keeps pinning `trace_step`-is-not-"has-steps" with no tool calls in
    the way.
    """
    later = times.quiet_since + dt.timedelta(minutes=10)
    assert (
        post(
            client,
            ingest,
            open_run("tooled-restart", at=times.opened),
            *step("tooled-restart", at=times.quiet_since),
            tool_call(ref="tooled-restart", when=times.quiet_since, dedup_key="tr-a"),
            tool_call(ref="tooled-restart", when=times.quiet_since, dedup_key="tr-b"),
            run_event("started", ref="tooled-restart", when=later, dedup_key="tooled-again"),
        ).status_code
        == 202
    )

    (row,) = listed(quiet_runs(client, actor, ingest))
    assert row["activity_evidence"] == ActivityEvidence.RUN_EVENT, (
        "the run's own clock is newer than anything its trace knows"
    )
    assert row["has_untimed_activity"] is True, (
        "and nothing dated is newer than it, so the counted tool work is all there is"
    )
    assert row["tool_call_count"] == 2, "counted through ingest, not written by hand"
    assert row["step_count"] == 1, "one step finished; `step_started` alone would count none"
    assert row["last_activity_at"].startswith(later.isoformat()[:16]), (
        "the newer run-level timestamp, not the trace's"
    )
    assert row["seconds_since_activity"] >= 80 * 60


async def test_ingest_never_leaves_a_run_without_a_clock(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """Why `activity_evidence: none` is unreachable through the producer.

    `_append_run` writes `last_event_at` in the same INSERT that creates
    the row, so a run folded from events always has one. This asserts that
    against the database rather than restating it, which is what makes
    `none` a branch for seeded rows rather than a value nobody has thought
    about.
    """
    assert (
        post(
            client,
            ingest,
            open_run("complete", at=times.opened),
            *step("complete", at=times.quiet_since),
            tool_call(ref="complete", when=times.quiet_since, dedup_key="tc"),
        ).status_code
        == 202
    )

    stored = await run_clocks(owner_database, ingest.project_id)
    assert [row["last_event_at"] is not None for row in stored] == [True]
    assert (
        listed(quiet_runs(client, actor, ingest))[0]["activity_evidence"] != ActivityEvidence.NONE
    )


async def test_a_directly_seeded_run_with_no_clock_reports_none(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    times: Times,
    owner_database: Database,
) -> None:
    """The branch the test above proves ingest cannot reach.

    Rows like this exist: `test_agent_work.py` seeds `runs` directly, and a
    run written before the R1 producer landed would look like one. The
    fallback is `started_at` and no timestamp is invented.
    """
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO runs (id, workspace_id, project_id, agent_id, run_ref, "
                "status, tool_call_count, started_at) VALUES "
                "(:i, :w, :p, :a, 'no-clock', 'running', 2, :t)"
            ),
            {
                "i": uuid.uuid4(),
                "w": ingest.workspace_id,
                "p": ingest.project_id,
                "a": uuid.UUID(str(agent["id"])),
                "t": times.opened,
            },
        )

    (row,) = listed(quiet_runs(client, actor, ingest))
    assert row["activity_evidence"] == ActivityEvidence.NONE
    assert row["last_activity_at"].startswith(times.opened.isoformat()[:16])
    assert row["has_untimed_activity"] is True, "two tool calls counted, nothing dated"


# -------------------------------------------------------- order, limit, validation ---


async def test_the_quietest_come_first_and_the_limit_bounds_the_page(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times
) -> None:
    """Three silences of different lengths, longest first."""
    events: list[dict[str, Any]] = []
    for index, minutes in enumerate((30, 120, 75)):
        ref = f"quiet-{index}"
        events.append(open_run(ref, at=times.now - dt.timedelta(minutes=minutes + 60)))
        events.extend(step(ref, at=times.now - dt.timedelta(minutes=minutes)))
    assert post(client, ingest, *events).status_code == 202

    every = listed(quiet_runs(client, actor, ingest))
    assert [row["run_ref"] for row in every] == ["quiet-1", "quiet-2", "quiet-0"]
    assert [row["run_ref"] for row in listed(quiet_runs(client, actor, ingest, limit=2))] == [
        "quiet-1",
        "quiet-2",
    ]


async def test_the_threshold_is_required(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any
) -> None:
    """No default patience, deliberately: a caller has to say what quiet means."""
    assert quiet_runs(client, actor, ingest, seconds=None).status_code == 422


@pytest.mark.parametrize(
    ("seconds", "limit"),
    [(0, None), (-1, None), (2_592_001, None), (THRESHOLD, 0), (THRESHOLD, 201)],
)
async def test_the_bounds_are_enforced(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    agent: Any,
    seconds: int,
    limit: int | None,
) -> None:
    assert quiet_runs(client, actor, ingest, seconds=seconds, limit=limit).status_code == 422


# -------------------------------------------------------------------- isolation ---


async def test_another_projects_quiet_run_is_absent(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, times: Times
) -> None:
    """Two projects in one workspace, each with a quiet run of its own.

    Not a second `Fixture`: its workspace is named "Ingest", a workspace
    name is a slug, and a slug is unique - so the second one is refused.
    The second project gets its own ingest key, because a key is pinned to
    one project and that pinning is half of what is being tested.
    """
    other = client.post(
        f"/v1/workspaces/{ingest.workspace_id}/projects",
        json={"name": "Mainland"},
        headers=actor.headers,
    )
    assert other.status_code == 201, other.text
    other_id = other.json()["id"]
    other_agent = client.post(
        f"/v1/projects/{other_id}/agents",
        json={
            "display_name": "Researcher",
            "agent_key": "researcher",
            "interaction_mode": "dispatchable",
            "dispatch": {"transport": "webhook", "url": "https://example.test/hook"},
        },
        headers=actor.headers,
    )
    assert other_agent.status_code == 201, other_agent.text
    minted = client.post(
        f"/v1/workspaces/{ingest.workspace_id}/api-keys",
        json={"name": "Other", "scope": "ingest", "project_id": other_id},
        headers=actor.headers,
    )
    assert minted.status_code == 201, minted.text

    assert post(client, ingest, open_run("mine", at=times.opened)).status_code == 202
    theirs = post_with(
        client,
        {KEY_HEADER: str(minted.json()["key"])},
        open_run("theirs", at=times.opened),
    )
    assert theirs.status_code == 202, theirs.text

    assert [row["run_ref"] for row in listed(quiet_runs(client, actor, ingest))] == ["mine"]
    assert [
        row["run_ref"] for row in listed(quiet_runs(client, actor, ingest, project_id=other_id))
    ] == ["theirs"]


async def test_an_unknown_project_is_not_found(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    assert quiet_runs(client, actor, ingest, project_id=uuid.uuid4()).status_code == 404

"""R1: the event that brings an external agent's run into being.

Until this stage `runs` was a table the reducers patched and nothing
inserted, so `GET /v1/projects/{id}/agents/{id}/runs` was an empty list for
every agent in every project. `run.started` now creates the row, in the same
transaction as the event it was folded from.

Everything here goes through real ingest over HTTP. The rows are never seeded
- seeding would prove the read works, which D2 already did, and say nothing
about the producer. The only direct SQL is reading back what ingest wrote.

Two properties are worth naming before the tests:

**The owner is resolved once.** `agent_id` is NOT NULL on `runs`, and it
comes from the `ResolvedRefs` the ingest transaction already built for the
event row - not a second lookup - so the event and the run cannot disagree
about whose work they describe. An unresolvable reference writes no run and
fails nothing, which is what every other unresolved reference already does.

**The id is a function of the reference.** `run_id_for(project_id, run_ref)`
rather than `gen_random_uuid()`, so refolding the stored log rebuilds the
same rows rather than new ones with new ids - and `run_steps.run_id` keeps
pointing at the run it pointed at before. `test_ingest.py` is where that
invariant is asserted end to end.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_ingest import Fixture, event, post

from agentworld.domain.enums import ApiKeyScope
from agentworld.domain.principals import KeyPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.derived import run_id_for
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.ingest import IngestService
from helpers import Actor

pytestmark = pytest.mark.integration

#: Comfortably in the past, so `started_at` coming back equal to it proves
#: the value was read from the event rather than from the server's clock.
OCCURRED = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).replace(microsecond=0)

#: A reference PostgreSQL keeps apart from `r-ss` and `str.casefold()` does
#: not: U+00DF, LATIN SMALL LETTER SHARP S. Written as an escape rather than
#: as itself so that no editor, terminal or reviewer has to guess which
#: character is in the file.
SHARP_S = "r-\u00df"


@pytest.fixture
def ingest(client: TestClient, actor: Actor) -> Fixture:
    return Fixture(client, actor)


@pytest.fixture
def agent(client: TestClient, actor: Actor, ingest: Fixture) -> Any:
    """One dispatchable agent, created through the API as a person would.

    `dispatchable` because that is what an external run belongs to: the
    agent lives somewhere else and reports what it did. An agent row is
    configuration and nothing in the protocol can create one.
    """
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


def run_event(name: str, ref: str = "run-1", **overrides: Any) -> dict[str, Any]:
    """A `run.*` event correlated to the agent that owns the run.

    The correlation is the whole of the ownership question: a run-subject
    event names no agent in its subject, so `correlation.agent_key` is the
    only agent reference the ingest allowlist validates and the only one the
    producer can be held to.
    """
    body: dict[str, Any] = {
        "type": f"run.{name}",
        "subject": {"kind": "run", "ref": ref},
        "correlation": {"agent_key": "researcher"},
        "occurred_at": OCCURRED.isoformat(),
    }
    body.update(overrides)
    return event(**body)


async def stored_runs(database: Database, project_id: Any) -> list[dict[str, Any]]:
    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, run_ref, agent_id, status, step_count, tool_call_count, "
                    "       started_at, finished_at, error, last_event_at, duration_ms "
                    "  FROM runs WHERE project_id = :p ORDER BY started_at DESC, id DESC"
                ),
                {"p": project_id},
            )
        ).mappings()
        return [dict(row) for row in rows]


async def stored_steps(database: Database, project_id: Any) -> list[dict[str, Any]]:
    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT run_id, seq, name, status FROM run_steps "
                    " WHERE project_id = :p ORDER BY seq"
                ),
                {"p": project_id},
            )
        ).mappings()
        return [dict(row) for row in rows]


def listed_runs(client: TestClient, actor: Actor, ingest: Fixture, agent_id: Any) -> Any:
    return client.get(
        f"/v1/projects/{ingest.project_id}/agents/{agent_id}/runs",
        headers=actor.headers,
    )


# --------------------------------------------------------------- the create ---


async def test_a_started_run_becomes_exactly_one_row(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """The gap D2 documented, closed.

    Every value is checked against the event or against a database default,
    because the point of this stage is that nothing is invented: the status
    and the two timestamps come from `run.started`, the counters from the
    column defaults, and `duration_ms` is null while the run is in flight.
    """
    accepted = post(client, ingest, run_event("started"))
    assert accepted.status_code == 202, accepted.text

    rows = await stored_runs(owner_database, ingest.project_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["run_ref"] == "run-1"
    assert row["agent_id"] == uuid.UUID(str(agent["id"]))
    assert row["status"] == "running"
    assert row["started_at"] == OCCURRED, "read from the event, not from the server's clock"
    assert row["last_event_at"] == OCCURRED
    assert row["finished_at"] is None
    assert row["duration_ms"] is None
    assert row["step_count"] == 0
    assert row["tool_call_count"] == 0
    assert row["error"] is None


async def test_the_id_is_derived_from_the_project_and_the_reference(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """Deterministic, which is what makes replay equivalence possible.

    Asserted against the function itself rather than against a literal, so
    the test pins "the id is this function of these two values" - changing
    the namespace or the input would break it, which is the whole point.
    """
    post(client, ingest, run_event("started"))

    rows = await stored_runs(owner_database, ingest.project_id)
    assert rows[0]["id"] == run_id_for(ingest.project_id, "run-1")


def test_the_id_is_a_function_of_the_exact_reference() -> None:
    """Python normalizes nothing here, deliberately.

    This test used to assert that `run_id_for` folded case the way `citext`
    does. It cannot. `citext` compares by `lower()` under the database's
    collation, `str.casefold()` is full Unicode folding, and the two
    disagree in both directions - `SHARP_S` folds to `ss` where PostgreSQL
    keeps the two apart, and folding here silently lost the second run.

    Which references are the same is a question only the database can
    answer, so it is asked there: the two tests below put both cases through
    real ingest. What is left for Python to promise is what this asserts -
    the id is a pure function of the project and the exact string it was
    given.
    """
    project = uuid.uuid4()
    assert run_id_for(project, "run-1") == run_id_for(project, "run-1")
    assert run_id_for(project, "Run-1") != run_id_for(project, "run-1")
    assert run_id_for(project, "r-ss") != run_id_for(project, SHARP_S)
    assert run_id_for(project, "run-1") != run_id_for(uuid.uuid4(), "run-1")


async def test_two_spellings_of_one_reference_make_one_run(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """`Run-1` then `run-1`: `citext` calls them one reference, so one row.

    The ids differ, because `run_id_for` is told the exact spelling - so
    what collapses the two here is `runs_project_ref_key`, which is the
    arrangement rather than a near miss. The second event is a real event
    with its own dedup key: it is accepted and stored, its append writes
    nothing, and the patch behind it lands on the row the first one made.

    `last_event_at` is the proof that it landed. It moves to the second
    event's time, which no row could show if the patch had matched nothing.
    """
    later = OCCURRED + dt.timedelta(minutes=1)
    first = post(client, ingest, run_event("started", ref="Run-1", dedup_key="spell-1"))
    second = post(
        client,
        ingest,
        run_event("started", ref="run-1", dedup_key="spell-2", occurred_at=later.isoformat()),
    )
    assert first.status_code == 202, first.text
    assert second.json()["accepted"] == 1, "a distinct event, not a duplicate delivery"

    rows = await stored_runs(owner_database, ingest.project_id)
    assert len(rows) == 1, "one reference, whatever it was spelled like"
    assert rows[0]["run_ref"] == "Run-1", "the spelling that arrived first is the one kept"
    assert rows[0]["id"] == run_id_for(ingest.project_id, "Run-1")
    assert rows[0]["id"] != run_id_for(ingest.project_id, "run-1"), "two ids, one row"
    assert rows[0]["last_event_at"] == later, "the second event patched the existing row"


async def test_two_references_only_python_would_merge_make_two_runs(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """`r-ss` and `r-` + SHARP_S: two references, and PostgreSQL knows it.

    `citext` is `lower()`, which leaves SHARP_S alone, so these are two runs
    and the table's unique key permits both. `str.casefold()` maps SHARP_S
    to `ss`, so a folded id would have given them one id - and the second
    run's insert would have been swallowed as a primary key conflict,
    leaving a run that was reported, accepted, and never stored.

    The steps are here because that is where the loss would have surfaced:
    a step resolves its run by `run_ref` through `citext`, so a run that
    never existed takes its whole trace with it. Each one is finished as
    well as started, because the trace and the counter take different
    routes to the run - an append keyed by the reference, and an increment
    on the run row - and both have to land on the right one.
    """
    post(
        client,
        ingest,
        run_event("started", ref="r-ss", dedup_key="fold-1"),
        run_event("started", ref=SHARP_S, dedup_key="fold-2"),
        run_event("step_started", ref="r-ss", dedup_key="fold-3", payload={"seq": 0, "name": "a"}),
        run_event("step_started", ref=SHARP_S, dedup_key="fold-4", payload={"seq": 0, "name": "b"}),
        run_event("step_finished", ref="r-ss", dedup_key="fold-5", payload={"seq": 0}),
        run_event("step_finished", ref=SHARP_S, dedup_key="fold-6", payload={"seq": 0}),
    )

    rows = await stored_runs(owner_database, ingest.project_id)
    assert len(rows) == 2, "PostgreSQL keeps these apart; so must we"
    by_ref = {row["run_ref"]: row for row in rows}
    assert set(by_ref) == {"r-ss", SHARP_S}
    assert by_ref["r-ss"]["id"] != by_ref[SHARP_S]["id"], "distinct references, distinct ids"
    assert by_ref["r-ss"]["id"] == run_id_for(ingest.project_id, "r-ss")
    assert by_ref[SHARP_S]["id"] == run_id_for(ingest.project_id, SHARP_S)

    owner = uuid.UUID(str(agent["id"]))
    assert {row["agent_id"] for row in rows} == {owner}, "both belong to the agent that reported"
    assert [row["step_count"] for row in (by_ref["r-ss"], by_ref[SHARP_S])] == [1, 1]

    steps = await stored_steps(owner_database, ingest.project_id)
    attached = {step["name"]: step["run_id"] for step in steps}
    assert attached == {
        "a": by_ref["r-ss"]["id"],
        "b": by_ref[SHARP_S]["id"],
    }, "neither trace attached to the other run"


async def test_the_run_belongs_to_the_agent_the_correlation_names(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """Two agents in one project; the correlation decides, not the key."""
    other = client.post(
        f"/v1/projects/{ingest.project_id}/agents",
        json={
            "display_name": "Writer",
            "agent_key": "writer",
            "interaction_mode": "dispatchable",
            "dispatch": {"transport": "webhook", "url": "https://example.test/hook"},
        },
        headers=actor.headers,
    )
    assert other.status_code == 201, other.text

    post(
        client,
        ingest,
        run_event("started", ref="run-mine"),
        run_event("started", ref="run-theirs", correlation={"agent_key": "writer"}),
    )

    rows = await stored_runs(owner_database, ingest.project_id)
    owners = {row["run_ref"]: row["agent_id"] for row in rows}
    assert owners["run-mine"] == uuid.UUID(str(agent["id"]))
    assert owners["run-theirs"] == uuid.UUID(str(other.json()["id"]))


# ---------------------------------------------------------------- the trace ---


async def test_the_trace_attaches_to_the_run_and_moves_its_counters(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """Steps and counters, which only work because the run now exists.

    Before R1 every one of these folded into an update that matched no row
    and changed nothing.
    """
    accepted = post(
        client,
        ingest,
        run_event("started"),
        run_event("step_started", payload={"seq": 0, "name": "turn", "kind": "llm"}),
        run_event("step_finished", payload={"seq": 0, "output": {"tokens": 240}}),
        event(
            type="tool.invoked",
            subject={"kind": "tool_call", "ref": "run-1:tool-1"},
            correlation={"agent_key": "researcher", "run_ref": "run-1"},
            occurred_at=OCCURRED.isoformat(),
        ),
    )
    assert accepted.status_code == 202, accepted.text

    (row,) = await stored_runs(owner_database, ingest.project_id)
    assert row["step_count"] == 1
    assert row["tool_call_count"] == 1

    steps = await stored_steps(owner_database, ingest.project_id)
    assert [(step["seq"], step["name"], step["status"]) for step in steps] == [
        (0, "turn", "succeeded")
    ]
    assert steps[0]["run_id"] == row["id"], "the trace points at the run it belongs to"


async def test_a_finished_run_records_when_it_ended(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    ended = OCCURRED + dt.timedelta(seconds=90)
    post(
        client,
        ingest,
        run_event("started"),
        run_event("finished", occurred_at=ended.isoformat()),
    )

    (row,) = await stored_runs(owner_database, ingest.project_id)
    assert row["status"] == "succeeded"
    assert row["finished_at"] == ended
    assert row["duration_ms"] == 90_000, "computed by the database from the two timestamps"


async def test_a_failed_run_records_why(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    post(
        client,
        ingest,
        run_event("started"),
        run_event(
            "failed",
            occurred_at=(OCCURRED + dt.timedelta(seconds=5)).isoformat(),
            payload={"error": {"code": "rate_limited"}},
        ),
    )

    (row,) = await stored_runs(owner_database, ingest.project_id)
    assert row["status"] == "failed"
    assert row["error"] == {"code": "rate_limited"}


# ----------------------------------------------------------- what is refused ---


async def test_a_duplicate_delivery_creates_no_second_run(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """Dedup refuses the event before it is folded; the keys are the backstop."""
    first = post(client, ingest, run_event("started", dedup_key="r-once"))
    again = post(client, ingest, run_event("started", dedup_key="r-once"))

    assert first.json()["accepted"] == 1
    assert again.json()["duplicates"] == 1, "the same event, not a second fact"
    assert len(await stored_runs(owner_database, ingest.project_id)) == 1


async def test_a_second_fact_about_the_same_run_creates_no_second_row(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """A distinct event naming a run that already exists.

    Dedup cannot help here - the keys differ, so both events are accepted
    and both fold - which is exactly the case the primary key and
    `runs_project_ref_key` are there for. The second insert does nothing;
    the patch behind it is permitted, because both events carry the same
    `occurred_at` and the staleness guard is `<=`, and writes the row the
    values it already holds. One row either way, which is what is asserted.
    """
    post(client, ingest, run_event("started", dedup_key="r-a"))
    second = post(client, ingest, run_event("started", dedup_key="r-b"))

    assert second.json()["accepted"] == 1, "a valid event, accepted and stored"
    assert len(await stored_runs(owner_database, ingest.project_id)) == 1


async def test_a_step_before_its_run_creates_neither(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """No stub run, and no orphan step.

    Creating a run here would mean inventing `started_at` from a step's
    timestamp, which is the one thing this producer must not do. The step is
    dropped instead, as it was before R1, and the event is still stored - so
    the loss is recoverable from the log if that decision is ever revisited.
    """
    accepted = post(
        client,
        ingest,
        run_event("step_started", ref="run-late", payload={"seq": 0, "name": "turn"}),
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["outcomes"][0]["rows_changed"] == 0

    assert await stored_runs(owner_database, ingest.project_id) == []
    assert await stored_steps(owner_database, ingest.project_id) == []


async def test_an_unknown_agent_reference_creates_no_run(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """202 and nothing written: an unresolved reference is not an error.

    The event is a true statement about the past whether or not this project
    holds the agent it names, so refusing it would lose history to tidy up a
    row that is not there.
    """
    accepted = post(client, ingest, run_event("started", correlation={"agent_key": "nobody-here"}))

    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["accepted"] == 1
    assert await stored_runs(owner_database, ingest.project_id) == []


async def test_an_event_with_no_agent_correlation_creates_no_run(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """`agent_id` is NOT NULL and a run subject names no agent.

    So an event that correlates nothing has nobody to attribute the run to,
    and inventing an owner is not available.
    """
    accepted = post(
        client,
        ingest,
        event(
            type="run.started",
            subject={"kind": "run", "ref": "run-anonymous"},
            occurred_at=OCCURRED.isoformat(),
        ),
    )

    assert accepted.status_code == 202, accepted.text
    assert await stored_runs(owner_database, ingest.project_id) == []


async def test_an_agent_in_another_project_creates_no_run(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """Reference resolution is project-scoped, so the key cannot reach out."""
    elsewhere = client.post(
        f"/v1/workspaces/{ingest.workspace['id']}/projects",
        json={"name": "Mainland"},
        headers=actor.headers,
    )
    assert elsewhere.status_code == 201, elsewhere.text
    outsider = client.post(
        f"/v1/projects/{elsewhere.json()['id']}/agents",
        json={
            "display_name": "Outsider",
            "agent_key": "outsider",
            "interaction_mode": "dispatchable",
            "dispatch": {"transport": "webhook", "url": "https://example.test/hook"},
        },
        headers=actor.headers,
    )
    assert outsider.status_code == 201, outsider.text

    accepted = post(client, ingest, run_event("started", correlation={"agent_key": "outsider"}))

    assert accepted.status_code == 202, accepted.text
    assert await stored_runs(owner_database, ingest.project_id) == []
    assert await stored_runs(owner_database, uuid.UUID(elsewhere.json()["id"])) == []


async def test_an_archived_agent_still_gets_its_run(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """Archiving stops an agent being used, not having been.

    Consistent with every other reducer: `agents` is patched and its history
    appended for an archived agent too, and D2's reads include them
    deliberately.
    """
    archived = client.delete(
        f"/v1/projects/{ingest.project_id}/agents/{agent['id']}",
        headers=actor.headers,
    )
    assert archived.status_code == 204, archived.text

    accepted = post(client, ingest, run_event("started"))

    assert accepted.status_code == 202, accepted.text
    (row,) = await stored_runs(owner_database, ingest.project_id)
    assert row["agent_id"] == uuid.UUID(str(agent["id"]))


async def test_a_key_outside_the_allowlist_is_refused_before_anything_is_written(
    client: TestClient, actor: Actor, owner_database: Database
) -> None:
    """The allowlist guards `correlation.agent_key`, which is the field R1 uses.

    That is not a coincidence worth leaving untested: the only agent
    reference a run-subject event carries is the one `_validated` already
    checks, so a key cannot create a run attributed to an agent it may not
    speak for.

    Its own fixture rather than the shared one, and not both: a workspace
    name is a slug, so a second `Fixture` in the same test would be refused
    before it got as far as the key.
    """
    restricted = Fixture(client, actor, agent_key_allowlist=["writer"])
    moved = client.post(
        f"/v1/projects/{restricted.project_id}/agents",
        json={
            "display_name": "Researcher",
            "agent_key": "researcher",
            "interaction_mode": "dispatchable",
            "dispatch": {"transport": "webhook", "url": "https://example.test/hook"},
        },
        headers=actor.headers,
    )
    assert moved.status_code == 201, moved.text

    refused = post(client, restricted, run_event("started"))

    assert refused.status_code == 403, refused.text
    assert await stored_runs(owner_database, restricted.project_id) == []


async def test_a_batch_refused_part_way_writes_neither_event_nor_run(
    client: TestClient, ingest: Fixture, agent: Any, owner_database: Database
) -> None:
    """Rollback inside the transaction, not before it.

    The skew and schema checks run before anything is opened, so they prove
    nothing about atomicity. Step ownership is checked inside the loop,
    after earlier events have been appended and folded - so a batch whose
    second event is refused there is the real test that the run and the
    event roll back together.
    """
    refused = post(
        client,
        ingest,
        run_event("started"),
        event(
            type="workflow.step_succeeded",
            subject={"kind": "execution_step", "ref": f"{uuid.uuid4()}:nowhere"},
            occurred_at=OCCURRED.isoformat(),
            payload={"output": {}},
        ),
    )
    assert refused.status_code == 403, refused.text

    assert await stored_runs(owner_database, ingest.project_id) == []
    async with owner_database.session() as session:
        stored = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE project_id = :p"),
                {"p": ingest.project_id},
            )
        ).scalar_one()
    assert stored == 0, "the event went back with the run"


async def test_two_concurrent_batches_cannot_create_two_runs(
    ingest: Fixture, agent: Any, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Both batches name one run with different dedup keys, at the same time.

    Through the service rather than the client, because `TestClient` is
    synchronous and cannot have two requests in flight. The project's cursor
    lock serialises them, so one inserts and the other meets the conflict -
    which is the arrangement, not a race that happens to come out right.
    """
    principal = KeyPrincipal(
        api_key_id=uuid.uuid4(),
        workspace_id=ingest.workspace_id,
        project_id=ingest.project_id,
        scope=ApiKeyScope.INGEST,
    )
    service = IngestService(factory)

    results = await asyncio.gather(
        service.ingest(principal, [run_event("started", dedup_key="c-1")]),
        service.ingest(principal, [run_event("started", dedup_key="c-2")]),
    )

    assert [result.accepted for result in results] == [1, 1], "both events are real and stored"
    assert len(await stored_runs(owner_database, ingest.project_id)) == 1


# ------------------------------------------------------------ the D2 read API ---


async def test_the_run_is_visible_through_the_agents_runs_endpoint(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any
) -> None:
    """What R1 was for: D2's read, returning something.

    The endpoint's contract is unchanged - the same fields in the same
    shape - and this is the first time any of it is non-empty.
    """
    post(
        client,
        ingest,
        run_event("started"),
        run_event("step_started", payload={"seq": 0, "name": "turn"}),
        run_event("step_finished", payload={"seq": 0, "output": {"tokens": 12}}),
        run_event("finished", occurred_at=(OCCURRED + dt.timedelta(seconds=30)).isoformat()),
    )

    response = listed_runs(client, actor, ingest, agent["id"])

    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    row = response.json()[0]
    assert row["run_ref"] == "run-1"
    assert row["status"] == "succeeded"
    assert row["step_count"] == 1
    assert row["duration_ms"] == 30_000
    assert row["task_id"] is None, "a standalone run belongs to no task"
    assert row["execution_step_id"] is None


async def test_another_agents_run_is_not_in_this_agents_list(
    client: TestClient, actor: Actor, ingest: Fixture, agent: Any
) -> None:
    other = client.post(
        f"/v1/projects/{ingest.project_id}/agents",
        json={
            "display_name": "Writer",
            "agent_key": "writer",
            "interaction_mode": "dispatchable",
            "dispatch": {"transport": "webhook", "url": "https://example.test/hook"},
        },
        headers=actor.headers,
    )
    assert other.status_code == 201, other.text

    post(
        client,
        ingest,
        run_event("started", ref="run-mine"),
        run_event("started", ref="run-theirs", correlation={"agent_key": "writer"}),
    )

    mine = listed_runs(client, actor, ingest, agent["id"]).json()
    theirs = listed_runs(client, actor, ingest, other.json()["id"]).json()

    assert [row["run_ref"] for row in mine] == ["run-mine"]
    assert [row["run_ref"] for row in theirs] == ["run-theirs"]

"""Migration 0026 populates `runs.last_activity_at` from the trace.

Migration 0025 added the column and backfilled nothing, so every run that
existed before it carries NULL there however busy it has been. 0026 copies
`max(run_steps.last_event_at)` forward and leaves behind the invariant stage
A2.2 is allowed to depend on:

    max(run_steps.last_event_at) <= runs.last_activity_at

for every run with any timestamped step, in every workspace, at every status.

These tests run the migration's own `upgrade()` - through an Alembic
`Operations` context, the same proxy `op` resolves through when alembic runs
it - against the migrated database the rest of the integration suite uses. Not
a copy of its SQL: a copy would keep passing after the shipped statement
changed, which is the one failure a migration test exists to prevent.

The states it has to handle cannot be produced through ingest any more, which
is the point of the migration: `_stirred` writes the clock now. So the rows are
seeded directly, as the schema owner, in the shapes a database that predates
0025 contains.

**Two tests turn on which role runs the statement, and the distinction is not
cosmetic.** `owner_database` is a superuser here, and a superuser bypasses
row-level security even under FORCE - so a test of RLS behaviour run through it
passes whether or not the migration sets a tenant, and proves nothing.
`database` is the application role: NOSUPERUSER, NOBYPASSRLS, really subject
to the policies migration 0010 put on `runs`. The three tests below that turn
on row-level security - the one hidden workspace, the blinded statement, and
the two-workspace loop - run the statement through `database` for that
reason, while seeding and verification stay with the owner.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = API_ROOT / "migrations" / "versions" / "0026_run_activity_backfill.py"

#: The migration's own stranded-row predicate, so "this dataset needed
#: backfilling" and "this dataset was backfilled" are one question asked
#: twice. `_stranded` asserts the shipped statement still contains it, so a
#: reworded migration fails here rather than quietly measuring something else.
STRANDED_PREDICATE = (
    "(SELECT max(s.last_event_at) "
    "FROM run_steps AS s "
    "WHERE s.run_id = r.id "
    "AND s.workspace_id = scope) "
    "> COALESCE(r.last_activity_at, '-infinity'::timestamptz)"
)

#: `scope` is the migration's loop variable, and the only rows its check reads
#: are the ones it has just restricted to that same workspace
#: (`r.workspace_id = scope`). Substituting the column therefore asks the
#: identical question over every workspace at once, which is what a test
#: outside the loop needs.
SCOPED = "s.workspace_id = scope"
UNSCOPED = "s.workspace_id = r.workspace_id"

#: Every column of `runs` this migration must leave alone, for the tests that
#: assert it changed nothing but the one clock.
RUN_COLUMNS = (
    "id, workspace_id, project_id, run_ref, agent_id, task_id, execution_step_id, status, "
    "step_count, tool_call_count, error, started_at, finished_at, last_event_at"
)


def _migration() -> ModuleType:
    """The shipped module, loaded by path.

    `migrations/versions` is not an importable package - alembic loads each
    revision by file - so this is the only way to reach the real `upgrade()`.
    """
    spec = importlib.util.spec_from_file_location("migration_0026", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _backfill(database: Database) -> None:
    """Run migration 0026's `upgrade()` once, and commit it.

    One transaction, because that is how alembic runs it
    (`transaction_per_migration=True` in `migrations/env.py`) and because the
    tenant setting the migration writes is transaction-local: a backfill split
    across two transactions would lose it half way.
    """
    module = _migration()

    def run(connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    async with database.engine.begin() as conn:
        await conn.run_sync(run)


# ------------------------------------------------------------------- seeding ---


async def _island(database: Database, slug: str = "island") -> tuple[uuid.UUID, uuid.UUID]:
    """A workspace and a project, which is all a run needs to exist."""
    user, workspace, project = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": user, "email": f"{slug}@example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO workspaces (id, slug, name, created_by) "
                "VALUES (:id, :slug, :name, :user)"
            ),
            {"id": workspace, "slug": slug, "name": slug.title(), "user": user},
        )
        await session.execute(
            text(
                "INSERT INTO projects (id, workspace_id, slug, name) "
                "VALUES (:id, :workspace, :slug, :name)"
            ),
            {"id": project, "workspace": workspace, "slug": slug, "name": slug.title()},
        )
    return workspace, project


async def _agent(database: Database, island: tuple[uuid.UUID, uuid.UUID]) -> uuid.UUID:
    workspace, project = island
    agent = uuid.uuid4()
    async with database.session() as session:
        await session.execute(
            text(
                "INSERT INTO agents "
                "(id, workspace_id, project_id, agent_key, interaction_mode, display_name, "
                " status) "
                "VALUES (:id, :workspace, :project, :key, 'observed', 'Reporter', 'idle')"
            ),
            {"id": agent, "workspace": workspace, "project": project, "key": f"a-{agent}"},
        )
    return agent


async def _run(
    database: Database,
    island: tuple[uuid.UUID, uuid.UUID],
    agent: uuid.UUID,
    *,
    run_ref: str,
    status: str = "running",
    started_minutes_ago: int = 180,
    event_minutes_ago: int | None = 180,
    activity_minutes_ago: int | None = None,
    steps: int = 0,
    tools: int = 0,
) -> uuid.UUID:
    """One run, in the shape a database that predates 0025 contains.

    `activity_minutes_ago=None` is that shape: the column exists and nothing
    has ever written it. The tests that need a clock already set pass one.
    """
    workspace, project = island
    run = uuid.uuid4()
    async with database.session() as session:
        await session.execute(
            text(
                "INSERT INTO runs (id, workspace_id, project_id, run_ref, agent_id, status, "
                "                  step_count, tool_call_count, started_at, finished_at, "
                "                  last_event_at, last_activity_at) "
                "VALUES (:id, :workspace, :project, :ref, :agent, CAST(:status AS run_status), "
                "        :steps, :tools, "
                "        now() - (CAST(:started AS int) * interval '1 minute'), "
                "        CASE WHEN CAST(:status AS run_status) = 'running' THEN NULL "
                "             ELSE now() - interval '5 minutes' END, "
                "        CASE WHEN CAST(:event AS int) IS NULL THEN NULL "
                "             ELSE now() - (CAST(:event AS int) * interval '1 minute') END, "
                "        CASE WHEN CAST(:activity AS int) IS NULL THEN NULL "
                "             ELSE now() - (CAST(:activity AS int) * interval '1 minute') END)"
            ),
            {
                "id": run,
                "workspace": workspace,
                "project": project,
                "ref": run_ref,
                "agent": agent,
                "status": status,
                "steps": steps,
                "tools": tools,
                "started": started_minutes_ago,
                "event": event_minutes_ago,
                "activity": activity_minutes_ago,
            },
        )
    return run


async def _step(
    database: Database,
    island: tuple[uuid.UUID, uuid.UUID],
    run: uuid.UUID,
    *,
    seq: int,
    minutes_ago: int | None,
) -> None:
    """One `run_steps` row. `minutes_ago=None` leaves its clock NULL.

    A NULL step clock is reachable only by a direct write - `step_started`
    fills it from the envelope - which is exactly why the migration has to
    decide what to do with it.
    """
    workspace, project = island
    async with database.session() as session:
        await session.execute(
            text(
                "INSERT INTO run_steps (run_id, workspace_id, project_id, seq, name, status, "
                "                       started_at, last_event_at) "
                "VALUES (:run, :workspace, :project, :seq, :name, 'succeeded', "
                "        now() - interval '2 hours', "
                "        CASE WHEN CAST(:minutes AS int) IS NULL THEN NULL "
                "             ELSE now() - (CAST(:minutes AS int) * interval '1 minute') END)"
            ),
            {
                "run": run,
                "workspace": workspace,
                "project": project,
                "seq": seq,
                "name": f"step-{seq}",
                "minutes": minutes_ago,
            },
        )


# ------------------------------------------------------------------- reading ---


async def _clock(database: Database, run: uuid.UUID) -> dt.datetime | None:
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT last_activity_at FROM runs WHERE id = :id"), {"id": run}
            )
        ).scalar_one()


async def _newest_step(database: Database, run: uuid.UUID) -> dt.datetime | None:
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT max(last_event_at) FROM run_steps WHERE run_id = :id"), {"id": run}
            )
        ).scalar_one()


async def _row(database: Database, run: uuid.UUID) -> dict[str, Any]:
    """Every column the migration must not touch, and the row version.

    `xmin` is in here because equal values are not the same as an untouched
    row: an UPDATE that writes a column back to the value it already held
    leaves a dead tuple behind and a WAL record for it, which on a table of
    this kind is work for nothing. It is also the only way to observe the
    aggregate guard in the shipped statement, whose effect on the *values* is
    nil - the monotonic predicate already refuses to write a NULL over a
    timestamp, so without `xmin` that guard could be deleted and no test here
    would notice.
    """
    query = f"SELECT xmin::text AS row_version, {RUN_COLUMNS} FROM runs WHERE id = :id"
    async with database.session() as session:
        return dict((await session.execute(text(query), {"id": run})).mappings().one())


async def _stranded(database: Database) -> int:
    """How many runs still hold trace activity their clock does not cover.

    The migration's own predicate, asserted to still be the migration's own
    predicate. Whitespace-normalised, because the shipped statement wraps it
    across two lines.
    """
    shipped = " ".join(_migration().BACKFILL.split())
    needle = " ".join(STRANDED_PREDICATE.split())
    assert shipped.count(needle) == 1, (
        "migration 0026 no longer contains this predicate, so these tests would be "
        "measuring something the migration does not check"
    )
    predicate = needle.replace(SCOPED, UNSCOPED, 1)
    assert predicate != needle
    async with database.session() as session:
        return int(
            (
                await session.execute(text(f"SELECT count(*) FROM runs AS r WHERE {predicate}"))
            ).scalar_one()
        )


# --------------------------------------------------------------- the invariant ---


async def test_a_historical_run_gets_its_newest_step_as_its_clock(
    owner_database: Database,
) -> None:
    """The newest step, not the first and not the last written.

    The three rows are inserted out of order on purpose: a backfill that took
    `min`, or the last row it happened to see, would pass a one-step test.
    """
    island = await _island(owner_database)
    agent = await _agent(owner_database, island)
    run = await _run(owner_database, island, agent, run_ref="historical")
    await _step(owner_database, island, run, seq=1, minutes_ago=90)
    await _step(owner_database, island, run, seq=2, minutes_ago=30)
    await _step(owner_database, island, run, seq=3, minutes_ago=60)

    assert await _clock(owner_database, run) is None

    await _backfill(owner_database)

    newest = await _newest_step(owner_database, run)
    assert newest is not None
    assert await _clock(owner_database, run) == newest


async def test_a_clock_already_newer_than_the_trace_is_left_alone(
    owner_database: Database,
) -> None:
    """Monotonic: what `_stirred` wrote wins.

    A run whose newest activity was a tool call carries a clock no step row
    accounts for. Overwriting it with the older trace would lose the only
    record that the tool call happened, and would make this migration a
    regression rather than a repair.
    """
    island = await _island(owner_database)
    agent = await _agent(owner_database, island)
    run = await _run(
        owner_database, island, agent, run_ref="already-dated", activity_minutes_ago=1, tools=4
    )
    await _step(owner_database, island, run, seq=1, minutes_ago=90)

    before = await _clock(owner_database, run)
    assert before is not None

    await _backfill(owner_database)

    assert await _clock(owner_database, run) == before


async def test_a_run_with_no_steps_is_not_touched(owner_database: Database) -> None:
    """No trace, nothing to copy - and nothing else moved either."""
    island = await _island(owner_database)
    agent = await _agent(owner_database, island)
    run = await _run(owner_database, island, agent, run_ref="untraced", tools=3)

    before = await _row(owner_database, run)

    await _backfill(owner_database)

    assert await _clock(owner_database, run) is None
    assert await _row(owner_database, run) == before


async def test_a_run_whose_every_step_clock_is_null_is_not_touched(
    owner_database: Database,
) -> None:
    """`max` over nothing but NULLs is NULL, and NULL is not a timestamp.

    Reachable only by a direct write, and the case the aggregate guard in the
    shipped statement exists for. The run keeps its NULL clock and its row.
    """
    island = await _island(owner_database)
    agent = await _agent(owner_database, island)
    run = await _run(owner_database, island, agent, run_ref="undated-trace")
    await _step(owner_database, island, run, seq=1, minutes_ago=None)
    await _step(owner_database, island, run, seq=2, minutes_ago=None)

    before = await _row(owner_database, run)

    await _backfill(owner_database)

    assert await _clock(owner_database, run) is None
    assert await _row(owner_database, run) == before


async def test_running_it_twice_changes_nothing(owner_database: Database) -> None:
    """Idempotent, which a monotonic update gets for free.

    A backfill that is not idempotent is one that cannot be re-run after a
    failure half way through, and this one walks a list of workspaces.
    """
    island = await _island(owner_database)
    agent = await _agent(owner_database, island)
    run = await _run(owner_database, island, agent, run_ref="twice")
    await _step(owner_database, island, run, seq=1, minutes_ago=45)

    await _backfill(owner_database)
    once_clock = await _clock(owner_database, run)
    once_row = await _row(owner_database, run)
    assert once_clock is not None

    await _backfill(owner_database)

    assert await _clock(owner_database, run) == once_clock
    assert await _row(owner_database, run) == once_row


async def test_the_dataset_really_needed_backfilling(owner_database: Database) -> None:
    """**The non-vacuity test.**

    Every other test here would pass against a migration that did nothing, if
    the rows it was given needed nothing. This one asks the migration's own
    stranded-row predicate before and after, so the dataset has to contain
    work and the migration has to do all of it.
    """
    island = await _island(owner_database)
    agent = await _agent(owner_database, island)
    for index, status in enumerate(("running", "succeeded", "failed", "cancelled")):
        run = await _run(owner_database, island, agent, run_ref=f"needs-{index}", status=status)
        # Two steps, with different clocks, because one step cannot tell
        # `max` from `min`: an aggregate mutated to the oldest step would
        # leave the newest uncovered and strand the run, which is what the
        # count below then catches.
        await _step(owner_database, island, run, seq=1, minutes_ago=50 + index)
        await _step(owner_database, island, run, seq=2, minutes_ago=20 + index)

    stranded_before = await _stranded(owner_database)
    assert stranded_before == 4, (
        f"expected four runs needing the backfill, found {stranded_before} - "
        "this dataset would prove nothing"
    )

    await _backfill(owner_database)

    assert await _stranded(owner_database) == 0


# ------------------------------------------------------- row-level security ---


async def test_the_backfill_reaches_rows_a_policy_would_hide(
    owner_database: Database, database: Database
) -> None:
    """Run as the application role, which row-level security really covers.

    `settings.py` documents the deployment this stands in for: no
    `MIGRATION_DATABASE_URL`, so alembic connects as the role the application
    uses - NOSUPERUSER, NOBYPASSRLS, and subject to FORCE ROW LEVEL SECURITY
    on `runs`. Alembic sets no tenant variable, so an `UPDATE runs` from a
    migration matches nothing at all in that deployment and reports success.
    This is the test that fails if the migration stops setting the variable
    per workspace.
    """
    island = await _island(owner_database)
    agent = await _agent(owner_database, island)
    run = await _run(owner_database, island, agent, run_ref="under-policy")
    await _step(owner_database, island, run, seq=1, minutes_ago=15)

    await _backfill(database)

    newest = await _newest_step(owner_database, run)
    assert newest is not None
    assert await _clock(owner_database, run) == newest


async def test_the_self_check_catches_a_backfill_that_cannot_see_its_rows(
    owner_database: Database, database: Database
) -> None:
    """**Why the shipped statement carries two guards and not one.**

    The stranded-row check reads `runs` through the very policy that hid the
    rows from the UPDATE. With no tenant set it therefore finds nothing
    stranded and raises nothing: measured on a scratch database as the
    application role, this exact statement without its `set_config` completes
    successfully and writes nothing at all. `workspaces` carries no policy, so
    the loop still turns - which is what makes the failure quiet.

    The visibility guard is what closes it, and this is the test that holds it
    there. The statement is derived from the shipped `BACKFILL` rather than
    written out here, so a migration reworded past the replacement fails on
    the count assertion instead of silently testing nothing.
    """
    module = _migration()
    needle = f"PERFORM set_config('{module.SETTING}', scope::text, true);"
    assert module.BACKFILL.count(needle) == 1, (
        "migration 0026 no longer sets the tenant once per workspace in the shape this "
        "test blinds, so what follows would not be the shipped statement"
    )
    blinded = module.BACKFILL.replace(needle, "", 1)
    assert blinded != module.BACKFILL

    island = await _island(owner_database)
    agent = await _agent(owner_database, island)
    run = await _run(owner_database, island, agent, run_ref="blinded")
    await _step(owner_database, island, run, seq=1, minutes_ago=15)

    with pytest.raises(DBAPIError, match="did not take for workspace"):
        async with database.engine.begin() as conn:
            await conn.execute(text(blinded))

    assert await _clock(owner_database, run) is None
    assert await _stranded(owner_database) == 1


async def test_every_workspace_is_backfilled_not_just_the_first(
    owner_database: Database, database: Database
) -> None:
    """The loop is a partition of the work, not a first-match.

    Two workspaces, each holding a run the migration has to reach, and the
    statement runs as the application role - so the tenant setting is what
    makes the rows visible, and it really has to be re-set on the second
    iteration rather than once before the loop. Run as a superuser this test
    would pass with the `set_config` deleted outright, which is the one thing
    it exists to rule out.

    Seeding and verification stay with the owner: each run belongs to a
    different workspace, and no single application-role session can see both.
    """
    first = await _island(owner_database, "first")
    second = await _island(owner_database, "second")
    one_agent = await _agent(owner_database, first)
    two_agent = await _agent(owner_database, second)
    one = await _run(owner_database, first, one_agent, run_ref="w")
    two = await _run(owner_database, second, two_agent, run_ref="w")
    await _step(owner_database, first, one, seq=1, minutes_ago=25)
    await _step(owner_database, second, two, seq=1, minutes_ago=35)

    assert await _stranded(owner_database) == 2

    await _backfill(database)

    assert await _clock(owner_database, one) == await _newest_step(owner_database, one)
    assert await _clock(owner_database, two) == await _newest_step(owner_database, two)
    assert await _stranded(owner_database) == 0

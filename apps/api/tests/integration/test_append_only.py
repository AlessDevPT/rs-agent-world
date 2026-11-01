"""The partitioned append-only tables.

Partitioning quietly undermines three guarantees, and each is checked here
rather than assumed. A unique constraint cannot be declared on the parent, so
dedup lives on each partition. Append-only is not inherited, so revoking the
write verbs on the parent alone would leave `UPDATE events_y2026m11` open.
And - the one that would have been a cross-tenant read - the tenant policy is
not applied to a query that names a partition directly.

All three rest on a single invariant, asserted below over every partition:
the application role holds no privilege on any of them, and needs none,
because a partition is reached through its parent.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration


#: The three partitioned parents, in migration order.
_PARENTS = ["events", "logs", "metric_samples"]


def _current_partition(parent: str) -> str:
    """The partition today's rows land in, named as the function names it."""
    today = dt.datetime.now(dt.UTC)
    return f"{parent}_y{today:%Y}m{today:%m}"


class Tenants:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.project_a = uuid.uuid4()
        self.project_b = uuid.uuid4()


@pytest.fixture
async def tenants(owner_database: Database) -> Tenants:
    t = Tenants()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'log@example.com')"),
            {"i": t.user_id},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :s, :n, :u)"),
            {"i": t.workspace_id, "s": "logs", "n": "Logs", "u": t.user_id},
        )
        for project_id, slug in ((t.project_a, "alpha"), (t.project_b, "beta")):
            await session.execute(
                text("INSERT INTO projects (id, workspace_id, slug, name) VALUES (:i, :w, :s, :n)"),
                {"i": project_id, "w": t.workspace_id, "s": slug, "n": slug},
            )
    return t


async def _append(
    db: Database,
    t: Tenants,
    *,
    project_id: uuid.UUID,
    seq: int,
    dedup_key: str,
    visible: list[uuid.UUID] | None = None,
) -> None:
    async with db.session(visible_workspaces=visible) as session:
        await session.execute(
            text(
                "INSERT INTO events (workspace_id, project_id, seq, dedup_key, type, "
                " protocol_version, occurred_at, subject_kind, subject_ref) "
                "VALUES (:w, :p, :seq, :dedup, 'agent.heartbeat', '1.0', now(), "
                "        'agent', 'worker-1')"
            ),
            {"w": t.workspace_id, "p": project_id, "seq": seq, "dedup": dedup_key},
        )


# ------------------------------------------------------------ partitioning ---


async def test_an_event_lands_in_the_current_month_partition(
    owner_database: Database, tenants: Tenants
) -> None:
    """Routing, checked by reading the partition directly rather than the parent."""
    await _append(owner_database, tenants, project_id=tenants.project_a, seq=1, dedup_key="a-1")

    partition = _current_partition("events")
    async with owner_database.session() as session:
        # The name is computed from the clock, not from user input.
        count = (await session.execute(text(f"SELECT count(*) FROM {partition}"))).scalar_one()
    assert count == 1


async def test_the_migration_already_created_the_months_it_promised(
    owner_database: Database,
) -> None:
    """`MONTHS_AHEAD` partitions exist before anything calls the function."""
    async with owner_database.session() as session:
        created = (
            await session.execute(text("SELECT agentworld_ensure_partitions(2)"))
        ).scalar_one()
    assert created == 0


async def test_ensure_partitions_is_idempotent(owner_database: Database) -> None:
    """A worker will call this far more often than monthly.

    The horizon is deliberately one no other test uses, and the assertion is
    on the *second* call rather than the first: partition DDL commits, so a
    test that assumed which partitions already existed would depend on the
    order the suite happened to run in.
    """
    async with owner_database.session() as session:
        await session.execute(text("SELECT agentworld_ensure_partitions(7)"))
    async with owner_database.session() as session:
        again = (await session.execute(text("SELECT agentworld_ensure_partitions(7)"))).scalar_one()
    assert again == 0, "a second call must create nothing"


async def test_ensure_partitions_refuses_an_absurd_horizon(
    owner_database: Database,
) -> None:
    """A typo should not create two hundred years of empty tables."""
    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(text("SELECT agentworld_ensure_partitions(99)"))
    assert "months_ahead" in str(caught.value)


#: Every partition of the named parents. Going through `pg_inherits` rather
#: than matching `relname LIKE 'events_y%'` matters twice: the propagated
#: indexes are named after their partition too, and `logs` would otherwise
#: never be distinguishable from a future `logs_yearly` of some other kind.
_PARTITIONS = (
    "SELECT c.relname FROM pg_class c "
    "JOIN pg_inherits i ON i.inhrelid = c.oid "
    "JOIN pg_class p ON p.oid = i.inhparent "
    "WHERE c.relkind = 'r' AND p.relname = ANY (:parents)"
)

#: A horizon past every other test's, so these cover partitions the migration
#: never created - which is the half that has to keep working unattended.
_FAR_HORIZON = 9


async def test_every_events_partition_carries_its_dedup_index(
    owner_database: Database,
) -> None:
    """The function, not the migration, is what keeps dedup going.

    A partition created next month must arrive with the same index, or
    idempotent ingest quietly lapses at the month boundary.
    """
    async with owner_database.session() as session:
        await session.execute(text("SELECT agentworld_ensure_partitions(:n)"), {"n": _FAR_HORIZON})
        partitions = list(
            (await session.execute(text(_PARTITIONS), {"parents": ["events"]})).scalars()
        )
        indexed = list(
            (
                await session.execute(
                    text("SELECT tablename FROM pg_indexes WHERE indexname LIKE '%dedup_idx'")
                )
            ).scalars()
        )

    assert len(partitions) == _FAR_HORIZON + 1
    assert sorted(indexed) == sorted(partitions)


async def test_the_application_role_holds_no_privilege_on_any_partition(
    owner_database: Database,
) -> None:
    """The invariant the other three guarantees rest on.

    A partition inherits neither the parent's revoked write verbs nor its
    tenant policy, and migration 0005's default privileges hand every newly
    created table `SELECT, INSERT`. The maintenance function revokes them
    again, and this is the test - not a policy - that keeps cross-tenant
    reads shut.
    """
    async with owner_database.session() as session:
        await session.execute(text("SELECT agentworld_ensure_partitions(:n)"), {"n": _FAR_HORIZON})
        granted = list(
            (
                await session.execute(
                    text(
                        "SELECT table_name || ':' || privilege_type "
                        "FROM information_schema.table_privileges "
                        f"WHERE grantee = :role AND table_name IN ({_PARTITIONS})"
                    ),
                    {"role": "agentworld_app", "parents": _PARENTS},
                )
            ).scalars()
        )
    assert granted == [], f"the application role can address partitions directly: {granted}"


# ------------------------------------------------------------------- dedup ---


async def test_a_duplicate_dedup_key_in_one_project_is_refused(
    owner_database: Database, tenants: Tenants
) -> None:
    """Idempotent ingest: a retried batch yields one event per dedup_key."""
    await _append(owner_database, tenants, project_id=tenants.project_a, seq=1, dedup_key="run-1")
    with pytest.raises(Exception) as caught:
        await _append(
            owner_database, tenants, project_id=tenants.project_a, seq=2, dedup_key="run-1"
        )
    assert "dedup_idx" in str(caught.value)


async def test_the_same_dedup_key_in_another_project_is_fine(
    owner_database: Database, tenants: Tenants
) -> None:
    """The key is scoped per project, so two tenants cannot collide."""
    await _append(owner_database, tenants, project_id=tenants.project_a, seq=1, dedup_key="run-1")
    await _append(owner_database, tenants, project_id=tenants.project_b, seq=1, dedup_key="run-1")


async def test_dedup_is_case_insensitive(owner_database: Database, tenants: Tenants) -> None:
    """`dedup_key` is citext, so `Run-1` and `run-1` are the same key."""
    await _append(owner_database, tenants, project_id=tenants.project_a, seq=1, dedup_key="run-1")
    with pytest.raises(Exception) as caught:
        await _append(
            owner_database, tenants, project_id=tenants.project_a, seq=2, dedup_key="RUN-1"
        )
    assert "dedup_idx" in str(caught.value)


async def test_a_sequence_number_must_be_positive(
    owner_database: Database, tenants: Tenants
) -> None:
    with pytest.raises(Exception) as caught:
        await _append(owner_database, tenants, project_id=tenants.project_a, seq=0, dedup_key="z")
    assert "event_seq_is_positive" in str(caught.value)


# -------------------------------------------------------------- append-only ---


@pytest.mark.parametrize("table", _PARENTS)
async def test_the_application_cannot_rewrite_history_through_the_parent(
    database: Database, owner_database: Database, tenants: Tenants, table: str
) -> None:
    await _append(owner_database, tenants, project_id=tenants.project_a, seq=1, dedup_key="p-1")

    # Both statements name a table from a fixed literal list.
    for statement in (
        f"UPDATE {table} SET workspace_id = workspace_id",
        f"DELETE FROM {table}",
    ):
        with pytest.raises(Exception) as caught:
            async with database.session(visible_workspaces=[tenants.workspace_id]) as session:
                await session.execute(text(statement))
        assert "permission denied" in str(caught.value).lower()


async def test_the_application_cannot_reach_a_partition_directly_at_all(
    database: Database, owner_database: Database, tenants: Tenants
) -> None:
    """What a parent-level revoke would have missed.

    The `SELECT` is in here with the writes deliberately, and is the worse of
    the two: the parent's tenant policy does not apply to a query that names
    a partition, so it would have returned every tenant's events rather than
    this one's.
    """
    await _append(owner_database, tenants, project_id=tenants.project_a, seq=1, dedup_key="q-1")
    partition = _current_partition("events")

    for statement in (
        f"SELECT count(*) FROM {partition}",
        f"UPDATE {partition} SET subject_ref = 'x'",
        f"DELETE FROM {partition}",
    ):
        with pytest.raises(Exception) as caught:
            async with database.session(visible_workspaces=[tenants.workspace_id]) as session:
                await session.execute(text(statement))
        assert "permission denied" in str(caught.value).lower()


async def test_the_application_can_still_append_and_read(
    database: Database, tenants: Tenants
) -> None:
    """So the refusals above are not passing because the grant is missing."""
    await _append(
        database,
        tenants,
        project_id=tenants.project_a,
        seq=1,
        dedup_key="r-1",
        visible=[tenants.workspace_id],
    )
    async with database.session(visible_workspaces=[tenants.workspace_id]) as session:
        count = (await session.execute(text("SELECT count(*) FROM events"))).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------- RLS ---


@pytest.mark.parametrize("table", _PARENTS)
async def test_the_append_only_tables_are_tenant_isolated(
    database: Database, owner_database: Database, tenants: Tenants, table: str
) -> None:
    """Through the parent, which is the only way the application can read."""
    await _append(owner_database, tenants, project_id=tenants.project_a, seq=1, dedup_key="s-1")
    expected = 1 if table == "events" else 0

    async with database.session() as session:
        # A table name from a fixed literal list, not from user input.
        unset = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    assert unset == 0, "an unset tenant sees nothing"

    async with database.session(visible_workspaces=[tenants.workspace_id]) as session:
        visible = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    assert visible == expected

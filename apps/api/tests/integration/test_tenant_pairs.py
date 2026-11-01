"""`workspace_id` and `project_id` must name the same tenant.

Every tenant-scoped table carries both: the first is what row-level security
reads, the second is the domain scope. Two single-column foreign keys check
that each exists and never that they agree, and RLS checks only the first - so
before migration 0012 a row could name one tenant's workspace and another
tenant's project. The structural test below is the one that matters: it holds
for tables nobody has written yet.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration

#: The three tables where the pair is *not* constrained, and why. They carry
#: no foreign key to `projects` because they outlive the rows they describe,
#: and a composite key would reintroduce a cascade over the largest tables in
#: the schema. Their only writer is ingest, which checks the pair itself.
BY_INGEST_ONLY = {"events", "logs", "metric_samples"}


class Tenants:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.mine = uuid.uuid4()
        self.theirs = uuid.uuid4()
        self.my_project = uuid.uuid4()
        self.their_project = uuid.uuid4()


@pytest.fixture
async def tenants(owner_database: Database) -> Tenants:
    """Two workspaces, one project each. Nothing is shared between them."""
    t = Tenants()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'pairs@example.com')"),
            {"i": t.user_id},
        )
        for workspace_id, project_id, slug in (
            (t.mine, t.my_project, "mine"),
            (t.theirs, t.their_project, "theirs"),
        ):
            await session.execute(
                # Separate binds for `slug` and `name`: one bind cannot serve
                # a citext column and a text one.
                text(
                    "INSERT INTO workspaces (id, slug, name, created_by) "
                    "VALUES (:i, :slug, :name, :u)"
                ),
                {"i": workspace_id, "slug": slug, "name": slug, "u": t.user_id},
            )
            await session.execute(
                text(
                    "INSERT INTO projects (id, workspace_id, slug, name) "
                    "VALUES (:i, :w, :slug, :name)"
                ),
                {"i": project_id, "w": workspace_id, "slug": slug, "name": slug},
            )
    return t


async def _insert_task(db: Database, *, workspace_id: uuid.UUID, project_id: uuid.UUID) -> None:
    async with db.session(visible_workspaces=[workspace_id]) as session:
        await session.execute(
            text("INSERT INTO tasks (workspace_id, project_id, title) VALUES (:w, :p, 'planted')"),
            {"w": workspace_id, "p": project_id},
        )


# ------------------------------------------------------------ the agreement ---


async def test_a_row_cannot_name_another_tenants_project(
    database: Database, tenants: Tenants
) -> None:
    """The write the probe in migration 0012 performed.

    Note which role does it: the application role, with only its own
    workspace visible, which is to say an ordinary authenticated caller
    rather than an administrator.
    """
    with pytest.raises(Exception) as caught:
        await _insert_task(database, workspace_id=tenants.mine, project_id=tenants.their_project)
    assert "tasks_project_fkey" in str(caught.value)


async def test_a_matching_pair_is_accepted(database: Database, tenants: Tenants) -> None:
    """So the refusal above is about the pair, not about the insert."""
    await _insert_task(database, workspace_id=tenants.mine, project_id=tenants.my_project)


async def test_a_workspace_wide_credential_needs_no_project(
    owner_database: Database, tenants: Tenants
) -> None:
    """NULL `project_id` means workspace-wide, and MATCH SIMPLE passes it.

    This is the intent rather than a loophole: a provider key shared by every
    project in a workspace has no project to agree with.
    """
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO credentials (workspace_id, project_id, name, kind, provider_slug, "
                " ciphertext, nonce, wrapped_key, key_id) "
                "VALUES (:w, NULL, 'shared', 'api_key', 'openai', "
                "        '\\x00'::bytea, '\\x00'::bytea, '\\x00'::bytea, 'kek-1')"
            ),
            {"w": tenants.mine},
        )


# --------------------------------------------------------- the structural one ---


async def test_every_tenant_scoped_table_constrains_the_pair(
    owner_database: Database,
) -> None:
    """The test that holds for tables nobody has written yet.

    A new table carrying both columns and only a single-column project key
    would reopen exactly what 0012 closed, and would do it silently. This
    fails instead, and names the table.
    """
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text("""
                SELECT c.relname,
                       EXISTS (
                         SELECT 1 FROM pg_constraint con
                          WHERE con.conrelid = c.oid AND con.contype = 'f'
                            AND con.confrelid = 'projects'::regclass
                            AND array_length(con.conkey, 1) = 2
                       ) AS has_composite
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                   AND c.relispartition = false
                   AND EXISTS (SELECT 1 FROM pg_attribute a
                                WHERE a.attrelid = c.oid AND a.attname = 'workspace_id'
                                  AND a.attnum > 0)
                   AND EXISTS (SELECT 1 FROM pg_attribute a
                                WHERE a.attrelid = c.oid AND a.attname = 'project_id'
                                  AND a.attnum > 0)
                 ORDER BY c.relname
                """)
            )
        ).all()

    found = {name for name, _ in rows}
    unconstrained = {name for name, composite in rows if not composite}

    assert len(found) > 15, f"the query should find every tenant-scoped table, found {found}"
    assert unconstrained == BY_INGEST_ONLY, (
        "a table carries both tenancy columns without constraining the pair: "
        f"{sorted(unconstrained - BY_INGEST_ONLY)}"
    )


async def test_the_append_only_tables_still_accept_a_mismatched_pair(
    database: Database, tenants: Tenants
) -> None:
    """The gap 0012 leaves, asserted rather than left for a reader to assume.

    If this test ever fails because the insert was *refused*, the gap has been
    closed - delete the test and take `events` out of `BY_INGEST_ONLY`. It is
    written this way round because an undocumented hole is worse than a
    documented one, and because the obligation it describes belongs to ingest,
    which does not exist yet.
    """
    async with database.session(visible_workspaces=[tenants.mine]) as session:
        await session.execute(
            text(
                "INSERT INTO events (workspace_id, project_id, seq, dedup_key, type, "
                " protocol_version, occurred_at, subject_kind, subject_ref) "
                "VALUES (:w, :p, 1, 'mismatch', 'agent.heartbeat', '1.0', now(), "
                "        'agent', 'worker-1')"
            ),
            {"w": tenants.mine, "p": tenants.their_project},
        )

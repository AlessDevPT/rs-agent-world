"""Migration 0022 populates `workflows.current_version_id`.

The column was declared by migration 0008 and written by nothing until S2, so
every workflow published before that commit names no version. Migration 0022
is what makes the column true of the whole table, and `ExecutionService.start`
cannot be moved onto it until that holds.

These tests run the migration's own `upgrade()` - through an Alembic
`Operations` context, the same proxy `op` resolves through when alembic runs
it - against the migrated database the rest of the integration suite uses. Not
a copy of its SQL: a copy would keep passing after the shipped statement
changed.

The states it has to handle cannot be produced through the API any more, which
is the point of the migration: publishing writes the pointer now. So the rows
are seeded directly, as the schema owner, in exactly the shapes a database
that predates S2 contains.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = API_ROOT / "migrations" / "versions" / "0022_current_version_backfill.py"


def _migration() -> ModuleType:
    """The shipped module, loaded by path.

    `migrations/versions` is not an importable package - alembic loads each
    revision by file - so this is the only way to reach the real `upgrade()`.
    """
    spec = importlib.util.spec_from_file_location("migration_0022", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _backfill(database: Database) -> None:
    """Run migration 0022's `upgrade()` once, and commit it.

    One transaction, because that is how alembic runs it
    (`transaction_per_migration=True` in `migrations/env.py`) and because the
    session variable the migration sets is transaction-local: a backfill split
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
    """A workspace and a project, which is all a workflow needs to exist."""
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


async def _workflow(
    database: Database,
    island: tuple[uuid.UUID, uuid.UUID],
    *,
    slug: str,
    published: Iterable[int] = (),
    drafts: Iterable[int] = (),
) -> tuple[uuid.UUID, dict[int, uuid.UUID]]:
    """A workflow with the versions named, published or not.

    `current_version_id` is left NULL, which is the state of every workflow
    published before S2 and the state this migration exists to correct. The
    tests that need a pointer already set write it themselves.
    """
    workspace, project = island
    workflow = uuid.uuid4()
    versions: dict[int, uuid.UUID] = {}
    published_numbers = set(published)

    async with database.session() as session:
        await session.execute(
            text(
                "INSERT INTO workflows (id, workspace_id, project_id, slug, name) "
                "VALUES (:id, :workspace, :project, :slug, :name)"
            ),
            {
                "id": workflow,
                "workspace": workspace,
                "project": project,
                "slug": slug,
                "name": slug.title(),
            },
        )
        for number in sorted(published_numbers | set(drafts)):
            versions[number] = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO workflow_versions "
                    "(id, workflow_id, workspace_id, project_id, version, graph, published_at) "
                    "VALUES (:id, :workflow, :workspace, :project, :version, '{}'::jsonb, "
                    "CASE WHEN :is_published THEN now() ELSE NULL END)"
                ),
                {
                    "id": versions[number],
                    "workflow": workflow,
                    "workspace": workspace,
                    "project": project,
                    "version": number,
                    "is_published": number in published_numbers,
                },
            )
    return workflow, versions


async def _point_at(database: Database, workflow_id: uuid.UUID, version_id: uuid.UUID) -> None:
    async with database.session() as session:
        await session.execute(
            text("UPDATE workflows SET current_version_id = :version WHERE id = :id"),
            {"version": version_id, "id": workflow_id},
        )


async def _pointer(database: Database, workflow_id: uuid.UUID) -> uuid.UUID | None:
    async with database.session() as session:
        return (
            await session.execute(
                text("SELECT current_version_id FROM workflows WHERE id = :id"),
                {"id": workflow_id},
            )
        ).scalar_one()


async def _rows(database: Database, sql: str, **params: Any) -> Sequence[Any]:
    async with database.session() as session:
        return (await session.execute(text(sql), params)).mappings().all()


# ----------------------------------------------------------- the four states ---


async def test_a_workflow_that_never_published_keeps_its_null_pointer(
    owner_database: Database,
) -> None:
    """NULL is not a gap to be filled: it means nothing has been published.

    A draft is never eligible, so a workflow whose only version is a draft
    comes out of the migration exactly as it went in.
    """
    island = await _island(owner_database)
    workflow, _ = await _workflow(owner_database, island, slug="unpublished", drafts=(1,))

    await _backfill(owner_database)

    assert await _pointer(owner_database, workflow) is None


async def test_one_published_version_and_a_draft_backfills_to_the_published_one(
    owner_database: Database,
) -> None:
    """The shape of every workflow that was edited after its first publish."""
    island = await _island(owner_database)
    workflow, versions = await _workflow(
        owner_database, island, slug="published-once", published=(1,), drafts=(2,)
    )

    await _backfill(owner_database)

    pointer = await _pointer(owner_database, workflow)
    assert pointer == versions[1]
    assert pointer != versions[2], "the open draft must never be named"


async def test_several_published_versions_backfill_to_the_highest_numbered(
    owner_database: Database,
) -> None:
    """`published_version`'s rule, in SQL.

    The published row with the highest `version`: not the first one, and not
    the newest row of any kind, which is the draft sitting above it.
    """
    island = await _island(owner_database)
    workflow, versions = await _workflow(
        owner_database, island, slug="published-thrice", published=(1, 2, 3), drafts=(4,)
    )

    await _backfill(owner_database)

    assert await _pointer(owner_database, workflow) == versions[3]


async def test_a_pointer_already_correct_is_left_alone(owner_database: Database) -> None:
    """What S2 wrote is what the migration finds.

    Running it twice changes nothing either: a backfill that is not idempotent
    cannot be re-run after a deploy that failed half way.
    """
    island = await _island(owner_database)
    workflow, versions = await _workflow(
        owner_database, island, slug="already-correct", published=(1, 2), drafts=(3,)
    )
    await _point_at(owner_database, workflow, versions[2])

    await _backfill(owner_database)
    assert await _pointer(owner_database, workflow) == versions[2]

    await _backfill(owner_database)
    assert await _pointer(owner_database, workflow) == versions[2]


# -------------------------------------------------- and the states around them ---


async def test_a_pointer_at_an_unpublished_draft_is_cleared(owner_database: Database) -> None:
    """The composite foreign key permits it; the lifecycle does not.

    `(id, current_version_id) -> workflow_versions(workflow_id, id)` says the
    version belongs to this workflow and says nothing about `published_at`, so
    the row is storable. No writer creates one - the only writer names a
    version it has just published - but leaving one in place would hand
    `start` a draft to execute once it reads this column.
    """
    island = await _island(owner_database)
    workflow, versions = await _workflow(owner_database, island, slug="draft-pointer", drafts=(1,))
    await _point_at(owner_database, workflow, versions[1])

    await _backfill(owner_database)

    assert await _pointer(owner_database, workflow) is None


async def test_every_workspace_is_backfilled_not_just_the_first(
    owner_database: Database,
) -> None:
    """The migration walks workspaces one at a time.

    So one of them being right says nothing about the rest, and a loop that
    stopped after its first iteration would pass every test above.
    """
    first = await _island(owner_database, "first")
    second = await _island(owner_database, "second")
    one, one_versions = await _workflow(owner_database, first, slug="w", published=(1, 2))
    two, two_versions = await _workflow(owner_database, second, slug="w", published=(1,))
    three, _ = await _workflow(owner_database, second, slug="x", drafts=(1,))

    await _backfill(owner_database)

    assert await _pointer(owner_database, one) == one_versions[2]
    assert await _pointer(owner_database, two) == two_versions[1]
    assert await _pointer(owner_database, three) is None


async def test_the_backfill_changes_nothing_but_the_pointer(owner_database: Database) -> None:
    """Every other column of `workflows`, and `workflow_versions` entire.

    Read before and after and compared row for row, because "data-only
    migration" is a claim about what it does not touch, and `published_at` in
    particular is what `_definition_of` and migration 0018 both depend on.
    """
    island = await _island(owner_database)
    workflow, _ = await _workflow(
        owner_database, island, slug="untouched", published=(1, 2), drafts=(3,)
    )

    columns = (
        "id, workspace_id, project_id, slug, name, description, enabled, "
        "created_by_user_id, created_at, archived_at, test_for_agent_id"
    )
    workflow_sql = f"SELECT {columns} FROM workflows WHERE id = :id"
    versions_sql = "SELECT * FROM workflow_versions WHERE workflow_id = :id ORDER BY version"

    before_workflow = await _rows(owner_database, workflow_sql, id=workflow)
    before_versions = await _rows(owner_database, versions_sql, id=workflow)

    await _backfill(owner_database)

    assert await _rows(owner_database, workflow_sql, id=workflow) == before_workflow
    assert await _rows(owner_database, versions_sql, id=workflow) == before_versions
    assert await _pointer(owner_database, workflow) is not None


async def test_the_backfill_reaches_rows_a_policy_would_hide(
    owner_database: Database, database: Database
) -> None:
    """Run as the application role, which row-level security really covers.

    `settings.py` documents the deployment this stands in for: no
    `MIGRATION_DATABASE_URL`, so alembic connects as the role the application
    uses - NOSUPERUSER, NOBYPASSRLS, and subject to FORCE ROW LEVEL SECURITY
    on `workflows`. Alembic sets no tenant variable, so an `UPDATE workflows`
    from a migration matches nothing at all in that deployment and reports
    success. This is the test that fails if the migration stops setting the
    variable per workspace.
    """
    island = await _island(owner_database)
    workflow, versions = await _workflow(
        owner_database, island, slug="under-policy", published=(1, 2), drafts=(3,)
    )

    await _backfill(database)

    assert await _pointer(owner_database, workflow) == versions[2]

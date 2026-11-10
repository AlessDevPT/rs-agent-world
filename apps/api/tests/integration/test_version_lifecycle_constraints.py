"""Migration 0023: whose version an execution runs, and the one open draft.

Two rules the application has always kept and the schema could not state. The
tests seed rows directly, as the schema owner, because the states that matter
are the ones no service would ever write - an execution naming another
workflow's version, a workflow with two editable copies of itself - and the
whole question is whether the database refuses them when something other than
`services/` is doing the writing: a migration, a repair script, a future
feature, a hand-written UPDATE at 2am.

The downgrade test runs the shipped `downgrade()` and then `upgrade()` again,
through an Alembic `Operations` context - the same proxy `op` resolves through
when alembic runs them - rather than a copy of their SQL, which would keep
passing after the shipped statements changed. It restores the schema in a
`finally`, because every test after it in the session shares this database.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = API_ROOT / "migrations" / "versions" / "0023_version_lifecycle_constraints.py"

VERSION_FKEY = "workflow_executions_version_fkey"
OLD_VERSION_FKEY = "workflow_executions_version_id_fkey"
OPEN_DRAFT_KEY = "workflow_versions_open_draft_key"

#: Keys this migration must leave alone. `workflow_versions_workflow_id_id_key`
#: is 0008's and is what `workflows.current_version_id` points at, so dropping
#: the new foreign key must not take it with it.
UNTOUCHED = {
    "workflow_versions_pkey",
    "workflow_versions_number_key",
    "workflow_versions_workflow_id_id_key",
}


def _migration() -> ModuleType:
    """The shipped module, loaded by path.

    `migrations/versions` is not an importable package - alembic loads each
    revision by file - so this is the only way to reach the real functions.
    """
    spec = importlib.util.spec_from_file_location("migration_0023", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(database: Database, step: str) -> None:
    """Run `upgrade()` or `downgrade()` once, in one transaction, and commit."""
    module = _migration()
    operation = getattr(module, step)

    def run(connection: Connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            operation()

    async with database.engine.begin() as conn:
        await conn.run_sync(run)


# ------------------------------------------------------------------- seeding ---


class Island:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.project_id = uuid.uuid4()


async def _island(database: Database, slug: str = "lifecycle") -> Island:
    island = Island()
    async with database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": island.user_id, "email": f"{slug}@example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO workspaces (id, slug, name, created_by) "
                "VALUES (:id, :slug, :name, :user)"
            ),
            {
                "id": island.workspace_id,
                "slug": slug,
                "name": slug.title(),
                "user": island.user_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO projects (id, workspace_id, slug, name) "
                "VALUES (:id, :workspace, :slug, :name)"
            ),
            {
                "id": island.project_id,
                "workspace": island.workspace_id,
                "slug": slug,
                "name": slug.title(),
            },
        )
    return island


async def _workflow(database: Database, island: Island, slug: str) -> uuid.UUID:
    workflow_id = uuid.uuid4()
    async with database.session() as session:
        await session.execute(
            text(
                "INSERT INTO workflows (id, workspace_id, project_id, slug, name) "
                "VALUES (:id, :workspace, :project, :slug, :name)"
            ),
            {
                "id": workflow_id,
                "workspace": island.workspace_id,
                "project": island.project_id,
                "slug": slug,
                "name": slug.title(),
            },
        )
    return workflow_id


async def _version(
    database: Database,
    island: Island,
    workflow_id: uuid.UUID,
    *,
    number: int,
    published: bool,
    version_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One version of one workflow, published or not, as the schema owner."""
    version_id = version_id or uuid.uuid4()
    async with database.session() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_versions "
                "(id, workflow_id, workspace_id, project_id, version, graph, published_at) "
                "VALUES (:id, :workflow, :workspace, :project, :number, '{}'::jsonb, "
                "        CASE WHEN :published THEN now() ELSE NULL END)"
            ),
            {
                "id": version_id,
                "workflow": workflow_id,
                "workspace": island.workspace_id,
                "project": island.project_id,
                "number": number,
                "published": published,
            },
        )
    return version_id


async def _execution(
    database: Database,
    island: Island,
    *,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    execution_id: uuid.UUID | None = None,
) -> uuid.UUID:
    execution_id = execution_id or uuid.uuid4()
    async with database.session() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(id, workspace_id, project_id, workflow_id, workflow_version_id, "
                " initiator_kind, initiator_label) "
                "VALUES (:id, :workspace, :project, :workflow, :version, 'system', 'test')"
            ),
            {
                "id": execution_id,
                "workspace": island.workspace_id,
                "project": island.project_id,
                "workflow": workflow_id,
                "version": version_id,
            },
        )
    return execution_id


async def _count(database: Database, table: str, column: str, value: uuid.UUID) -> int:
    """How many rows. The table and column are this file's own literals."""
    async with database.session() as session:
        return (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :value"), {"value": value}
            )
        ).scalar_one()


async def _execution_keys(database: Database) -> dict[str, str]:
    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'workflow_executions'::regclass"
                )
            )
        ).all()
    return dict(rows)


async def _version_indexes(database: Database) -> dict[str, str]:
    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename = 'workflow_versions'"
                )
            )
        ).all()
    return dict(rows)


# ------------------------------------------------- whose version is this run ---


async def test_an_execution_may_name_a_version_of_its_own_workflow(
    owner_database: Database,
) -> None:
    """So every refusal below is about the pair and not about the insert."""
    island = await _island(owner_database)
    workflow_id = await _workflow(owner_database, island, "own")
    version_id = await _version(owner_database, island, workflow_id, number=1, published=True)

    execution_id = await _execution(
        owner_database, island, workflow_id=workflow_id, version_id=version_id
    )

    assert await _count(owner_database, "workflow_executions", "id", execution_id) == 1


async def test_an_execution_cannot_name_another_workflows_version(
    owner_database: Database,
) -> None:
    """The gap 0023 closes.

    Before it this row was storable: `workflow_version_id` was bound to
    `workflow_versions (id)` alone, so the version had to exist and had not
    to belong to anything in particular. The record would then describe a run
    of a graph that is not a version of the workflow it names - and `advance`,
    which resolves the version by id, would execute it.
    """
    island = await _island(owner_database)
    mine = await _workflow(owner_database, island, "mine")
    theirs = await _workflow(owner_database, island, "theirs")
    their_version = await _version(owner_database, island, theirs, number=1, published=True)

    with pytest.raises(Exception) as caught:
        await _execution(owner_database, island, workflow_id=mine, version_id=their_version)
    assert VERSION_FKEY in str(caught.value)

    assert await _count(owner_database, "workflow_executions", "workflow_id", mine) == 0


async def test_an_execution_cannot_name_a_version_that_does_not_exist(
    owner_database: Database,
) -> None:
    """What the single-column key already caught, still caught.

    Replacing a foreign key is the kind of change that can quietly weaken
    one: a composite key spanning a nullable column, for instance, waves a
    partial row through by default. Both columns here are NOT NULL, and this
    is the test that says so.
    """
    island = await _island(owner_database)
    workflow_id = await _workflow(owner_database, island, "own")
    await _version(owner_database, island, workflow_id, number=1, published=True)

    with pytest.raises(Exception) as caught:
        await _execution(owner_database, island, workflow_id=workflow_id, version_id=uuid.uuid4())
    assert VERSION_FKEY in str(caught.value)


async def test_a_version_an_execution_is_bound_to_cannot_be_deleted(
    owner_database: Database,
) -> None:
    """RESTRICT, carried over unchanged from 0009.

    The record must not be able to become a reference to nothing: a deleted
    version would leave an execution describing a graph nobody can read.
    """
    island = await _island(owner_database)
    workflow_id = await _workflow(owner_database, island, "own")
    version_id = await _version(owner_database, island, workflow_id, number=1, published=True)
    await _execution(owner_database, island, workflow_id=workflow_id, version_id=version_id)

    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text("DELETE FROM workflow_versions WHERE id = :id"), {"id": version_id}
            )
    assert VERSION_FKEY in str(caught.value)


# ------------------------------------------------------------ one open draft ---


async def test_a_workflow_cannot_have_two_open_drafts(owner_database: Database) -> None:
    """A second editable copy of one workflow is unstorable.

    Nothing in `services/` writes one: `create` and `find_or_create_test`
    open version 1, `next_draft` opens the one above what was just published,
    and `save_draft` edits the open draft in place. Two would mean two
    answers to "what is being edited", one of which every reader ignores.
    """
    island = await _island(owner_database)
    workflow_id = await _workflow(owner_database, island, "two-drafts")
    await _version(owner_database, island, workflow_id, number=1, published=False)

    with pytest.raises(Exception) as caught:
        await _version(owner_database, island, workflow_id, number=2, published=False)
    assert OPEN_DRAFT_KEY in str(caught.value)

    assert await _count(owner_database, "workflow_versions", "workflow_id", workflow_id) == 1


async def test_each_workflow_has_its_own_open_draft(owner_database: Database) -> None:
    """The index is per workflow, which is the rule - not one draft per project.

    The second workflow here is in a second workspace as well, so this also
    says the constraint does not reach across tenants: uniqueness is enforced
    over every row in the table whatever a policy would show, and the column
    it spans is a workflow id, which belongs to one workspace already.
    """
    here = await _island(owner_database, "first-island")
    there = await _island(owner_database, "second-island")

    for island, slug in ((here, "a"), (there, "b")):
        workflow_id = await _workflow(owner_database, island, slug)
        await _version(owner_database, island, workflow_id, number=1, published=False)
        assert await _count(owner_database, "workflow_versions", "workflow_id", workflow_id) == 1


async def test_a_workflow_may_have_many_published_versions_and_one_draft(
    owner_database: Database,
) -> None:
    """The lifecycle this permits, stated as the thing it must not break.

    Three publishes and the draft the third opened: `published_at IS NULL` is
    the whole predicate, so the frozen history is as long as it likes.
    """
    island = await _island(owner_database)
    workflow_id = await _workflow(owner_database, island, "long-lived")
    for number in (1, 2, 3):
        await _version(owner_database, island, workflow_id, number=number, published=True)
    await _version(owner_database, island, workflow_id, number=4, published=False)

    assert await _count(owner_database, "workflow_versions", "workflow_id", workflow_id) == 4


async def test_the_application_role_is_subject_to_the_draft_rule(
    database: Database, owner_database: Database
) -> None:
    """The role the application actually connects as, under its own policy.

    Not a formality. Migration 0004 put `workflow_versions` behind a FORCE'd
    row-level security policy, and 0022 had to set the tenant variable per
    workspace because an UPDATE from a migration otherwise matched no rows at
    all. A unique index is the other half of that story: it is enforced over
    every row whether the writer can see it or not, and this is the test that
    holds that claim up.
    """
    island = await _island(owner_database)
    workflow_id = await _workflow(owner_database, island, "under-policy")
    insert = text(
        "INSERT INTO workflow_versions "
        "(workflow_id, workspace_id, project_id, version, graph) "
        "VALUES (:workflow, :workspace, :project, :number, '{}'::jsonb)"
    )
    binds = {
        "workflow": workflow_id,
        "workspace": island.workspace_id,
        "project": island.project_id,
    }

    async with database.session(visible_workspaces=[island.workspace_id]) as session:
        await session.execute(insert, {**binds, "number": 1})

    with pytest.raises(Exception) as caught:
        async with database.session(visible_workspaces=[island.workspace_id]) as session:
            await session.execute(insert, {**binds, "number": 2})
    assert OPEN_DRAFT_KEY in str(caught.value)


# --------------------------------------------------------------- the way back ---


async def test_the_downgrade_removes_these_two_and_nothing_else(
    owner_database: Database,
) -> None:
    """What `downgrade()` gives back, and what it must not take with it.

    Both halves matter. A downgrade that leaves the index behind makes the
    revision unrepeatable; one that drops 0008's unique key along with the
    foreign key referencing it breaks `workflows.current_version_id`, which is
    another migration's guarantee and S2's and S4's whole authority.

    The schema is put back in a `finally`: this database is shared by every
    test in the session, and leaving it at 0022 would fail the rest of them
    for a reason none of them is about.
    """
    island = await _island(owner_database)
    mine = await _workflow(owner_database, island, "mine")
    theirs = await _workflow(owner_database, island, "theirs")
    their_version = await _version(owner_database, island, theirs, number=1, published=True)
    planted_execution = uuid.uuid4()

    assert VERSION_FKEY in await _execution_keys(owner_database)
    assert OPEN_DRAFT_KEY in await _version_indexes(owner_database)

    try:
        await _run(owner_database, "downgrade")

        keys = await _execution_keys(owner_database)
        assert VERSION_FKEY not in keys
        # 0009's single-column key, with its RESTRICT: the prior behaviour,
        # not merely the prior name.
        assert OLD_VERSION_FKEY in keys
        assert "REFERENCES workflow_versions(id) ON DELETE RESTRICT" in keys[OLD_VERSION_FKEY]
        # Nothing else on the table went anywhere.
        assert "workflow_executions_id_version_key" in keys
        assert "workflow_executions_project_fkey" in keys

        indexes = await _version_indexes(owner_database)
        assert OPEN_DRAFT_KEY not in indexes
        assert set(indexes) >= UNTOUCHED, f"0023 dropped a key that is not its own: {indexes}"

        # And both states are storable again, which is what "the prior schema
        # behaviour" means.
        await _execution(
            owner_database,
            island,
            workflow_id=mine,
            version_id=their_version,
            execution_id=planted_execution,
        )
        await _version(owner_database, island, mine, number=1, published=False)
        await _version(owner_database, island, mine, number=2, published=False)
    finally:
        # The planted rows first: `upgrade()` validates the rows already
        # there, so leaving them would make the restore fail - which is the
        # same guarantee seen from the other side, and not what this test is
        # for.
        async with owner_database.session() as session:
            await session.execute(
                text("DELETE FROM workflow_executions WHERE id = :id"), {"id": planted_execution}
            )
            await session.execute(
                text("DELETE FROM workflow_versions WHERE workflow_id = :id AND version = 2"),
                {"id": mine},
            )
        await _run(owner_database, "upgrade")

    keys = await _execution_keys(owner_database)
    assert VERSION_FKEY in keys
    assert OLD_VERSION_FKEY not in keys
    assert OPEN_DRAFT_KEY in await _version_indexes(owner_database)

    # Refused again, both of them, by the restored objects.
    with pytest.raises(Exception) as caught:
        await _execution(owner_database, island, workflow_id=mine, version_id=their_version)
    assert VERSION_FKEY in str(caught.value)

    with pytest.raises(Exception) as caught:
        await _version(owner_database, island, mine, number=3, published=False)
    assert OPEN_DRAFT_KEY in str(caught.value)

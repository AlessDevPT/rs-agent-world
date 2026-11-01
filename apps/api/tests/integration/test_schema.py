"""Does the schema match what the code believes about it.

Three independent sources of truth have to agree, and each pair can drift:

- `domain.enums` says what the members of each enum are.
- `migrations/versions/0002_enums.py` created them, with its labels frozen as
  literals so the migration chain stays valid.
- `repositories.models` says what the tables look like, while
  `0003_tenancy.py` is what actually built them.

Rather than trusting discipline, this file asks the database.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection

from agentworld.domain.enums import PG_ENUMS
from agentworld.infra.db import Database
from agentworld.repositories.models import Base

pytestmark = pytest.mark.integration


async def test_every_python_enum_exists_in_the_database_with_the_same_members(
    owner_database: Database,
) -> None:
    """Label for label, and in order.

    Order matters for more than tidiness: PostgreSQL compares enum values by
    their declared order, so `ORDER BY status` and any range comparison depend
    on it. A member appended in Python but inserted in SQL would sort
    differently in the two places.
    """
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT t.typname, e.enumlabel "
                    "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                    "WHERE t.typname = ANY(:names) "
                    "ORDER BY t.typname, e.enumsortorder"
                ),
                {"names": list(PG_ENUMS)},
            )
        ).all()

    in_database: dict[str, list[str]] = {}
    for type_name, label in rows:
        in_database.setdefault(type_name, []).append(label)

    expected = {
        type_name: [member.value for member in python_enum]
        for type_name, python_enum in PG_ENUMS.items()
    }
    assert in_database == expected


async def test_there_are_no_database_enums_the_code_does_not_know_about(
    owner_database: Database,
) -> None:
    """The other direction: a type created by a migration and never used.

    Caught here rather than left to rot, because an unused enum is either a
    mistake in the migration or a missing entry in `PG_ENUMS`, and both are
    worth knowing about.
    """
    async with owner_database.session() as session:
        types = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT typname FROM pg_type t "
                        "JOIN pg_namespace n ON n.oid = t.typnamespace "
                        "WHERE t.typtype = 'e' AND n.nspname = 'public'"
                    )
                )
            ).all()
        }
    assert types == set(PG_ENUMS)


async def test_the_models_match_the_migrated_schema(owner_database: Database) -> None:
    """No drift between `repositories.models` and `migrations/`.

    Alembic's own comparison, run against a freshly migrated database. This is
    what makes it safe to hand-write migrations - which is necessary for
    `citext`, partial indexes and check constraints that autogenerate cannot
    express - without the models quietly describing a different schema.

    A failure here lists exactly what differs. If the difference is
    intentional, the fix is a new migration, not an edit to this test.
    """

    def diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(connection)
        return list(compare_metadata(context, Base.metadata))

    async with owner_database.engine.connect() as conn:
        differences = await conn.run_sync(diff)

    assert differences == [], f"models and migrations disagree: {differences}"


async def test_the_application_role_has_the_privileges_it_needs(
    database: Database,
) -> None:
    """Migration 0005's grants actually reached the application role.

    Without this, a missing grant would surface as a permission error in
    whichever test happened to run first, rather than as a statement about
    privileges.
    """
    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.table_privileges "
                    "WHERE grantee = current_user AND table_schema = 'public'"
                )
            )
        ).all()

    granted: dict[str, set[str]] = {}
    for table, privilege in rows:
        granted.setdefault(table, set()).add(privilege)

    for table in ("users", "workspaces", "workspace_members", "projects", "api_keys"):
        assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= granted.get(table, set()), (
            f"the application role is missing privileges on {table}"
        )

    # Append-only: readable and writable, never rewritable.
    assert {"SELECT", "INSERT"} <= granted.get("audit_log", set())
    assert not {"UPDATE", "DELETE"} & granted.get("audit_log", set())


async def test_the_single_owner_index_is_a_real_constraint(
    owner_database: Database,
) -> None:
    """Exactly one owner per workspace, enforced by the database.

    The service layer transfers ownership by demoting and promoting in one
    transaction. This index is what guarantees no code path - present or
    future - can leave a workspace with two owners.
    """
    import uuid

    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    workspace = uuid.uuid4()

    async with owner_database.session() as session:
        for user_id, email in ((user_a, "a@example.com"), (user_b, "b@example.com")):
            await session.execute(
                text("INSERT INTO users (id, email) VALUES (:id, :email)"),
                {"id": user_id, "email": email},
            )
        await session.execute(
            text(
                "INSERT INTO workspaces (id, slug, name, created_by) "
                "VALUES (:id, :slug, :name, :user_id)"
            ),
            {"id": workspace, "slug": "owners", "name": "Owners", "user_id": user_a},
        )
        await session.execute(
            text(
                "INSERT INTO workspace_members (workspace_id, user_id, role) "
                "VALUES (:workspace_id, :user_id, 'owner')"
            ),
            {"workspace_id": workspace, "user_id": user_a},
        )

    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO workspace_members (workspace_id, user_id, role) "
                    "VALUES (:workspace_id, :user_id, 'owner')"
                ),
                {"workspace_id": workspace, "user_id": user_b},
            )
    assert "workspace_single_owner" in str(caught.value)


async def test_an_ingest_key_cannot_be_created_without_a_project(
    owner_database: Database,
) -> None:
    """The check constraint, not just the service-layer validation.

    An unpinned ingest key could emit events into every project in the
    workspace, which is exactly the blast radius the scope exists to limit.
    """
    import uuid

    user_id, workspace = uuid.uuid4(), uuid.uuid4()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'k@example.com')"),
            {"id": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO workspaces (id, slug, name, created_by) "
                "VALUES (:id, :slug, :name, :user_id)"
            ),
            {"id": workspace, "slug": "keys", "name": "Keys", "user_id": user_id},
        )

    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO api_keys "
                    "(workspace_id, name, prefix, secret_hash, scope, created_by) "
                    "VALUES (:workspace_id, :name, :prefix, 'x', 'ingest', :user_id)"
                ),
                {
                    "workspace_id": workspace,
                    "name": "unpinned",
                    "prefix": "aw_test_abcdef123456",
                    "user_id": user_id,
                },
            )
    assert "ingest_keys_are_project_scoped" in str(caught.value)


async def test_only_a_managed_agent_may_name_a_model_profile(
    owner_database: Database,
) -> None:
    """The check constraint behind "runtime and model provider are separate".

    A `dispatchable` or `observed` agent runs somewhere else and chooses its
    own model, which AgentWorld never calls. Recording a model profile for one
    would imply a choice this system does not make and cannot enforce, so the
    database refuses it rather than the service remembering to.
    """
    import uuid

    user_id, workspace_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    profile_id = uuid.uuid4()

    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'modes@example.com')"),
            {"i": user_id},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :s, :n, :u)"),
            {"i": workspace_id, "s": "modes", "n": "Modes", "u": user_id},
        )
        await session.execute(
            text("INSERT INTO projects (id, workspace_id, slug, name) VALUES (:i, :w, :s, :n)"),
            {"i": project_id, "w": workspace_id, "s": "modes", "n": "Modes"},
        )
        await session.execute(
            text(
                "INSERT INTO model_profiles (id, workspace_id, name, provider_slug, model_id) "
                "VALUES (:i, :w, :n, 'anthropic', 'claude-opus-5')"
            ),
            {"i": profile_id, "w": workspace_id, "n": "Careful reasoning"},
        )

    async def insert(mode: str, with_profile: bool) -> None:
        async with owner_database.session() as session:
            await session.execute(
                # Separate binds for agent_key and display_name: one
                # parameter used for both a citext and a text column leaves
                # asyncpg unable to deduce a single type for it.
                text(
                    "INSERT INTO agents (workspace_id, project_id, agent_key, "
                    "interaction_mode, display_name, model_profile_id) "
                    "VALUES (:w, :p, :key, :mode, :name, :profile)"
                ),
                {
                    "w": workspace_id,
                    "p": project_id,
                    "key": f"{mode}-{with_profile}",
                    "name": f"{mode}-{with_profile}",
                    "mode": mode,
                    "profile": profile_id if with_profile else None,
                },
            )

    # A managed agent may name one, and must be able to.
    await insert("managed", with_profile=True)
    # Every mode may omit it.
    for mode in ("managed", "dispatchable", "observed"):
        await insert(mode, with_profile=False)
    # The other two may not name one.
    for mode in ("dispatchable", "observed"):
        with pytest.raises(Exception) as caught:
            await insert(mode, with_profile=True)
        assert "model_profile_only_when_managed" in str(caught.value)


async def test_exactly_one_default_model_profile_per_workspace(
    owner_database: Database,
) -> None:
    """The partial unique index, the same shape as `workspace_single_owner`."""
    import uuid

    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'defaults@example.com')"),
            {"i": user_id},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :s, :n, :u)"),
            {"i": workspace_id, "s": "defaults", "n": "Defaults", "u": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO model_profiles (workspace_id, name, provider_slug, model_id, "
                "is_default) VALUES (:w, 'Fast', 'anthropic', 'm', true)"
            ),
            {"w": workspace_id},
        )

    # A second non-default is fine; a second default is not.
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO model_profiles (workspace_id, name, provider_slug, model_id, "
                "is_default) VALUES (:w, 'Careful', 'anthropic', 'm', false)"
            ),
            {"w": workspace_id},
        )

    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO model_profiles (workspace_id, name, provider_slug, model_id, "
                    "is_default) VALUES (:w, 'Second default', 'anthropic', 'm', true)"
                ),
                {"w": workspace_id},
            )
    assert "one_default_model_profile" in str(caught.value)

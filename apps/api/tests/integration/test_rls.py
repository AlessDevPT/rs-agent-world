"""Row-level security, and the privileges that make it mean something.

The first test in this file is the most important one in the suite. RLS is
silently inert for a superuser or for any role with `BYPASSRLS` - `FORCE ROW
LEVEL SECURITY` included - so a tenant-isolation suite run as such a role
passes entirely on the strength of the application's own `WHERE` clauses while
reporting that the database layer works too. That is worse than having no RLS,
because it reads like protection.

So: before asserting anything about policies, assert that the role asserting
it cannot bypass them. Everything else here is conditional on that.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from agentworld.infra.db import Database, current_tenant_setting
from agentworld.repositories.models import RLS_PROTECTED_TABLES

pytestmark = pytest.mark.integration


async def test_the_application_role_cannot_bypass_rls(database: Database) -> None:
    """The guard against this whole file passing for the wrong reason.

    If this fails, every other RLS assertion here is vacuous.
    """
    async with database.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()

    _, is_superuser, can_bypass = row
    assert is_superuser is False, (
        f"The application connects as superuser {row[0]!r}. A superuser ignores every "
        "RLS policy, so the isolation tests would pass without the database doing "
        "anything. See infra/compose/postgres-init/01-app-role.sql."
    )
    assert can_bypass is False, f"The application role {row[0]!r} holds BYPASSRLS."


async def test_the_owner_does_bypass_rls(owner_database: Database) -> None:
    """The other half of the same point, stated positively.

    The schema owner here *is* a superuser, which is exactly why the
    application does not connect as it. This test documents that the
    difference between the two roles is real and not cosmetic.
    """
    async with owner_database.session() as session:
        is_superuser = (
            await session.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()
    assert is_superuser is True


async def test_every_protected_table_has_rls_enabled_and_forced(
    owner_database: Database,
) -> None:
    """The model's list and the database must agree.

    `RLS_PROTECTED_TABLES` is what the code claims; `pg_class` is what is
    true. A table added to one and not the other fails here.
    """
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {"names": list(RLS_PROTECTED_TABLES)},
            )
        ).all()

    found = {name: (enabled, forced) for name, enabled, forced in rows}
    assert set(found) == set(RLS_PROTECTED_TABLES), "a listed table is missing from pg_class"
    for name, (enabled, forced) in found.items():
        assert enabled is True, f"{name} does not have RLS enabled"
        assert forced is True, f"{name} has RLS enabled but not FORCED, so its owner bypasses it"


async def test_every_protected_table_has_exactly_one_policy(
    owner_database: Database,
) -> None:
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text("SELECT tablename, policyname FROM pg_policies WHERE tablename = ANY(:names)"),
                {"names": list(RLS_PROTECTED_TABLES)},
            )
        ).all()

    by_table: dict[str, list[str]] = {}
    for table, policy in rows:
        by_table.setdefault(table, []).append(policy)
    assert set(by_table) == set(RLS_PROTECTED_TABLES)
    for table, policies in by_table.items():
        assert policies == [f"{table}_tenant"], f"unexpected policies on {table}: {policies}"


# ------------------------------------------------------------------ behaviour ---


async def _seed_two_tenants(owner: Database) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Two workspaces, each with one project, inserted as the owner."""
    user_id = uuid.uuid4()
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()

    async with owner.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'seed@example.com')"),
            {"id": user_id},
        )
        for workspace, slug in ((workspace_a, "tenant-a"), (workspace_b, "tenant-b")):
            # Separate binds for slug and name: one parameter used for both a
            # citext and a text column leaves asyncpg unable to deduce a single
            # type for it.
            await session.execute(
                text(
                    "INSERT INTO workspaces (id, slug, name, created_by) "
                    "VALUES (:id, :slug, :name, :user_id)"
                ),
                {"id": workspace, "slug": slug, "name": slug, "user_id": user_id},
            )
        for project, workspace, slug in (
            (project_a, workspace_a, "proj-a"),
            (project_b, workspace_b, "proj-b"),
        ):
            await session.execute(
                text(
                    "INSERT INTO projects (id, workspace_id, slug, name) "
                    "VALUES (:id, :workspace_id, :slug, :name)"
                ),
                {"id": project, "workspace_id": workspace, "slug": slug, "name": slug},
            )
    return workspace_a, workspace_b, project_a, project_b


async def _count_projects(database: Database, visible: list[uuid.UUID] | None) -> int:
    """Count projects with a query that has no tenant filter of its own.

    Deliberately unfiltered: the point is what the *policy* does, not what a
    repository does. Every number this returns comes from the database.
    """
    async with database.session(visible_workspaces=visible) as session:
        return int((await session.execute(text("SELECT count(*) FROM projects"))).scalar_one())


async def test_an_unset_tenant_sees_nothing(database: Database, owner_database: Database) -> None:
    """Fail closed. An empty setting makes `= ANY (NULL)` evaluate to NULL,
    which RLS reads as "no rows" - so a request that has not established whose
    data it is entitled to gets none of it."""
    await _seed_two_tenants(owner_database)
    assert await _count_projects(database, None) == 0


async def test_one_workspace_sees_only_its_own(
    database: Database, owner_database: Database
) -> None:
    workspace_a, _, _, _ = await _seed_two_tenants(owner_database)
    assert await _count_projects(database, [workspace_a]) == 1


async def test_a_member_of_both_sees_both(database: Database, owner_database: Database) -> None:
    """Why the setting is a set: a user belongs to several workspaces, and
    `GET /v1/projects/{id}` has to be resolvable across all of them."""
    workspace_a, workspace_b, _, _ = await _seed_two_tenants(owner_database)
    assert await _count_projects(database, [workspace_a, workspace_b]) == 2


async def test_a_foreign_project_is_invisible_by_id(
    database: Database, owner_database: Database
) -> None:
    """The exact query `ProjectLookupRepository.workspace_of` runs."""
    workspace_a, _, _, project_b = await _seed_two_tenants(owner_database)
    async with database.session(visible_workspaces=[workspace_a]) as session:
        found = (
            await session.execute(
                text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": project_b}
            )
        ).scalar_one_or_none()
    assert found is None


async def test_writing_into_an_invisible_workspace_is_refused(
    database: Database, owner_database: Database
) -> None:
    """The WITH CHECK half. A USING-only policy would filter reads and let a
    caller insert rows into someone else's workspace."""
    workspace_a, workspace_b, _, _ = await _seed_two_tenants(owner_database)
    with pytest.raises(Exception) as caught:
        async with database.session(visible_workspaces=[workspace_a]) as session:
            await session.execute(
                text(
                    "INSERT INTO projects (workspace_id, slug, name) "
                    "VALUES (:workspace_id, 'sneak', 'sneak')"
                ),
                {"workspace_id": workspace_b},
            )
    assert "row-level security" in str(caught.value).lower()


async def test_updating_an_invisible_row_affects_nothing(
    database: Database, owner_database: Database
) -> None:
    """RLS turns a cross-tenant UPDATE into a no-op rather than an error,
    because the rows are simply not there to be matched."""
    workspace_a, _, _, project_b = await _seed_two_tenants(owner_database)
    async with database.session(visible_workspaces=[workspace_a]) as session:
        updated = (
            await session.execute(
                text("UPDATE projects SET name = 'hijacked' WHERE id = :id RETURNING id"),
                {"id": project_b},
            )
        ).scalar_one_or_none()
    assert updated is None

    async with owner_database.session() as session:
        name = (
            await session.execute(
                text("SELECT name FROM projects WHERE id = :id"), {"id": project_b}
            )
        ).scalar_one()
    assert name == "proj-b"


async def test_the_owner_sees_both_tenants_regardless(
    owner_database: Database,
) -> None:
    """Closing the loop: the same unfiltered query, run as the superuser,
    returns everything. The isolation above is the policy's doing, not the
    query's."""
    await _seed_two_tenants(owner_database)
    assert await _count_projects(owner_database, None) == 2


async def test_the_tenant_setting_does_not_leak_between_transactions(
    database: Database, owner_database: Database
) -> None:
    """`set_config(..., is_local => true)` is scoped to the transaction.

    Without the local flag the value would persist on the pooled connection
    and the next request to borrow it would inherit someone else's tenant -
    which is the worst bug this file could miss.
    """
    workspace_a, _, _, _ = await _seed_two_tenants(owner_database)

    async with database.session(visible_workspaces=[workspace_a]) as session:
        assert await current_tenant_setting(session) == str(workspace_a)

    # A fresh transaction on the same pool must start empty.
    async with database.session() as session:
        assert await current_tenant_setting(session) == ""
    assert await _count_projects(database, None) == 0


# ------------------------------------------------------------ append-only log ---


async def test_the_application_cannot_update_or_delete_the_audit_log(
    database: Database, owner_database: Database
) -> None:
    """Append-only as a privilege, not a convention.

    Migration 0005 grants the application role SELECT and INSERT on
    `audit_log` and nothing else, so a future code path that tried to rewrite
    history would fail at the database rather than succeed quietly.
    """
    workspace_a, _, _, _ = await _seed_two_tenants(owner_database)
    async with database.session(visible_workspaces=[workspace_a]) as session:
        await session.execute(
            text(
                "INSERT INTO audit_log (workspace_id, actor_type, action, resource_type) "
                "VALUES (:workspace_id, 'system', 'test.write', 'test')"
            ),
            {"workspace_id": workspace_a},
        )

    for statement in (
        "UPDATE audit_log SET action = 'tampered' WHERE workspace_id = :workspace_id",
        "DELETE FROM audit_log WHERE workspace_id = :workspace_id",
    ):
        with pytest.raises(Exception) as caught:
            async with database.session(visible_workspaces=[workspace_a]) as session:
                await session.execute(text(statement), {"workspace_id": workspace_a})
        assert "permission denied" in str(caught.value).lower()


async def test_the_audit_log_is_still_readable_and_writable(
    database: Database, owner_database: Database
) -> None:
    """The negative tests above must not be passing because the grant is
    missing altogether."""
    workspace_a, _, _, _ = await _seed_two_tenants(owner_database)
    async with database.session(visible_workspaces=[workspace_a]) as session:
        await session.execute(
            text(
                "INSERT INTO audit_log (workspace_id, actor_type, action, resource_type) "
                "VALUES (:workspace_id, 'system', 'test.write', 'test')"
            ),
            {"workspace_id": workspace_a},
        )
    async with database.session(visible_workspaces=[workspace_a]) as session:
        count = (await session.execute(text("SELECT count(*) FROM audit_log"))).scalar_one()
    assert count == 1


async def test_an_agent_in_another_workspace_is_invisible(
    database: Database, owner_database: Database
) -> None:
    """The policy on `agents`, exercised rather than merely present.

    `test_every_protected_table_has_rls_enabled_and_forced` proves the policy
    exists; this proves it does something. The query below has no tenant
    filter of its own on purpose - every row it fails to see is the database's
    doing.
    """
    workspace_a, workspace_b, _, _ = await _seed_two_tenants(owner_database)
    agent_a, agent_b = uuid.uuid4(), uuid.uuid4()

    async with owner_database.session() as session:
        for agent_id, workspace_id, key in (
            (agent_a, workspace_a, "worker-a"),
            (agent_b, workspace_b, "worker-b"),
        ):
            project_id = (
                await session.execute(
                    text("SELECT id FROM projects WHERE workspace_id = :w LIMIT 1"),
                    {"w": workspace_id},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO agents (id, workspace_id, project_id, agent_key, "
                    "interaction_mode, display_name) "
                    "VALUES (:id, :w, :p, :key, 'observed', :name)"
                ),
                {"id": agent_id, "w": workspace_id, "p": project_id, "key": key, "name": key},
            )

    async def visible(visible_workspaces: list[uuid.UUID] | None) -> set[uuid.UUID]:
        async with database.session(visible_workspaces=visible_workspaces) as session:
            rows = await session.execute(text("SELECT id FROM agents"))
            return set(rows.scalars().all())

    assert await visible(None) == set(), "an unset tenant must see no agents"
    assert await visible([workspace_a]) == {agent_a}
    assert await visible([workspace_b]) == {agent_b}
    assert await visible([workspace_a, workspace_b]) == {agent_a, agent_b}


async def test_an_agent_cannot_be_written_into_an_invisible_workspace(
    database: Database, owner_database: Database
) -> None:
    """The WITH CHECK half, on the table that matters most."""
    workspace_a, workspace_b, _, _ = await _seed_two_tenants(owner_database)
    async with owner_database.session() as session:
        project_b = (
            await session.execute(
                text("SELECT id FROM projects WHERE workspace_id = :w LIMIT 1"),
                {"w": workspace_b},
            )
        ).scalar_one()

    with pytest.raises(Exception) as caught:
        async with database.session(visible_workspaces=[workspace_a]) as session:
            await session.execute(
                text(
                    "INSERT INTO agents (workspace_id, project_id, agent_key, "
                    "interaction_mode, display_name) "
                    "VALUES (:w, :p, 'sneak', 'observed', 'sneak')"
                ),
                {"w": workspace_b, "p": project_b},
            )
    assert "row-level security" in str(caught.value).lower()

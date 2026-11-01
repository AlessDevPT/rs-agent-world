"""The presentation tables hold no execution state, and prove it.

The island must reflect real backend events, which means it must not keep its
own copy of them. Two separate claims are checked here. The first is
structural and holds for columns nobody has written yet: no `world_*` table
may carry a name from the forbidden list. The second is behavioural: moving a
building changes nothing operational, and an agent that goes offline or gets
archived stays on the island.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration

WORLD_TABLES = ("world_layouts", "world_districts", "world_nodes", "world_routes")

#: A column by any of these names would be a cache of execution state. The
#: list is from the proposal, and `hidden` is deliberately *not* on it: it is
#: a user's choice to hide something, which is presentation.
FORBIDDEN_COLUMNS = (
    "status",
    "progress",
    "progress_percent",
    "running",
    "state",
    "last_heartbeat_at",
    "current_task_id",
    "enabled",
    "health",
)


class World:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.other_project_id = uuid.uuid4()
        self.agent_id = uuid.uuid4()
        self.district_id = uuid.uuid4()
        self.other_district_id = uuid.uuid4()


@pytest.fixture
async def world(owner_database: Database) -> World:
    """Two projects, an agent in the first, and a district in each."""
    w = World()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'world@example.com')"),
            {"i": w.user_id},
        )
        await session.execute(
            text(
                "INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :slug, :name, :u)"
            ),
            {"i": w.workspace_id, "slug": "world", "name": "World", "u": w.user_id},
        )
        for project_id, slug in ((w.project_id, "island"), (w.other_project_id, "mainland")):
            await session.execute(
                text(
                    "INSERT INTO projects (id, workspace_id, slug, name) "
                    "VALUES (:i, :w, :slug, :name)"
                ),
                {"i": project_id, "w": w.workspace_id, "slug": slug, "name": slug},
            )
        await session.execute(
            text(
                "INSERT INTO agents (id, workspace_id, project_id, agent_key, "
                "interaction_mode, display_name) "
                "VALUES (:i, :w, :p, :key, 'managed', :name)"
            ),
            {
                "i": w.agent_id,
                "w": w.workspace_id,
                "p": w.project_id,
                "key": "islander",
                "name": "Islander",
            },
        )
        for district_id, project_id in (
            (w.district_id, w.project_id),
            (w.other_district_id, w.other_project_id),
        ):
            await session.execute(
                # `bounds` goes in as a bind: a JSON literal inside `text()`
                # has its `:0` read as a bind parameter.
                text(
                    "INSERT INTO world_districts (id, workspace_id, project_id, name, bounds) "
                    "VALUES (:i, :w, :p, 'Harbour', CAST(:bounds AS jsonb))"
                ),
                {
                    "i": district_id,
                    "w": w.workspace_id,
                    "p": project_id,
                    "bounds": '{"q": 0, "r": 0, "width": 8, "height": 8}',
                },
            )
    return w


async def _place(
    db: Database,
    w: World,
    *,
    node_id: uuid.UUID,
    q: int = 1,
    r: int = 1,
    district: uuid.UUID | None = None,
    kind: str = "agent",
    **columns: object,
) -> None:
    extra_names = "".join(f", {name}" for name in columns)
    extra_binds = "".join(f", :{name}" for name in columns)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO world_nodes (workspace_id, project_id, node_kind, node_id, "
                f" district_id, tile_q, tile_r, building_kind{extra_names}) "
                "VALUES (:w, :p, :kind, :node, :district, :q, :r, 'workshop'"
                f"{extra_binds})"
            ),
            {
                "w": w.workspace_id,
                "p": w.project_id,
                "kind": kind,
                "node": node_id,
                "district": district,
                "q": q,
                "r": r,
                **columns,
            },
        )


# ------------------------------------------------------------- the structure ---


async def test_no_presentation_table_carries_execution_state(
    owner_database: Database,
) -> None:
    """The acceptance criterion from the proposal, as a test rather than a rule.

    A future `agent_status` column on `world_nodes` fails here. So would
    `enabled`, which is the subtler mistake: it reads like configuration and
    is in fact a cache of whether something runs.
    """
    async with owner_database.session() as session:
        offenders = list(
            (
                await session.execute(
                    text(
                        "SELECT table_name || '.' || column_name "
                        "  FROM information_schema.columns "
                        " WHERE table_schema = 'public' "
                        "   AND table_name = ANY (:tables) "
                        "   AND column_name = ANY (:forbidden) "
                        " ORDER BY 1"
                    ),
                    {"tables": list(WORLD_TABLES), "forbidden": list(FORBIDDEN_COLUMNS)},
                )
            ).scalars()
        )
    assert offenders == [], f"presentation tables are caching execution state: {offenders}"


async def test_no_execution_table_references_a_presentation_table(
    owner_database: Database,
) -> None:
    """Dropping all four tables must leave every execution query valid.

    Checked as the property that makes it true - no foreign key points that
    way - because the alternative is dropping the tables in a test and
    putting them back.
    """
    async with owner_database.session() as session:
        inbound = list(
            (
                await session.execute(
                    text(
                        "SELECT con.conrelid::regclass::text || ' -> ' "
                        "    || con.confrelid::regclass::text "
                        "  FROM pg_constraint con "
                        " WHERE con.contype = 'f' "
                        "   AND con.confrelid::regclass::text = ANY (:tables) "
                        "   AND con.conrelid::regclass::text <> ALL (:tables)"
                    ),
                    {"tables": list(WORLD_TABLES)},
                )
            ).scalars()
        )
    assert inbound == [], f"the domain now depends on presentation: {inbound}"


# ------------------------------------------------------------ the behaviour ---


async def test_an_agent_keeps_its_node_when_it_goes_offline_or_is_archived(
    owner_database: Database, world: World
) -> None:
    """The island must not empty itself when things go wrong.

    This is the whole reason placement is not derived from execution state.
    """
    await _place(owner_database, world, node_id=world.agent_id)

    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE agents SET status = 'offline', archived_at = now() WHERE id = :i"),
            {"i": world.agent_id},
        )
        still_there = (
            await session.execute(
                text("SELECT count(*) FROM world_nodes WHERE node_id = :i"), {"i": world.agent_id}
            )
        ).scalar_one()
    assert still_there == 1


async def test_deleting_a_node_leaves_the_agent_and_its_history_intact(
    owner_database: Database, world: World
) -> None:
    """Removing something from the island is not removing it from the system."""
    run_id = uuid.uuid4()
    await _place(owner_database, world, node_id=world.agent_id)
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO runs (id, workspace_id, project_id, agent_id) VALUES (:i, :w, :p, :a)"
            ),
            {"i": run_id, "w": world.workspace_id, "p": world.project_id, "a": world.agent_id},
        )

    async with owner_database.session() as session:
        await session.execute(
            text("DELETE FROM world_nodes WHERE node_id = :i"), {"i": world.agent_id}
        )
        agents = (
            await session.execute(
                text("SELECT count(*) FROM agents WHERE id = :i"), {"i": world.agent_id}
            )
        ).scalar_one()
        runs = (
            await session.execute(text("SELECT count(*) FROM runs WHERE id = :i"), {"i": run_id})
        ).scalar_one()
    assert (agents, runs) == (1, 1)


async def test_deleting_an_agent_leaves_its_node_behind(
    owner_database: Database, world: World
) -> None:
    """The gap migration 0013 names, pinned so it cannot pass for a guarantee.

    `node_id` is polymorphic and carries no foreign key, so the database does
    not clean up after a deleted agent - and the orphan holds its tile,
    because the tile key does not care that its referent is gone. The Phase 3
    delete paths owe this, and when they land this test is the one to change.
    """
    await _place(owner_database, world, node_id=world.agent_id, q=4, r=4)

    async with owner_database.session() as session:
        await session.execute(text("DELETE FROM agents WHERE id = :i"), {"i": world.agent_id})
        orphans = (
            await session.execute(
                text("SELECT count(*) FROM world_nodes WHERE node_id = :i"), {"i": world.agent_id}
            )
        ).scalar_one()
    assert orphans == 1, "if this is 0, the gap is closed - update the test and migration 0013"

    # And the tile it holds is still unavailable, which is the user-visible half.
    with pytest.raises(Exception) as caught:
        await _place(owner_database, world, node_id=uuid.uuid4(), q=4, r=4, kind="workflow")
    assert "world_nodes_tile_key" in str(caught.value)


# ----------------------------------------------------------------- integrity ---


async def test_a_node_cannot_join_another_projects_district(
    owner_database: Database, world: World
) -> None:
    """Composite, so a district is a region of *this* project's island."""
    with pytest.raises(Exception) as caught:
        await _place(
            owner_database, world, node_id=world.agent_id, district=world.other_district_id
        )
    assert "world_nodes_district_fkey" in str(caught.value)


async def test_deleting_a_district_keeps_its_nodes(owner_database: Database, world: World) -> None:
    """SET NULL: dissolving a district must not delete the island."""
    await _place(owner_database, world, node_id=world.agent_id, district=world.district_id)
    async with owner_database.session() as session:
        await session.execute(
            text("DELETE FROM world_districts WHERE id = :i"), {"i": world.district_id}
        )
        district = (
            await session.execute(
                text("SELECT district_id FROM world_nodes WHERE node_id = :i"),
                {"i": world.agent_id},
            )
        ).scalar_one()
    assert district is None


async def test_one_object_per_tile(owner_database: Database, world: World) -> None:
    await _place(owner_database, world, node_id=world.agent_id, q=2, r=3)
    with pytest.raises(Exception) as caught:
        await _place(owner_database, world, node_id=uuid.uuid4(), q=2, r=3, kind="workflow")
    assert "world_nodes_tile_key" in str(caught.value)


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("rotation", 400, "rotation_is_a_bearing"),
        ("rotation", -1, "rotation_is_a_bearing"),
        ("scale", "9.99", "scale_is_bounded"),
        ("scale", "0.10", "scale_is_bounded"),
        ("width", 0, "footprint_is_bounded"),
        ("height", 99, "footprint_is_bounded"),
    ],
)
async def test_appearance_values_are_bounded(
    owner_database: Database, world: World, column: str, value: object, constraint: str
) -> None:
    """These arrive from a drag in a browser and are handed to a renderer."""
    with pytest.raises(Exception) as caught:
        await _place(owner_database, world, node_id=world.agent_id, **{column: value})
    assert constraint in str(caught.value)


async def test_a_tile_cannot_be_off_the_grid(owner_database: Database, world: World) -> None:
    with pytest.raises(Exception) as caught:
        await _place(owner_database, world, node_id=world.agent_id, q=-1)
    assert "tile_is_on_the_grid" in str(caught.value)


# ---------------------------------------------------------------------- RLS ---


async def test_the_presentation_tables_are_tenant_isolated(
    database: Database, owner_database: Database, world: World
) -> None:
    """A project's layout says what a tenant has built, so it is protected."""
    await _place(owner_database, world, node_id=world.agent_id)
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO world_layouts (workspace_id, project_id) VALUES (:w, :p)"),
            {"w": world.workspace_id, "p": world.project_id},
        )
        await session.execute(
            text(
                "INSERT INTO world_routes (workspace_id, project_id, route_kind, route_id) "
                "VALUES (:w, :p, 'connection', :r)"
            ),
            {"w": world.workspace_id, "p": world.project_id, "r": uuid.uuid4()},
        )

    async def counts(visible: list[uuid.UUID] | None) -> dict[str, int]:
        async with database.session(visible_workspaces=visible) as session:
            out: dict[str, int] = {}
            for table in WORLD_TABLES:
                # A fixed literal list, not user input.
                result = await session.execute(text(f"SELECT count(*) FROM {table}"))
                out[table] = int(result.scalar_one())
            return out

    unset = await counts(None)
    assert all(value == 0 for value in unset.values()), f"unset tenant saw {unset}"

    visible = await counts([world.workspace_id])
    assert visible == {
        "world_layouts": 1,
        "world_districts": 2,
        "world_nodes": 1,
        "world_routes": 1,
    }

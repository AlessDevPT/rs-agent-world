"""The island's presentation tables: layouts, districts, nodes, routes.

Phase 2, and the last of the schema. These four tables hold where a building
sits, how big it is, what it looks like and how a road is drawn - and nothing
else. The separation is the point, stated as the two failures it prevents:

**Moving a building must never change what the system does.** If position
lived on `agents`, a drag would be a write to a domain row, would need the
permission that edits an agent, would appear in the audit trail as a
configuration change, and would have to be considered by everything that
reads agent state.

**An agent that is offline, archived or broken must stay on the island.** If a
node's existence were derived from execution state, the island would empty
itself exactly when a user most needs to look at it. A shuttered building is
the information.

So no column here holds, mirrors or caches execution state - no status, no
progress, no health, no heartbeat. `hidden` is here because it is a *user's
choice to hide something*; an `offline` column would be a cache of execution
state, which is why there is not one. `test_world_separation.py` asserts that
against a forbidden-name list, so a future `agent_status` column on
`world_nodes` fails CI rather than a review.

Two vocabularies are native enums because they are structural - `node_kind`
decides which table `node_id` names, and `route_kind` likewise. Everything
about appearance stays `text`: `theme`, `building_kind`, `character_kind`,
`style` and `icon` grow with the art, and a migration per sprite would be
absurd.

**`node_id` has no foreign key, and that has a consequence worth stating.**
It names one of six kinds of object polymorphically, and three of those kinds
have no table yet, so there is nothing to point a key at. The proposal's
acceptance criterion "deleting the agent cascades the node" is therefore
*not* structural here: deleting an agent today leaves a node behind, and that
node keeps its tile, because `UNIQUE (project_id, tile_q, tile_r)` does not
care that its referent is gone. Two things follow, and both are obligations
rather than claims: the Phase 3 delete paths must remove the node in the same
transaction, and a reconciliation sweep must drop nodes whose referent has
vanished. `test_world_separation.py` pins the current behaviour so the gap
cannot be mistaken for a guarantee. The other direction *is* structural, and
is the one that matters more: deleting a node touches no domain row at all.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "agentworld_app"

SETTING = "agentworld.workspace_ids"

_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)

PROTECTED_TABLES: tuple[str, ...] = (
    "world_layouts",
    "world_districts",
    "world_nodes",
    "world_routes",
)

#: Frozen labels, like every other enum migration: these are the database's
#: vocabulary and must not drift with a Python module. `knowledge_source`,
#: `trigger` and `approval_desk` have no table yet and are here because the
#: island can place a node for something before Phase 6 builds its backend.
NODE_KINDS = (
    "agent",
    "workflow",
    "integration",
    "knowledge_source",
    "trigger",
    "approval_desk",
)

ROUTE_KINDS = ("connection", "workflow_edge")

_NODE_KIND = ENUM(*NODE_KINDS, name="world_node_kind", create_type=False)
_ROUTE_KIND = ENUM(*ROUTE_KINDS, name="world_route_kind", create_type=False)


def upgrade() -> None:
    _enums()
    _layouts()
    _districts()
    _nodes()
    _routes()

    for table in PROTECTED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} USING ({_VISIBLE}) WITH CHECK ({_VISIBLE})"
        )


def downgrade() -> None:
    for table in reversed(PROTECTED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("world_routes")
    op.drop_table("world_nodes")
    op.drop_table("world_districts")
    op.drop_table("world_layouts")
    op.execute("DROP TYPE IF EXISTS world_route_kind")
    op.execute("DROP TYPE IF EXISTS world_node_kind")


def _enums() -> None:
    for name, labels in (("world_node_kind", NODE_KINDS), ("world_route_kind", ROUTE_KINDS)):
        rendered = ", ".join(f"'{label}'" for label in labels)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")


def _tenancy() -> list[sa.SchemaItem]:
    """The workspace key, the project key, and the one that makes them agree.

    Migration 0012 has the probe that showed why the composite is not
    optional: two single-column keys check existence, never agreement, and
    row-level security reads `workspace_id` alone.
    """
    return [
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
    ]


def _project_keys(table: str) -> list[sa.SchemaItem]:
    return [
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=f"{table}_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["projects.workspace_id", "projects.id"],
            name=f"{table}_project_fkey",
            ondelete="CASCADE",
        ),
    ]


def _layouts() -> None:
    op.create_table(
        "world_layouts",
        *_tenancy(),
        sa.Column("theme", sa.Text(), server_default=sa.text("'island-temperate'"), nullable=False),
        sa.Column("grid_width", sa.SmallInteger(), server_default=sa.text("24"), nullable=False),
        sa.Column("grid_height", sa.SmallInteger(), server_default=sa.text("24"), nullable=False),
        # [{"q":3,"r":5,"terrain":"grass","decor":"tree"}, ...]
        sa.Column("tiles", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("landmarks", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        # The last camera position, per project. Presentation in the purest
        # sense: losing it costs a user one gesture.
        sa.Column("camera", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # A grid small enough to be unusable, or large enough to be a denial
        # of service against the renderer, is not a layout.
        sa.CheckConstraint(
            "grid_width BETWEEN 8 AND 256 AND grid_height BETWEEN 8 AND 256",
            name="grid_is_a_usable_size",
        ),
        # One layout per project, which is what makes `project_id` the key.
        sa.PrimaryKeyConstraint("project_id", name="world_layouts_pkey"),
        *_project_keys("world_layouts"),
    )


def _districts() -> None:
    op.create_table(
        "world_districts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        *_tenancy(),
        sa.Column("name", sa.Text(), nullable=False),
        # {"q":0,"r":0,"width":8,"height":8}
        sa.Column("bounds", JSONB(), nullable=False),
        sa.Column("accent_color", sa.Text(), nullable=True),
        sa.Column("collapsed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("sort_order >= 0", name="sort_order_is_not_negative"),
        sa.PrimaryKeyConstraint("id", name="world_districts_pkey"),
        sa.UniqueConstraint("project_id", "name", name="world_districts_project_name_key"),
        # What `world_nodes.district_id` points at, so a node cannot be filed
        # under a district belonging to another project.
        sa.UniqueConstraint("project_id", "id", name="world_districts_project_id_id_key"),
        *_project_keys("world_districts"),
    )


def _nodes() -> None:
    op.create_table(
        "world_nodes",
        *_tenancy(),
        # Which table `node_id` names. See the module docstring for why there
        # is no foreign key, and what that obliges.
        sa.Column("node_kind", _NODE_KIND, nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("district_id", sa.Uuid(), nullable=True),
        # ---- position and size ----
        sa.Column("tile_q", sa.SmallInteger(), nullable=False),
        sa.Column("tile_r", sa.SmallInteger(), nullable=False),
        sa.Column("width", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("height", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("rotation", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("z_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        # ---- appearance ----
        sa.Column("building_kind", sa.Text(), nullable=False),
        #: Sprite set, for agents.
        sa.Column("character_kind", sa.Text(), nullable=True),
        sa.Column("accent_color", sa.Text(), nullable=True),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.Column("scale", sa.Numeric(3, 2), server_default=sa.text("1.00"), nullable=False),
        # Whether the placement was chosen by the auto-layout or by a person.
        # The auto-layout may move its own nodes and must not move a person's.
        sa.Column("auto_assigned", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        # A user's choice to hide something - *not* a status. An `offline`
        # column here would be a cache of execution state.
        sa.Column("hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("tile_q >= 0 AND tile_r >= 0", name="tile_is_on_the_grid"),
        sa.CheckConstraint(
            "width BETWEEN 1 AND 16 AND height BETWEEN 1 AND 16", name="footprint_is_bounded"
        ),
        sa.CheckConstraint("rotation BETWEEN 0 AND 359", name="rotation_is_a_bearing"),
        sa.CheckConstraint("scale BETWEEN 0.25 AND 4.00", name="scale_is_bounded"),
        # One node per domain object per project.
        sa.PrimaryKeyConstraint("project_id", "node_kind", "node_id", name="world_nodes_pkey"),
        # One object per tile. This guards the *anchor* tile only: a 2x2
        # building and a 1x1 one can still overlap, because a constraint
        # cannot range over a footprint. Keeping footprints apart is the
        # placement service's job, and saying so here is better than implying
        # the database has it covered.
        sa.UniqueConstraint("project_id", "tile_q", "tile_r", name="world_nodes_tile_key"),
        # `SET NULL (district_id)` - the column list, new in PostgreSQL 15,
        # is load-bearing. A plain composite SET NULL nulls *every*
        # referencing column, so dissolving a district would try to null
        # `project_id` too, which is NOT NULL and half the primary key.
        # Deleting a district would simply fail; a test caught it.
        sa.ForeignKeyConstraint(
            ["project_id", "district_id"],
            ["world_districts.project_id", "world_districts.id"],
            name="world_nodes_district_fkey",
            ondelete="SET NULL (district_id)",
        ),
        *_project_keys("world_nodes"),
    )
    # The island's one query: every node in a project.
    op.create_index("world_nodes_project_idx", "world_nodes", ["project_id", "z_order"])
    op.create_index(
        "world_nodes_district_idx",
        "world_nodes",
        ["district_id"],
        postgresql_where=sa.text("district_id IS NOT NULL"),
    )


def _routes() -> None:
    op.create_table(
        "world_routes",
        *_tenancy(),
        # The *existence* of the relationship is domain - `connections`,
        # `workflow_edges`. Only its drawn path is here, which is what makes
        # rerouting a road change no grant.
        sa.Column("route_kind", _ROUTE_KIND, nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        # [{"q":4,"r":6}, ...]
        sa.Column("waypoints", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("style", sa.Text(), server_default=sa.text("'road'"), nullable=False),
        sa.Column("accent_color", sa.Text(), nullable=True),
        sa.Column("hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("project_id", "route_kind", "route_id", name="world_routes_pkey"),
        *_project_keys("world_routes"),
    )

"""Workflows: definitions, frozen versions, steps and edges.

Phase 2. A workflow is a definition; an **execution references a version, never
a workflow**. Editing a definition while it runs must not change what the
running copy means - otherwise a step can complete against a graph that no
longer contains it, and replay has nothing stable to fold against.

Four integrity rules are enforced here rather than left to the service layer,
because each one otherwise produces a graph that looks fine and cannot run:

**An edge cannot wire steps from two different versions.** A plain foreign key
to `workflow_steps(id)` would permit it, and the orchestrator would then see a
step that is not in its graph. The composite foreign keys on
`(workflow_version_id, from_step_id)` and `(workflow_version_id, to_step_id)`
make it impossible, which is why `workflow_steps` carries a redundant
`UNIQUE (workflow_version_id, id)` for them to reference.

**A workflow's current version must belong to that workflow.** Same shape:
`(id, current_version_id)` references `workflow_versions(workflow_id, id)`.

**Duplicate entry edges are refused.** The natural key is
`(version, from_step, to_step, label)`, and `from_step_id` is NULL for an entry
edge while `label` is usually NULL too. Under normal SQL semantics NULLs are
distinct, so that key would permit the same entry edge any number of times.
`UNIQUE NULLS NOT DISTINCT` (PostgreSQL 15+) treats them as equal, which is
what the key was meant to say.

**A step declares what it needs.** `agent_task` requires an agent and
`tool_call` requires a tool, by check constraint, so an unrunnable step cannot
be stored - `validate` then reports the richer problems, not this one.

`workflow_steps.tool_id` is a bare uuid for now. The foreign key to `tools` is
added by the migration that creates that table in Phase 6, the same way
`agents.current_task_id` waits for `tasks`.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, JSONB

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STEP_KIND = ENUM(name="step_kind", create_type=False)

#: Frozen at this revision.
PROTECTED_TABLES: tuple[str, ...] = (
    "workflows",
    "workflow_versions",
    "workflow_steps",
    "workflow_edges",
)

SETTING = "agentworld.workspace_ids"

_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)

#: What a step may do when it fails. Text with a check constraint rather than a
#: native enum: three values that the orchestrator branches on, and adding a
#: fourth should not need `ALTER TYPE` and the transaction dance that comes
#: with it (see migration 0006 and `env.py`).
ON_FAILURE = ("fail", "continue", "route")


def upgrade() -> None:
    _workflows()
    _workflow_versions()
    # Circular: a workflow points at its live version, and a version belongs
    # to a workflow. The composite target means the version named here cannot
    # belong to a different workflow.
    op.create_foreign_key(
        "workflows_current_version_fkey",
        "workflows",
        "workflow_versions",
        ["id", "current_version_id"],
        ["workflow_id", "id"],
        ondelete="SET NULL",
    )
    _workflow_steps()
    _workflow_edges()

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

    op.drop_table("workflow_edges")
    op.drop_table("workflow_steps")
    # The circular constraint has to go before the tables it joins.
    op.drop_constraint("workflows_current_version_fkey", "workflows", type_="foreignkey")
    op.drop_table("workflow_versions")
    op.drop_table("workflows")


def _workflows() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("slug", CITEXT(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # The published version triggers and manual runs use. NULL until the
        # first publish, which is why a workflow starts disabled.
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="workflows_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="workflows_project_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="workflows_created_by_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="workflows_pkey"),
        sa.UniqueConstraint("project_id", "slug", name="workflows_project_slug_key"),
    )


def _workflow_versions() -> None:
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        # The whole graph, frozen. `workflow_steps` and `workflow_edges` are
        # the queryable projection of it; this column is the authority an
        # execution is bound to, and is what makes a version reproducible even
        # if the projection is rebuilt.
        sa.Column("graph", JSONB(), nullable=False),
        sa.Column("input_schema", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # NULL means a draft. Only a published version may be executed.
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="version_is_positive"),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="workflow_versions_workflow_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="workflow_versions_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="workflow_versions_project_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="workflow_versions_created_by_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="workflow_versions_pkey"),
        sa.UniqueConstraint("workflow_id", "version", name="workflow_versions_number_key"),
        # Redundant on its own; it exists so `workflows.current_version_id`
        # can reference (workflow_id, id) and so be unable to name a version
        # belonging to another workflow.
        sa.UniqueConstraint("workflow_id", "id", name="workflow_versions_workflow_id_id_key"),
    )


def _workflow_steps() -> None:
    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_version_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # Stable within the version, e.g. "research". What an input mapping
        # refers to, so renaming the display name does not break a reference.
        sa.Column("step_key", CITEXT(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", _STEP_KIND, nullable=False),
        # RESTRICT: an agent assigned a step in a published version cannot be
        # deleted out from under it. Archive it instead.
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        # The FK to `tools` arrives with that table in Phase 6.
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("config", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # Declarative on purpose: an expression language the orchestrator
        # evaluates, not code it executes.
        sa.Column("input_mapping", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("on_failure", sa.Text(), server_default=sa.text("'fail'"), nullable=False),
        # A step that declares neither what it needs is unrunnable, and an
        # unrunnable step should not be storable.
        sa.CheckConstraint(
            "kind <> 'agent_task' OR agent_id IS NOT NULL", name="agent_task_needs_agent"
        ),
        sa.CheckConstraint(
            "kind <> 'tool_call' OR tool_id IS NOT NULL", name="tool_call_needs_tool"
        ),
        sa.CheckConstraint(
            "on_failure IN ('" + "', '".join(ON_FAILURE) + "')", name="on_failure_is_known"
        ),
        sa.CheckConstraint("max_attempts >= 1", name="max_attempts_is_positive"),
        sa.CheckConstraint(
            "timeout_seconds IS NULL OR timeout_seconds > 0", name="timeout_is_positive"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name="workflow_steps_version_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="workflow_steps_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="workflow_steps_project_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="workflow_steps_agent_id_fkey", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="workflow_steps_pkey"),
        sa.UniqueConstraint("workflow_version_id", "step_key", name="workflow_steps_key_key"),
        # Redundant on its own; it exists so `workflow_edges` can reference
        # (workflow_version_id, id) and so be unable to wire steps from two
        # different versions together.
        sa.UniqueConstraint("workflow_version_id", "id", name="workflow_steps_version_id_id_key"),
    )
    op.create_index("workflow_steps_agent_idx", "workflow_steps", ["agent_id"])


def _workflow_edges() -> None:
    op.create_table(
        "workflow_edges",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_version_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # NULL means an entry edge: this step starts when the execution does.
        sa.Column("from_step_id", sa.Uuid(), nullable=True),
        sa.Column("to_step_id", sa.Uuid(), nullable=False),
        # NULL means unconditional.
        sa.Column("condition", JSONB(), nullable=True),
        # "approved" / "rejected" - drawn on the island and on the DAG canvas.
        sa.Column("label", sa.Text(), nullable=True),
        # A self-loop is a cycle, and loops are not a v1 feature.
        sa.CheckConstraint(
            "from_step_id IS NULL OR from_step_id <> to_step_id", name="no_self_edge"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name="workflow_edges_version_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="workflow_edges_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="workflow_edges_project_id_fkey",
            ondelete="CASCADE",
        ),
        # The two that matter: both endpoints must live in the same version as
        # the edge. MATCH SIMPLE, so a NULL `from_step_id` - an entry edge -
        # passes without a target.
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "from_step_id"],
            ["workflow_steps.workflow_version_id", "workflow_steps.id"],
            name="workflow_edges_from_step_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "to_step_id"],
            ["workflow_steps.workflow_version_id", "workflow_steps.id"],
            name="workflow_edges_to_step_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="workflow_edges_pkey"),
        # NULLS NOT DISTINCT, because an entry edge has a NULL `from_step_id`
        # and usually a NULL `label`: under the default semantics this key
        # would permit the same entry edge any number of times.
        sa.UniqueConstraint(
            "workflow_version_id",
            "from_step_id",
            "to_step_id",
            "label",
            name="workflow_edges_edge_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "workflow_edges_from_idx", "workflow_edges", ["workflow_version_id", "from_step_id"]
    )
    op.create_index(
        "workflow_edges_to_idx", "workflow_edges", ["workflow_version_id", "to_step_id"]
    )

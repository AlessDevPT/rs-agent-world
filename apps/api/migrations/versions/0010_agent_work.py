"""Agent work: tasks, dependencies, runs, run steps, status history.

Phase 2. A **task** is the unit of work handed to an agent, and a **run** is
one attempt at it, carrying the agent's own trace - the LLM turns and tool
calls that made it up.

The relationship to workflows is the point of this slice. A task is no longer
the top-level concept it was in the previous design: an `agent_task` step
*materialises* one, so `tasks.execution_step_id` records where it came from,
and a standalone task - a plain work queue with no workflow - simply has NULL
there. That keeps the whole claim/heartbeat pipeline, including the
`FOR UPDATE SKIP LOCKED` claim, and gives it a place in the new product
instead of a competing one.

This migration also closes the two circular foreign keys earlier slices
deferred: `agents.current_task_id` and `execution_steps.task_id` both point
here, and neither could be declared before `tasks` existed.

Two integrity rules worth the lines:

**A dependency cannot cross projects.** `(project_id, task_id)` and
`(project_id, depends_on_id)` both reference `tasks(project_id, id)`, so a
task cannot wait on one in another tenant's project - which would be both
nonsense and a cross-tenant information channel.

**Progress is bounded.** `progress_percent` is checked to 0..100 rather than
trusted, because it arrives from an agent over the wire and is rendered
directly as a progress bar.

`runs.duration_ms` is a stored generated column: computed by PostgreSQL from
`finished_at - started_at` so no writer can disagree with it and no reader
has to recompute it.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, JSONB

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TASK_STATUS = ENUM(name="task_status", create_type=False)
_TASK_PRIORITY = ENUM(name="task_priority", create_type=False)
_RUN_STATUS = ENUM(name="run_status", create_type=False)
_AGENT_STATUS = ENUM(name="agent_status", create_type=False)

PROTECTED_TABLES: tuple[str, ...] = (
    "tasks",
    "task_dependencies",
    "runs",
    "run_steps",
    "agent_status_history",
)

SETTING = "agentworld.workspace_ids"

_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)

#: PostgreSQL computes this, so no writer can disagree with it and no reader
#: has to recompute it.
DURATION_MS = "(EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000)::int"


def upgrade() -> None:
    _tasks()
    _task_dependencies()
    _runs()
    _run_steps()
    _agent_status_history()

    # The two circular references earlier slices had to defer.
    op.create_foreign_key(
        "agents_current_task_fkey",
        "agents",
        "tasks",
        ["current_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "execution_steps_task_fkey",
        "execution_steps",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="SET NULL",
    )

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

    # Drop the inbound references before the table they point at.
    op.drop_constraint("execution_steps_task_fkey", "execution_steps", type_="foreignkey")
    op.drop_constraint("agents_current_task_fkey", "agents", type_="foreignkey")

    op.drop_table("agent_status_history")
    op.drop_table("run_steps")
    op.drop_table("runs")
    op.drop_table("task_dependencies")
    op.drop_table("tasks")


def _tasks() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # The caller's own id, so an integrator can address a task by the
        # reference their system already uses.
        sa.Column("task_ref", CITEXT(), nullable=True),
        # NULL for a standalone task; set when a workflow step materialised
        # this one. The whole relationship between the two models.
        sa.Column("execution_step_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status", _TASK_STATUS, server_default=sa.text("'pending'::task_status"), nullable=False
        ),
        sa.Column(
            "priority",
            _TASK_PRIORITY,
            server_default=sa.text("'normal'::task_priority"),
            nullable=False,
        ),
        sa.Column("assigned_agent_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_agent_id", sa.Uuid(), nullable=True),
        # Arrives from an agent over the wire and is rendered directly as a
        # progress bar, so it is checked rather than trusted.
        sa.Column("progress_percent", sa.SmallInteger(), nullable=True),
        sa.Column("progress_message", sa.Text(), nullable=True),
        sa.Column("input", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("error", JSONB(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("labels", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("queued_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100",
            name="progress_is_a_percentage",
        ),
        sa.CheckConstraint("attempt >= 0", name="task_attempt_is_not_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="task_max_attempts_is_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="tasks_workspace_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="tasks_project_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            name="tasks_execution_step_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_agent_id"],
            ["agents.id"],
            name="tasks_assigned_agent_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="tasks_created_by_user_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_agent_id"],
            ["agents.id"],
            name="tasks_created_by_agent_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="tasks_pkey"),
        sa.UniqueConstraint("project_id", "task_ref", name="tasks_project_ref_key"),
        # Redundant alone; the target of `task_dependencies`, which is what
        # stops a dependency crossing projects.
        sa.UniqueConstraint("project_id", "id", name="tasks_project_id_id_key"),
    )
    # The claim query's index: pending work in priority then arrival order.
    op.create_index(
        "tasks_claimable_idx",
        "tasks",
        ["project_id", "status", sa.text("priority DESC"), "created_at"],
    )
    op.create_index("tasks_assigned_idx", "tasks", ["assigned_agent_id", "status"])
    op.create_index("tasks_labels_idx", "tasks", ["labels"], postgresql_using="gin")
    op.create_index("tasks_execution_step_idx", "tasks", ["execution_step_id"])


def _task_dependencies() -> None:
    op.create_table(
        "task_dependencies",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("depends_on_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("task_id <> depends_on_id", name="no_self_dependency"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="task_dependencies_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        # Both ends are reached through (project_id, id), so a dependency
        # cannot cross projects - which would be nonsense and a cross-tenant
        # information channel.
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
            name="task_dependencies_task_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "depends_on_id"],
            ["tasks.project_id", "tasks.id"],
            name="task_dependencies_depends_on_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "depends_on_id", name="task_dependencies_pkey"),
    )
    op.create_index("task_dependencies_depends_on_idx", "task_dependencies", ["depends_on_id"])


def _runs() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_ref", CITEXT(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        # NULL for a run outside any workflow - an agent doing its own thing,
        # or a standalone task.
        sa.Column("execution_step_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", _RUN_STATUS, server_default=sa.text("'running'::run_status"), nullable=False
        ),
        sa.Column("step_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", JSONB(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Computed by PostgreSQL, so no writer can disagree with it.
        sa.Column("duration_ms", sa.Integer(), sa.Computed(DURATION_MS, persisted=True)),
        sa.CheckConstraint("step_count >= 0", name="step_count_is_not_negative"),
        sa.CheckConstraint("tool_call_count >= 0", name="tool_call_count_is_not_negative"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="runs_workspace_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="runs_project_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="runs_agent_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="runs_task_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            name="runs_execution_step_id_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="runs_pkey"),
        sa.UniqueConstraint("project_id", "run_ref", name="runs_project_ref_key"),
    )
    op.create_index("runs_task_idx", "runs", ["task_id", sa.text("started_at DESC")])
    op.create_index("runs_agent_idx", "runs", ["agent_id", sa.text("started_at DESC")])


def _run_steps() -> None:
    op.create_table(
        "run_steps",
        # bigserial: written far more often than read, always in order, and a
        # monotonic key keeps the index appending to one page.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # Free text: 'llm' | 'tool' | 'decision' | 'io' | anything an agent
        # chooses. The core never branches on it.
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column(
            "status", _RUN_STATUS, server_default=sa.text("'running'::run_status"), nullable=False
        ),
        sa.Column("input", JSONB(), nullable=True),
        sa.Column("output", JSONB(), nullable=True),
        sa.Column("error", JSONB(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("seq >= 0", name="run_step_seq_is_not_negative"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="run_steps_run_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="run_steps_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="run_steps_project_id_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="run_steps_pkey"),
        sa.UniqueConstraint("run_id", "seq", name="run_steps_seq_key"),
    )


def _agent_status_history() -> None:
    op.create_table(
        "agent_status_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # NULL for the first transition, out of nothing.
        sa.Column("from_status", _AGENT_STATUS, nullable=True),
        sa.Column("to_status", _AGENT_STATUS, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        # The event that caused the transition. A plain uuid, not a foreign
        # key: `events` is partitioned and dropped by retention, and this
        # table outlives it by design - a dangling reference here is correct,
        # a cascade would delete the forensic record with the partition.
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="agent_status_history_agent_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="agent_status_history_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="agent_status_history_project_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="agent_status_history_pkey"),
    )
    op.create_index(
        "agent_status_history_agent_idx",
        "agent_status_history",
        ["agent_id", sa.text("changed_at DESC")],
    )

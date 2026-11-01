"""Execution: workflow_executions, execution_steps, and the dispatch outbox.

Phase 2. The three tables that record what actually ran, and the one place
work is handed out.

**`dispatches` is the outbox, and it is not part of the event log.** One
transaction appends an event, folds the state change, and inserts a dispatch
row; a worker then performs the side effect. That separation is what lets the
event log be replayed to rebuild state without re-sending the emails - the
guarantee protocol promise 7 asserts. `dispatches` is therefore operational
state rather than history: rows are updated as they progress and pruned after
completion, and nothing replays them.

Four integrity rules, each preventing a class of silent corruption:

**An execution's initiator is immutable and total.** `initiator_kind` is NOT
NULL and the XOR check means a `user` initiator names a user while a `system`
one names none. An execution with no recorded initiator is an audit hole.
Authority for a system-initiated run comes from the published workflow
version, never from a user - so there is no account whose permissions it
borrows and nothing to go stale after a demotion or a departure.

**An execution step must belong to the version its execution is bound to.**
The same bug class as 0008's cross-version edge, one level down: without the
two composite foreign keys here, a step record could be attributed to a step
absent from the graph being executed. `execution_steps` carries a redundant
`workflow_version_id` so both can be expressed.

**One dispatch per step attempt.** `UNIQUE (execution_step_id, kind, attempt)`
is what makes `advance()` safe to call liberally: two concurrent calls produce
a constraint violation on the second, not two Slack messages.

**A leased dispatch has a lease.** A row marked `leased` with no `leased_by`
or `leased_until` could never be reclaimed by the reaper, so it would be
stuck forever. The check constraint makes that state unrepresentable.

`workflow_executions.trigger_id`, `initiator_trigger_id` and
`execution_steps.task_id` are bare uuids for now; their foreign keys arrive
with `triggers` and `tasks`, as `agents.current_task_id` already waits.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXECUTION_STATUS = ENUM(name="execution_status", create_type=False)
_STEP_STATUS = ENUM(name="step_status", create_type=False)
_INITIATOR_KIND = ENUM(name="initiator_kind", create_type=False)
_DISPATCH_KIND = ENUM(name="dispatch_kind", create_type=False)
_DISPATCH_STATUS = ENUM(name="dispatch_status", create_type=False)
_SIDE_EFFECT = ENUM(name="side_effect", create_type=False)

PROTECTED_TABLES: tuple[str, ...] = (
    "workflow_executions",
    "execution_steps",
    "dispatches",
)

SETTING = "agentworld.workspace_ids"

_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)

#: Statuses in which an execution may still progress. The no-stuck-executions
#: invariant reads this: a live execution with no live dispatch, no pending
#: approval and no terminal step is a bug.
LIVE_EXECUTION_STATUSES = (
    "pending",
    "running",
    "waiting_approval",
    "waiting_input",
    "paused",
)


def upgrade() -> None:
    _executions()
    _execution_steps()
    _dispatches()

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

    op.drop_table("dispatches")
    op.drop_table("execution_steps")
    op.drop_table("workflow_executions")


def _executions() -> None:
    live = ", ".join(f"'{status}'" for status in LIVE_EXECUTION_STATUSES)
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        # RESTRICT: the frozen version an execution is bound to cannot be
        # deleted while the execution references it, or the record would
        # describe a graph nobody can read.
        sa.Column("workflow_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            _EXECUTION_STATUS,
            server_default=sa.text("'pending'::execution_status"),
            nullable=False,
        ),
        # The FK arrives with `triggers` in Phase 7.
        sa.Column("trigger_id", sa.Uuid(), nullable=True),
        # ---- the immutable initiator ----
        sa.Column("initiator_kind", _INITIATOR_KIND, nullable=False),
        sa.Column("initiator_user_id", sa.Uuid(), nullable=True),
        sa.Column("initiator_trigger_id", sa.Uuid(), nullable=True),
        # Plain text, written once, so an audit two years later answers "what
        # started this" without a join that may have gone.
        sa.Column("initiator_label", sa.Text(), nullable=False),
        # A trigger firing twice yields one execution.
        sa.Column("idempotency_key", CITEXT(), nullable=True),
        sa.Column("input", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("output", JSONB(), nullable=True),
        sa.Column("error", JSONB(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Exactly one of the two initiator ids, matching the kind. A `system`
        # initiator names no user, which is the whole point: it borrows no
        # account's authority.
        sa.CheckConstraint(
            "(initiator_kind = 'user' AND initiator_user_id IS NOT NULL "
            "AND initiator_trigger_id IS NULL) "
            "OR (initiator_kind = 'system' AND initiator_user_id IS NULL)",
            name="initiator_user_xor_system",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="workflow_executions_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="workflow_executions_project_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="workflow_executions_workflow_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name="workflow_executions_version_id_fkey",
            ondelete="RESTRICT",
        ),
        # RESTRICT: a user who has initiated an execution cannot be hard
        # deleted out from under the record.
        sa.ForeignKeyConstraint(
            ["initiator_user_id"],
            ["users.id"],
            name="workflow_executions_initiator_user_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="workflow_executions_pkey"),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="workflow_executions_idempotency_key"
        ),
        # Redundant alone; it exists so `execution_steps` can reference
        # (id, workflow_version_id) and so be unable to record a step from a
        # version this execution is not bound to.
        sa.UniqueConstraint("id", "workflow_version_id", name="workflow_executions_id_version_key"),
    )
    op.create_index(
        "executions_live_idx",
        "workflow_executions",
        ["project_id", "status", "created_at"],
        postgresql_where=sa.text(f"status IN ({live})"),
    )
    op.create_index(
        "executions_initiator_idx",
        "workflow_executions",
        ["project_id", "initiator_kind", "created_at"],
    )
    op.create_index("executions_workflow_idx", "workflow_executions", ["workflow_id", "created_at"])


def _execution_steps() -> None:
    op.create_table(
        "execution_steps",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # Denormalised so the two composite foreign keys below can be
        # expressed: together they say "this step belongs to the version this
        # execution is bound to".
        sa.Column("workflow_version_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_step_id", sa.Uuid(), nullable=False),
        # Denormalised, so a timeline needs no join.
        sa.Column("step_key", CITEXT(), nullable=False),
        sa.Column(
            "status", _STEP_STATUS, server_default=sa.text("'pending'::step_status"), nullable=False
        ),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        # The FK arrives with `tasks`.
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("input", JSONB(), nullable=True),
        sa.Column("output", JSONB(), nullable=True),
        sa.Column("error", JSONB(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 0", name="step_attempt_is_not_negative"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="execution_steps_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="execution_steps_project_id_fkey",
            ondelete="CASCADE",
        ),
        # The first half of the version binding: this row's version must be
        # the one its execution is bound to.
        sa.ForeignKeyConstraint(
            ["execution_id", "workflow_version_id"],
            ["workflow_executions.id", "workflow_executions.workflow_version_id"],
            name="execution_steps_execution_version_fkey",
            ondelete="CASCADE",
        ),
        # The second half: the step named must live in that version.
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "workflow_step_id"],
            ["workflow_steps.workflow_version_id", "workflow_steps.id"],
            name="execution_steps_step_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="execution_steps_agent_id_fkey", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="execution_steps_pkey"),
        sa.UniqueConstraint(
            "execution_id", "workflow_step_id", "attempt", name="execution_steps_attempt_key"
        ),
    )
    op.create_index("execution_steps_execution_idx", "execution_steps", ["execution_id"])
    op.create_index(
        "execution_steps_live_idx",
        "execution_steps",
        ["project_id", "status"],
        postgresql_where=sa.text("status IN ('pending', 'ready', 'dispatched', 'running')"),
    )


def _dispatches() -> None:
    op.create_table(
        "dispatches",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("execution_step_id", sa.Uuid(), nullable=True),
        sa.Column("kind", _DISPATCH_KIND, nullable=False),
        sa.Column(
            "status",
            _DISPATCH_STATUS,
            server_default=sa.text("'pending'::dispatch_status"),
            nullable=False,
        ),
        # Copied from the step or tool at dispatch time, not looked up at
        # retry time: the retry policy must not change because somebody
        # edited a tool while work was in flight.
        sa.Column("side_effect_class", _SIDE_EFFECT, nullable=False),
        # The whole instruction, resolved: credential *references* and never
        # credentials.
        sa.Column("instruction", JSONB(), nullable=False),
        # Forwarded outbound as X-AgentWorld-Idempotency-Key, stable across
        # attempts of the same work, so a cooperating receiver can collapse a
        # retry. A human-initiated retry gets a new one, because that is new
        # work rather than a repeat.
        sa.Column(
            "idempotency_token",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        # A lease, not a lock: a worker that dies holds nothing.
        sa.Column("leased_by", sa.Text(), nullable=True),
        sa.Column("leased_until", sa.TIMESTAMP(timezone=True), nullable=True),
        # Set before the outbound call and cleared after it returns. A lease
        # that expires with this set means the side effect was in flight when
        # the worker died and nobody knows whether it landed.
        sa.Column("side_effect_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "run_after",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 0", name="dispatch_attempt_is_not_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="dispatch_max_attempts_is_positive"),
        # A row marked leased with no lease could never be reclaimed by the
        # reaper, so it would be stuck forever. Make that unrepresentable.
        sa.CheckConstraint(
            "status <> 'leased' OR (leased_by IS NOT NULL AND leased_until IS NOT NULL)",
            name="leased_dispatch_has_a_lease",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="dispatches_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="dispatches_project_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["workflow_executions.id"],
            name="dispatches_execution_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            name="dispatches_execution_step_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="dispatches_pkey"),
        # The idempotency guarantee: one dispatch per step attempt. Two
        # concurrent `advance` calls hit this rather than sending twice.
        sa.UniqueConstraint(
            "execution_step_id", "kind", "attempt", name="dispatches_step_attempt_key"
        ),
    )
    op.create_index(
        "dispatches_claimable_idx",
        "dispatches",
        ["run_after", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "dispatches_expired_lease_idx",
        "dispatches",
        ["leased_until"],
        postgresql_where=sa.text("status = 'leased'"),
    )
    # What the attention queue reads: the two states that need a human.
    op.create_index(
        "dispatches_attention_idx",
        "dispatches",
        ["project_id", "status"],
        postgresql_where=sa.text("status IN ('dead', 'needs_review')"),
    )

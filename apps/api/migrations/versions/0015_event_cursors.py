"""Where a project's next event sequence number comes from.

Phase 2. `events.seq` is the per-project monotonic cursor that WebSocket
resume and cursor pagination read, and it has to be allocated inside the same
transaction that appends the event - otherwise two concurrent batches can
agree on a number and one of them loses to the primary key, or worse, a reader
resuming from seq N misses an event that was committed after it with a lower
number.

Three ways to allocate it were available.

`SELECT max(seq) + 1` needs no table, and is wrong twice over: it scans a
partitioned table on every single event, and under concurrency two
transactions read the same maximum and race. A PostgreSQL sequence per project
would mean DDL at runtime, from a role that must not have it.

So: one row per project, locked with `FOR UPDATE` for the duration of the
append. That makes the allocation correct by serialising it, and the cost is
the thing to be honest about - **a project's ingest is serialised**. Two
batches for the same project queue behind each other for as long as the slower
one's transaction runs. That is the right trade for a cursor readers depend on
being gapless and ordered, and it bounds a single project's ingest throughput
rather than the system's. If one project ever needs more, the upgrade is to
drop the gapless property and hand out blocks of numbers, which costs resume
its simplicity - not a decision to take in advance.

The row is created on first use rather than when a project is, so this
migration backfills nothing: a project with no events needs no cursor, and
`INSERT ... ON CONFLICT DO NOTHING` makes the first append create it.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "agentworld.workspace_ids"

_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)


def upgrade() -> None:
    op.create_table(
        "project_event_cursors",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # The number the *next* event gets. Starts at 1, because `events` has
        # a `seq > 0` check and a zeroth event would be a lie about ordering.
        sa.Column("next_seq", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("next_seq > 0", name="next_seq_is_positive"),
        sa.PrimaryKeyConstraint("project_id", name="project_event_cursors_pkey"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="project_event_cursors_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["projects.workspace_id", "projects.id"],
            name="project_event_cursors_project_fkey",
            ondelete="CASCADE",
        ),
    )

    op.execute("ALTER TABLE project_event_cursors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_event_cursors FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY project_event_cursors_tenant ON project_event_cursors "
        f"USING ({_VISIBLE}) WITH CHECK ({_VISIBLE})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS project_event_cursors_tenant ON project_event_cursors")
    op.execute("ALTER TABLE project_event_cursors NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_event_cursors DISABLE ROW LEVEL SECURITY")
    op.drop_table("project_event_cursors")

"""Two tables the realtime spine needs, and nothing else.

Phase 3, step 3. The gateway's state is deliberately small: the frames are
built from `events`, which already exists, and the cursor is `events.seq`,
which already exists. What has nowhere to live is a WebSocket ticket and the
publisher's position.

**`ws_tickets`.** `03-API.md` §6: a browser cannot set a header on a
WebSocket, and a JWT in a query string ends up in access logs - so the client
exchanges its token for a 60-second single-use ticket and connects with that.
Single-use has to be enforced somewhere every gateway process can see, which
rules out process memory: a ticket minted by one replica is redeemed on
another, and R2's "the same ticket twice is refused the second time" would
otherwise be true only by accident of routing. A conditional
`UPDATE ... WHERE consumed_at IS NULL RETURNING` makes the redemption atomic
against a second attempt, including a concurrent one, which no read-then-write
in application code can claim.

The ticket string carries its workspace id, which is what keeps row-level
security *on* this table rather than exempted from it. The gateway holds a
ticket and no memberships, so without the id in the string it could not set
the visibility set the policy reads - the same circle `api_keys` resolves by
carrying no policy at all. Carrying the id is safe because it is only a read
filter: a forged workspace id widens the set to a tenant whose rows the
forger still cannot name, since redemption needs the secret's digest, and
every field the connection then uses is read back from the row rather than
taken from the string.

**`event_publication_cursors`.** P10.4: publisher progress is database-backed
and per project, so a publisher crash or a Redis outage loses nothing - the
next pass reads committed `events` rows from the recorded position. Its own
table rather than a column on `project_event_cursors`, and that is the
deliberate part: that row is locked `FOR UPDATE` by every ingest batch and
every definition write, so a publisher updating it would queue behind a
project's ingest and, worse, hold the lock that ingest is waiting for. The
publisher must never be able to slow the writers it is reading from.

Neither table holds a payload. The committed event row is the durable
publication record (P10, Option A), so there is no outbox here and nothing to
keep in step with it.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "agentworld.workspace_ids"

_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)


def _protect(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant ON {table} USING ({_VISIBLE}) WITH CHECK ({_VISIBLE})"
    )


def _unprotect(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.create_table(
        "ws_tickets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # Who the socket will act as. Recorded for the audit trail and read
        # back on redemption; never taken from the ticket string.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # SHA-256 of the secret half, hex. Not Argon2, and the difference is
        # the threat: an API key is a long-lived low-entropy-adjacent bearer
        # token whose stolen digest must stay uncrackable, while this is 32
        # random bytes that expire in a minute and cannot be reused - there
        # is no password to recover and nothing to slow an attacker down
        # against. Argon2 here would add tens of milliseconds to every
        # connection to defend a value that is already uniform random.
        sa.Column("secret_digest", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        # NULL until redeemed. The conditional UPDATE that sets it is what
        # makes a ticket single-use.
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="ws_tickets_pkey"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="ws_tickets_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["projects.workspace_id", "projects.id"],
            name="ws_tickets_project_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="ws_tickets_user_id_fkey", ondelete="CASCADE"
        ),
    )
    # Spent and expired tickets are rubbish within a minute of being written.
    # Nothing sweeps them yet - the retention pass in a later phase is where
    # that belongs - so the index exists to keep that sweep cheap rather than
    # to serve a read: redemption is by primary key.
    op.create_index("ws_tickets_expiry_idx", "ws_tickets", ["expires_at"])
    _protect("ws_tickets")

    op.create_table(
        "event_publication_cursors",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # The highest `events.seq` whose frames have been handed to the bus.
        # Starts at 0, which is below the first event's seq of 1, so a project
        # with no row has published nothing and needs no backfill.
        sa.Column("published_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("published_seq >= 0", name="published_seq_is_not_negative"),
        sa.PrimaryKeyConstraint("project_id", name="event_publication_cursors_pkey"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="event_publication_cursors_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["projects.workspace_id", "projects.id"],
            name="event_publication_cursors_project_fkey",
            ondelete="CASCADE",
        ),
    )
    _protect("event_publication_cursors")


def downgrade() -> None:
    _unprotect("event_publication_cursors")
    op.drop_table("event_publication_cursors")
    _unprotect("ws_tickets")
    op.drop_index("ws_tickets_expiry_idx", table_name="ws_tickets")
    op.drop_table("ws_tickets")

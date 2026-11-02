"""`attempt` is an identity; claiming gets its own counter. And a dispatch
belongs to a step.

T0 / D4 and D5. One migration because both are one change to one table.

## D4 - the contradiction

Three statements about `dispatches` could not all be true at once:

1. claiming does `attempt = attempt + 1` on the row it claims;
2. a human retry inserts a **new** row at `attempt + 1`;
3. `UNIQUE (execution_step_id, kind, attempt)` is what makes a double
   `advance()` collide, so that one decision produces one dispatch.

If claiming mutates `attempt`, the unique key is not stable - so it stops
guarding creation, which is the only thing it was there for. Worse, a row
claimed once and incremented to `attempt = 1` then collides with the retry
row a human creates at `attempt = 1`, and the retry fails for a reason that
has nothing to do with the retry.

The resolution keeps the constraint meaningful: `attempt` is the *identity of
the attempt*, written once at insert and never updated. A `BEFORE UPDATE`
trigger enforces that, because discipline in the claiming SQL is not the
thing we want to rely on - the constraint exists for the case where the
worker is wrong. Lease bookkeeping moves to `claim_count`, which a worker may
increment as often as it likes without touching anything the unique key
depends on.

## D5 - NULLs are distinct

`execution_step_id` was nullable, and PostgreSQL treats NULLs as distinct in
a unique constraint. So `dispatches_step_attempt_key` constrained nothing at
all for a row with no step: two identical step-less dispatches were legal.

Every dispatch kind Phase 3 defines belongs to a step - a managed agent turn,
a tool call, an agent push, a task offer - and the table is empty, so
`NOT NULL` costs nothing now and is the strongest available statement of the
invariant. If a later phase genuinely needs an execution-level dispatch it
can add a partial unique index for that shape, with the case for it in front
of the reviewer. Leaving the column loose for a use that does not exist is
how the hole got here.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- D4 ---------------------------------------------------------------
    op.add_column(
        "dispatches",
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "dispatch_claim_count_is_not_negative", "dispatches", "claim_count >= 0"
    )
    op.execute(
        """
        CREATE FUNCTION agentworld_refuse_attempt_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $fn$
        BEGIN
          RAISE EXCEPTION
            'dispatch %: attempt is the identity of an attempt and cannot be updated '
            '(use claim_count for leases, or insert a new row to retry)', OLD.id
            USING ERRCODE = 'restrict_violation';
        END;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER dispatch_attempt_is_immutable
        BEFORE UPDATE ON dispatches
        FOR EACH ROW
        WHEN (OLD.attempt IS DISTINCT FROM NEW.attempt)
        EXECUTE FUNCTION agentworld_refuse_attempt_change();
        """
    )

    # ---- D5 ---------------------------------------------------------------
    op.alter_column("dispatches", "execution_step_id", nullable=False)


def downgrade() -> None:
    op.alter_column("dispatches", "execution_step_id", nullable=True)
    op.execute("DROP TRIGGER IF EXISTS dispatch_attempt_is_immutable ON dispatches")
    op.execute("DROP FUNCTION IF EXISTS agentworld_refuse_attempt_change()")
    op.drop_constraint("dispatch_claim_count_is_not_negative", "dispatches", type_="check")
    op.drop_column("dispatches", "claim_count")

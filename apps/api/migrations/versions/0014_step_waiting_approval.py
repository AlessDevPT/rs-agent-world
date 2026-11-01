"""`step_status` gains `waiting_approval`.

Phase 2, and a correction found while writing the reducers rather than while
designing the schema. A step held at a human gate had no status of its own:
`execution_status` has `waiting_approval`, so the *execution* could say it was
blocked, but the step could not say it was the one blocking. Leaving it at
`pending` or `dispatched` would make a step waiting on a person
indistinguishable from one that has not been reached, and the approval desk's
whole question is which step is waiting.

The alternative was to derive it by joining `execution_steps` against the
approvals table. That would make the island's hottest query depend on a table
that only sometimes has a row, to recover something a status column can say
directly.

Inserted `BEFORE 'succeeded'` rather than appended, which is the opposite of
what migration 0006 did and worth justifying. `step_status`'s label order is
its *lifecycle* order - pending, ready, dispatched, running, then the terminal
ones - and that is a property worth keeping: it is what makes `ORDER BY
status` mean something and what a reader checks the enum against. Appending
would have broken it to protect a UI that does not exist yet, since nothing
sorts by this enum before Phase 8. The schema drift test compares label order,
so the two definitions cannot quietly diverge either way.

`transaction_per_migration` in `env.py` is what makes adding a value and using
it safe in one upgrade - PostgreSQL refuses to use a newly added enum value in
the transaction that added it.

**Downgrading leaves the label in place**, which is what migration 0006 did
with the `agent_status` members it added, for the same reason: PostgreSQL has
no `ALTER TYPE ... DROP VALUE`, and rebuilding the type would mean rewriting
every column that uses it under an exclusive lock - destructive work with no
place in a downgrade path. An unused label on an enum costs nothing, while
refusing to reverse would block `alembic downgrade` for anyone resetting a
local database and buy no safety: the rows that already hold the value are
there either way.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE step_status ADD VALUE IF NOT EXISTS 'waiting_approval' BEFORE 'succeeded'"
    )


def downgrade() -> None:
    # Deliberately nothing. See the module docstring: the added label stays,
    # because removing it would rewrite `execution_steps`, and an unused enum
    # label is harmless. Rows already carrying the value survive a downgrade
    # either way, so refusing would only block a local reset.
    pass

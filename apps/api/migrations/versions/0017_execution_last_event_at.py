"""The four remaining folded tables gain `last_event_at`.

T0 / D2. Migration 0016 gave `tasks` a staleness column and said why; what it
did not do was notice that four more tables needed the same thing. `agents`
and `tasks` were guarded; `runs`, `run_steps`, `workflow_executions` and
`execution_steps` were not, and each of them carries a mutable `status` folded
straight from events. A late `workflow.step_started` therefore overwrote a
`workflow.step_succeeded`, and the step showed as running forever.

In Phase 2 that was a display error, because nothing read those statuses. It
stops being one in Phase 3: `advance()` reads a step's status to decide what
happens next, so a reverted status is a re-dispatch or a permanent stall
rather than a wrong colour on the island.

Nullable, no default. The default is the part that matters and the reason this
is its own migration rather than a line in 0016's: `now()` would make the
guard refuse every event about a row the API created first, which is exactly
the mistake 0016's docstring records.

**A timestamp guard alone is not enough here**, which is why the reducers also
gained `Patch.unless`. `not_before` compares with `<=`, so two events stamped
with the same `occurred_at` both apply and the last one wins - and a producer
that stamps a whole batch with one timestamp could still revert a terminal
step. Monotonicity on the status itself closes that; the column below closes
the ordinary late-arrival case. Both, because they fail differently.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every folded table that did not already have one. `agents` and `tasks` are
#: absent because they do.
TABLES: tuple[str, ...] = (
    "runs",
    "run_steps",
    "workflow_executions",
    "execution_steps",
)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("last_event_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "last_event_at")

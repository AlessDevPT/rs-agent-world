"""`tasks` gains `last_event_at`.

Phase 2, and a correction found by a test rather than by design.

Folding an event must not let a stale one overwrite a newer one: a worker
that was briefly offline flushes a backlog, the events arrive in the order it
managed to send them, and an `task.started` from ten minutes ago lands after
the `task.completed` that followed it. The guard for that is a predicate -
apply only if the row is not already more recent - and it needs a column that
means *as of when do we know this*.

`agents` already had one. `tasks` did not, and the first attempt guarded on
`updated_at` instead, which defaults to `now()`: a task created through the
API and then reported on has an `updated_at` newer than every event about it,
so the guard refused all of them. A guard that blocks everything looks exactly
like a guard that works until something needs to get through.

Nullable with no default, which is the whole point: null means nothing has
been folded yet, so anything is newer.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("last_event_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "last_event_at")

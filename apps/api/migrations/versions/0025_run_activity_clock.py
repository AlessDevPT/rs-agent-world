"""`runs` gains `last_activity_at`.

A second timestamp on the same row, and the reason it is a second one rather
than a wider meaning for the first is a failure mode the D4.3 audit found.

`runs.last_event_at` is the staleness guard for run-state transitions.
`_patch` compares it with `<=` against the incoming `occurred_at` *on the
same column it writes*, so `run.finished` lands only while
`last_event_at <= finished.occurred_at`. Today only `run.started`,
`run.finished` and `run.failed` move it, and a finish is never older than a
start, so the guard always lets a genuine ending through.

Had step and tool events advanced that column instead, a run whose newest
step is dated after its own finish could never be closed: the step moves the
clock to T7, the `run.finished` carries T5, `T7 <= T5` fails, and the run
stays `running` for ever. Ordinary clock skew between whatever reports steps
and whatever reports the run is enough to reach it - and a permanently
running run is precisely the condition stage D4 exists to detect. Creating
the disease to improve the diagnostic is a poor trade.

So: `last_event_at` keeps meaning "as of when do we know this row's *state*",
and this column means "as of when did we last hear anything *about* it" -
a step started or finished, a tool invoked, returned, failed or denied.
Guarded on itself by the reducers, so it only ever moves forwards.

Nullable with no default, and no backfill. Null means nothing has been heard
since the run opened, which is true both of a quiet new run and of every row
written before this column existed; a backfill from `run_steps` would be an
irreversible guess about rows nobody has asked about yet. Readers coalesce.

No index. This column has no reader until stage A2 points the quiet-runs
query at it, and that stage decides the query and the index together rather
than guessing at one now.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("last_activity_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "last_activity_at")

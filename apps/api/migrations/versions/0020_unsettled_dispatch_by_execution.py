"""An index for the question the advancement reconciler asks.

T6c. The reliability gap this serves is in `18-T6C-ADVANCEMENT-RECOVERY.md`:
settling or resolving a dispatch commits in one transaction and
`advance_once` runs in a second, because it decides from committed state. A
process that dies between them leaves an execution that is complete,
consistent and **stalled**, and nothing schedules the advance again.

The reconciler finds those executions by deriving the condition rather than
by reading a marker somebody had to remember to write:

    a `running` execution with no dispatch in `pending`, `leased` or
    `needs_review` is either owed an advance or already finished

Both readings are safe, because `advance()` returns no decisions for an
execution that needs nothing and `advance_once` then writes nothing.

Deriving it needs this index. None of the existing ones can answer a
correlation on `execution_id`:

| index | covers |
| --- | --- |
| `dispatches_claimable_idx` | `(run_after, id) WHERE status = 'pending'` |
| `dispatches_expired_lease_idx` | `(leased_until) WHERE status = 'leased'` |
| `dispatches_attention_idx` | `(project_id, status) WHERE status IN ('dead','needs_review')` |
| `dispatches_step_attempt_key` | `(execution_step_id, kind, attempt)`, unique |

The alternative was a marker column on the execution, set in the same
transaction as the settle and cleared by the advance. That was rejected: it
adds an invariant every present and future writer of a dispatch outcome has
to maintain, and a writer that forgets reintroduces exactly this gap -
silently, because a missing marker is indistinguishable from "nothing is
owed". An index can only be slow, and slow is measurable.

`execution_id` is nullable, so the predicate excludes NULLs: such a row can
never satisfy the correlation, and indexing it would only cost space.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX dispatches_unsettled_by_execution_idx
            ON dispatches (execution_id, status)
         WHERE execution_id IS NOT NULL
           AND status IN ('pending', 'leased', 'needs_review')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS dispatches_unsettled_by_execution_idx")

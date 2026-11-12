"""The index that makes "what has this agent done" a lookup rather than a scan.

`execution_steps.agent_id` has been written since 0009 and read by nothing.
Stage D2 gives it its first reader - `GET /v1/projects/{id}/agents/{id}/
executions`, which answers "which executions involved this agent" - and that
question has no index to answer it with: `execution_steps_execution_idx` leads
with `execution_id`, and `execution_steps_live_idx` with `project_id` and a
status predicate that a history read does not meet.

Without this the read is a sequential scan of the one table in the schema that
grows per step per attempt - a retried step adds a row, and a long graph adds
one per node - so it is the table where the difference between a lookup and a
scan grows fastest.

`(agent_id, execution_id)` rather than `(agent_id)` alone. The reader asks for
the distinct executions an agent appears in, so the second column lets
PostgreSQL satisfy it from the index: the step rows themselves are never
needed to answer the question, and a multi-step execution has several of them
per agent.

Partial, on `agent_id IS NOT NULL`, following `workflows_test_for_agent_key`
and `events_agent_idx`. A step that names no agent - every `kind` other than
`agent_task` - is not an answer to this question and has no business in the
index. The predicate is literal enough for the planner to match the reader's
own `agent_id = :id`, which implies it.

No `project_id` in the index. The reader carries the tenant predicate, as
every reader in `repositories/` does, and `agent_id` is a foreign key to
`agents` - a table already scoped by project - so the leading column is
selective on its own and adding the project would make the index wider
without making it find fewer rows.

DDL only, so no tenant variable: row-level security constrains DML, and
`CREATE INDEX` reads the heap rather than a policy-filtered view. 0023's note
on this applies unchanged, as does its note on locks: this takes a SHARE lock
on `execution_steps` for the build, blocking writes to it for the duration.
`CONCURRENTLY` cannot run inside a transaction and so cannot be part of an
atomic migration; at the size this table is now the trade is not worth making,
and a deployment large enough to care should split this revision.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Named for the column it leads with, like `runs_agent_idx` and
#: `workflow_steps_agent_idx` - the two other "this agent's rows" indexes.
AGENT_IDX = "execution_steps_agent_idx"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX {AGENT_IDX} ON execution_steps (agent_id, execution_id) "
        "WHERE agent_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {AGENT_IDX}")

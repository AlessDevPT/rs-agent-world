"""Two things about a workflow version the schema could not yet say.

Both are gaps the version lifecycle work found, and both are the same kind of
gap: a rule every writer in `services/` already keeps, kept nowhere a reader
can rely on.

**An execution's version must belong to its workflow.** `workflow_executions`
carries `workflow_id` and `workflow_version_id`, and until now the second was
bound to `workflow_versions (id)` alone - so a pair naming one workflow and
another workflow's version was storable, and an execution record could
describe a graph that is not a version of the workflow it claims to run. That
is the bug class 0008 closed for `workflows.current_version_id`, 0009 closed
for `execution_steps`, and 0012 closed for every `(workspace_id, project_id)`
pair; this is the one place it was left open. `published_version_by_id` has
been carrying the missing predicate in its WHERE clause since the pinning
fix, which is the right thing for a reader to do and no guarantee at all
about what is in the table.

The composite key replaces the single-column one rather than joining it, as
in 0012: `(workflow_id, workflow_version_id)` referencing
`workflow_versions (workflow_id, id)` already implies the version exists, so
keeping both would mean two constraints enforcing one thing and two rows in
every plan that deletes a version. No new unique key is needed either -
`workflow_versions_workflow_id_id_key` has existed since 0008 for
`workflows.current_version_id` to point at, and one unique key can serve any
number of references.

`ON DELETE RESTRICT` is carried over unchanged, so deletion behaves exactly
as it did: the frozen version an execution is bound to still cannot be
deleted while the execution references it. Both columns are NOT NULL, so
MATCH SIMPLE has no NULL row to wave through. The check gets *cheaper* to
run, incidentally: there is no index on `workflow_version_id` alone, while
`executions_workflow_idx` leads with `workflow_id`.

**A workflow has at most one open draft.** Every path that creates a version
creates exactly one draft - `create` and `find_or_create_test` open version 1,
and `next_draft` opens the one above what was just published - and
`save_draft` edits the open draft in place rather than adding another. So
`draft_of`'s "the newest unpublished version" has always been "the unpublished
version", and the ORDER BY is a tiebreak for a tie that is not supposed to
exist. A partial unique index says so, and makes a second draft - from a
concurrent publish, a retry, a future editor feature, a hand-written
UPDATE - a constraint violation instead of two editable copies of one
workflow, one of which is invisible to every reader.

Partial, because `published_at IS NULL` is the whole rule: published versions
are many per workflow by design, and the index covers one row per workflow
rather than one per version.

**This migration is DDL only, which is why it needs no tenant variable.**
0022 had to set `agentworld.workspace_ids` per workspace because it ran an
UPDATE, and in the single-role deployment `settings.py` documents, alembic
connects as the application role, which migration 0004 made subject to its
own FORCE'd policies. Row-level security constrains DML, not DDL: `ALTER
TABLE` and `CREATE INDEX` read the heap rather than a policy-filtered view,
and uniqueness is enforced over every row in the table whether a policy would
show it or not. That cuts both ways, and the second half is the useful one:
the index build is itself the duplicate check, and it is a check that cannot
be fooled. A `SELECT ... GROUP BY ... HAVING count(*) > 1` added here as a
friendlier pre-flight would return nothing at all under those policies and
report "no duplicates" for a table full of them.

So there is no cleanup and no pre-flight. If a database holds two drafts of
one workflow, `CREATE UNIQUE INDEX` fails naming the duplicated
`workflow_id`, and - with `transaction_per_migration` - takes the whole
migration back with it, leaving head at 0022 and nothing half-applied. The
same is true of `ADD CONSTRAINT`, which validates the rows already there:
a mismatched execution stops the migration rather than being quietly
tolerated. Deciding what to do with such a row is a judgement about somebody
else's data, and this migration is not the place to make it.

**Locks.** Both statements take ACCESS EXCLUSIVE on their table for the
duration: `ADD CONSTRAINT` scans `workflow_executions`, `CREATE UNIQUE INDEX`
builds over `workflow_versions`. The lock-light forms - `NOT VALID` followed
by a separate `VALIDATE CONSTRAINT`, and `CREATE INDEX CONCURRENTLY` - cannot
run inside a transaction, so they cannot be one atomic migration, and that
trade is not worth making at the size these tables are now. A deployment with
large enough tables to care should split this revision rather than run it as
is.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The constraint being replaced, from 0009: the version alone, which said
#: nothing about whose version it was.
OLD_VERSION_FKEY = "workflow_executions_version_id_fkey"

#: Its replacement. The naming follows 0012 and 0009's own composite keys -
#: `_version_fkey` rather than `_version_id_fkey`, because the key is a pair
#: and not a column.
VERSION_FKEY = "workflow_executions_version_fkey"

#: One open draft per workflow.
OPEN_DRAFT_KEY = "workflow_versions_open_draft_key"


def upgrade() -> None:
    op.execute(f"ALTER TABLE workflow_executions DROP CONSTRAINT IF EXISTS {OLD_VERSION_FKEY}")
    op.execute(
        f"ALTER TABLE workflow_executions ADD CONSTRAINT {VERSION_FKEY} "
        "FOREIGN KEY (workflow_id, workflow_version_id) "
        "REFERENCES workflow_versions (workflow_id, id) ON DELETE RESTRICT"
    )
    op.execute(
        f"CREATE UNIQUE INDEX {OPEN_DRAFT_KEY} "
        "ON workflow_versions (workflow_id) WHERE published_at IS NULL"
    )


def downgrade() -> None:
    """Back to 0009's weaker key, and no draft rule.

    In reverse order, and only these two objects: the unique key in
    `workflow_versions` is 0008's and is still what
    `workflows.current_version_id` points at, so it is not this migration's
    to drop.
    """
    op.execute(f"DROP INDEX IF EXISTS {OPEN_DRAFT_KEY}")
    op.execute(f"ALTER TABLE workflow_executions DROP CONSTRAINT IF EXISTS {VERSION_FKEY}")
    op.execute(
        f"ALTER TABLE workflow_executions ADD CONSTRAINT {OLD_VERSION_FKEY} "
        "FOREIGN KEY (workflow_version_id) "
        "REFERENCES workflow_versions (id) ON DELETE RESTRICT"
    )

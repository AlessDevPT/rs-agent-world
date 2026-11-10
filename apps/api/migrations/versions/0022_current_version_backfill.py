"""Every workflow that has published names what it published.

`workflows.current_version_id` has existed since migration 0008, with a
composite foreign key that guarantees the version it names belongs to the
workflow naming it - and until `feat(workflows): publishing says what it
published` no statement in the application ever wrote it. So the column is
correct for anything published since that commit and NULL for everything
published before it, which is every workflow in every database that predates
it. This migration closes that window.

**It is the prerequisite for reading the column.** While nothing resolves it,
a NULL there is invisible; the moment `ExecutionService.start` asks it for the
version to run, a NULL means "this workflow has never published" and a
workflow that published last month cannot be run at all. The backfill has to
precede that change, not accompany it.

The rule is the one `WorkflowRepository.published_version` already applies,
written out in SQL so the two cannot disagree: the published version with the
highest `version`. `UNIQUE (workflow_id, version)` makes that exactly one row.
A draft is never eligible - `published_at IS NOT NULL` - and a workflow with
no published version keeps its NULL, which is what NULL has always meant here.

**Row-level security is why this is a loop and not one UPDATE.** Migration
0004 chose FORCE over ENABLE so that a table's owner is subject to its own
policies, and `settings.py` documents the single-role deployment this makes
possible: `MIGRATION_DATABASE_URL` empty, migrations running as the same role
the application connects as. In that deployment `UPDATE workflows SET ...`
from a migration matches **no rows at all** - the policy keys on
`agentworld.workspace_ids`, which alembic does not set - and the backfill
reports success having done nothing. A silent no-op is the one outcome that
would survive review here and then break `start`, so the migration does what
`agentworld.infra.db` does for every request: it sets the session variable.

One workspace per iteration rather than every id in one string, because the
policy runs `string_to_array` on that variable for each row it tests, and
because a backfill that walks a known list is easier to reason about than one
whose correctness depends on how long a string got. Under a superuser the
policies are bypassed and the loop is simply a partition of the same work.

Then it checks itself. The loop's own predicates cannot report whether they
matched anything useful, and `UPDATE ... WHERE current_version_id IS DISTINCT
FROM` is indistinguishable from a no-op by design, so each iteration ends by
asking the database the question the migration exists to answer and raising if
the answer is still wrong. That is what makes "this ran" different from "this
was recorded as having run".

What it does not touch: `workflow_versions`, the graph tables, `published_at`,
`enabled`, `archived_at`, executions, and every other column of `workflows`.
Archived workflows and agent test harnesses are backfilled like anything else,
because both are workflows that have published and the column is a fact about
publishing, not about who may run what.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The session variable migration 0004's policies read. Spelled out rather
#: than imported from `agentworld.infra.db`: a migration never reads current
#: code, for the reason migration 0002 gives at length.
SETTING = "agentworld.workspace_ids"

BACKFILL = f"""
DO $$
DECLARE
    scope uuid;
    stranded uuid;
BEGIN
    FOR scope IN SELECT id FROM workspaces ORDER BY id LOOP
        PERFORM set_config('{SETTING}', scope::text, true);

        -- Rule 1: the published version with the highest number.
        UPDATE workflows AS w
           SET current_version_id = newest.id
          FROM (
                SELECT DISTINCT ON (v.workflow_id) v.workflow_id, v.id
                  FROM workflow_versions AS v
                 WHERE v.workspace_id = scope
                   AND v.published_at IS NOT NULL
                 ORDER BY v.workflow_id, v.version DESC
               ) AS newest
         WHERE w.id = newest.workflow_id
           AND w.workspace_id = scope
           AND w.current_version_id IS DISTINCT FROM newest.id;

        -- Rules 2 and 3: nothing published, nothing named. The composite
        -- foreign key keeps a pointer inside its own workflow but says
        -- nothing about `published_at`, so a pointer at a draft is a state
        -- the schema permits. Nothing can have created one - the only writer
        -- names a version it has just published - and if one exists anyway
        -- it is cleared here rather than left for `start` to execute.
        UPDATE workflows AS w
           SET current_version_id = NULL
         WHERE w.workspace_id = scope
           AND w.current_version_id IS NOT NULL
           AND NOT EXISTS (
                SELECT 1
                  FROM workflow_versions AS v
                 WHERE v.workflow_id = w.id
                   AND v.published_at IS NOT NULL
               );

        SELECT w.id
          INTO stranded
          FROM workflows AS w
         WHERE w.workspace_id = scope
           AND w.current_version_id IS DISTINCT FROM (
                SELECT v.id
                  FROM workflow_versions AS v
                 WHERE v.workflow_id = w.id
                   AND v.published_at IS NOT NULL
                 ORDER BY v.version DESC
                 LIMIT 1
               )
         LIMIT 1;

        IF stranded IS NOT NULL THEN
            RAISE EXCEPTION
                'workflow % does not name its newest published version after the backfill',
                stranded;
        END IF;
    END LOOP;

    -- Back to "no workspaces", which migration 0004 made mean "no rows"
    -- rather than "every row". The transaction would discard it anyway;
    -- leaving a tenant selected for whatever runs next is a habit worth not
    -- having.
    PERFORM set_config('{SETTING}', '', true);
END
$$;
"""


def upgrade() -> None:
    op.execute(BACKFILL)


def downgrade() -> None:
    """Deliberately nothing.

    There is no previous value to restore. Every pointer this migration writes
    was NULL, but so were the pointers written by publishing before it ran,
    and nothing in the schema records which is which - so the only available
    downgrades are "set them all to NULL", which throws away correct data the
    application wrote, and "do nothing". The column is nullable and no code
    path requires it to be set, so a database rolled back to 0021 with the
    pointers populated is in a state the application already handles: it is
    the state every publish since S2 produces.
    """

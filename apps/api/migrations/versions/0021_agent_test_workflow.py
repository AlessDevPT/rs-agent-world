"""The marker that makes an agent's test workflow system-owned.

Phase 4 Track A, `agent.test`. `03-API-AND-FRONTEND.md` §3 promises a test
that "is a real execution producing real events, not a preview", and
`workflow_executions.workflow_id` and `workflow_version_id` are both NOT
NULL - so a test needs a real published workflow. One reusable workflow per
agent, rather than one per run, which means the schema has to be able to say
"this workflow is the agent's test harness, not something a person authored".

**Nothing already in `workflows` can say it.** `created_by_user_id` is
already overloaded: it is `ON DELETE SET NULL`, so NULL means "the author's
account is gone" as much as it could mean "nobody authored this". `enabled`
is the trigger flag and is false for every new workflow. `agents.labels` is
editable by any member through `PATCH /agents/{id}`, so a marker kept there
is forgeable by the person it is meant to constrain. The remaining option
was a slug convention - and `domain/slugs.py` caps a slug at 48 characters
and truncates silently, while `agent_key` is itself slug-validated, so
`test-<agent_key>` can both overflow and collide. A collision would refuse
one agent's first test while naming another agent's workflow.

So: one nullable foreign key, which is also the identity, the uniqueness
guarantee and the listing filter.

`ON DELETE CASCADE`, because a test harness has no meaning once its agent is
gone. Note what this does *not* override: `workflow_steps.agent_id` is
`ON DELETE RESTRICT`, so once the test workflow is published its step
references the agent and the agent cannot be hard deleted at all. The
cascade therefore applies to the window before the first publish, and to a
workflow whose versions have since gone. Both deletion paths are asserted
rather than assumed.

The index is unique and partial. Unique over one nullable column would
already permit any number of NULLs - PostgreSQL does not consider two NULLs
equal - so the predicate is not what makes the uniqueness correct; it keeps
the index to the handful of rows that have a value instead of one entry per
workflow in the project. What the uniqueness buys is the `ON CONFLICT DO
NOTHING` that makes two concurrent first tests converge on one workflow
without a lock and without a 500 from a lost race.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE workflows
            ADD COLUMN test_for_agent_id uuid
                REFERENCES agents (id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX workflows_test_for_agent_key
            ON workflows (test_for_agent_id)
         WHERE test_for_agent_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS workflows_test_for_agent_key")
    op.execute("ALTER TABLE workflows DROP COLUMN IF EXISTS test_for_agent_id")

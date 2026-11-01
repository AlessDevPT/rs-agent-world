"""Make `workspace_id` and `project_id` agree, structurally.

Phase 2, and a correction to migrations 0003 and 0007-0011 rather than new
ground.

Every tenant-scoped table carries both columns: `workspace_id` because that is
what the row-level security policy reads, and `project_id` because that is the
domain scope. Until now each was validated on its own - two foreign keys, one
to `workspaces`, one to `projects` - and a foreign key checks existence, not
agreement. RLS checks `workspace_id` alone. So nothing stopped a row naming
one tenant's workspace and another tenant's project, and the probe that found
this inserted exactly that as the application role, with only its own
workspace visible:

    INSERT INTO tasks (workspace_id, project_id, title)
    VALUES (<mine>, <a project in someone else's workspace>, 'planted')

What that is and is not worth stating precisely, because the difference
matters. It is a **write** channel, not a read: the planted row carries the
writer's `workspace_id`, so the victim's queries filter it out and no data of
theirs is exposed. What it breaks is the claim the rest of the design rests
on - that tenant scope is enforced by the schema. With the pair
unconstrained, one wrong `workspace_id` in one service becomes a cross-tenant
write instead of a constraint violation, and `ON DELETE CASCADE` from a
project reaches rows in a workspace that project does not belong to.

The fix is a composite foreign key, `(workspace_id, project_id)` against a new
`UNIQUE (workspace_id, id)` on `projects`. It subsumes the single-column one,
which is therefore dropped rather than left alongside it. Where `project_id`
is nullable - `credentials` and `api_keys`, whose NULL means workspace-wide -
the constraint passes on a NULL by the default MATCH SIMPLE, which is exactly
the intent: a credential shared across a workspace has no project to agree
with.

**Three tables are deliberately not fixed here.** `events`, `logs` and
`metric_samples` have no foreign key to `projects` at all: they outlive the
rows they describe, and a cascade would delete history along with the project
it belonged to. A composite key would reintroduce that cascade over the
largest tables in the schema. So on those three the pair is enforced by
ingest, which is their only writer, and `test_tenant_pairs.py` asserts the
gap explicitly rather than leaving a reader to assume otherwise.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The unique key the composite foreign keys point at. `projects.id` is
#: already unique on its own; this pair exists only so a child row can be made
#: to reference the project *and* its workspace in one constraint.
PROJECT_KEY = "projects_workspace_id_id_key"

#: Every table carrying both columns and bound to `projects`, with the name of
#: the single-column constraint being replaced. `task_dependencies` had none -
#: its `project_id` was bound transitively through `tasks` - so it gains the
#: check rather than trading one for it.
RETARGETED: tuple[str, ...] = (
    "api_keys",
    "credentials",
    "agents",
    "connections",
    "workflows",
    "workflow_versions",
    "workflow_steps",
    "workflow_edges",
    "workflow_executions",
    "execution_steps",
    "dispatches",
    "tasks",
    "task_dependencies",
    "runs",
    "run_steps",
    "agent_status_history",
)

#: Why these three are left out - see the module docstring.
UNCONSTRAINED: tuple[str, ...] = ("events", "logs", "metric_samples")


def upgrade() -> None:
    op.execute(f"ALTER TABLE projects ADD CONSTRAINT {PROJECT_KEY} UNIQUE (workspace_id, id)")

    for table in RETARGETED:
        # IF EXISTS because `task_dependencies` never had one, and because a
        # database restored from before this migration may differ.
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_project_id_fkey")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_project_fkey "
            "FOREIGN KEY (workspace_id, project_id) "
            f"REFERENCES projects (workspace_id, id) ON DELETE CASCADE"
        )


def downgrade() -> None:
    for table in reversed(RETARGETED):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_project_fkey")
        # `task_dependencies` is the one table that had no single-column
        # constraint to restore; recreating one would not be a downgrade.
        if table != "task_dependencies":
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {table}_project_id_fkey "
                "FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE"
            )

    op.execute(f"ALTER TABLE projects DROP CONSTRAINT IF EXISTS {PROJECT_KEY}")

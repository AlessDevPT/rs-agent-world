"""A published workflow version cannot be edited.

T0 / D3. Until now this was a property the code intended and nothing
enforced: `published_at` was recorded, `WorkflowVersion.is_published` read
it, and `agentworld_app` held `UPDATE` *and* `DELETE` on
`workflow_versions`, `workflow_steps` and `workflow_edges`. There was no
trigger and no rule anywhere in the schema. The criterion passed only
because nothing published yet, which is not the same as passing.

It matters from Phase 3 onwards because an execution is bound to a version
for its whole life. Editing a published version under a running execution
changes what that execution *means* retroactively - its steps, its edges, the
expressions its conditions evaluate - while the event log goes on saying it
did something else.

**Why the application layer is not where this goes.** The app role can
`UPDATE` these tables, so a service-layer check is advisory: anything holding
a session can write around it, and the one thing we want to be true of a
published version is that *nothing* can.

**Why `SECURITY DEFINER`.** The graph trigger reads `workflow_versions` to
find out whether the parent is published. Under forced row-level security an
invoker-rights function would see no row when the session's visibility is
unset, `published_at` would read as NULL, and the guard would fail open.
Running as the owner makes the answer independent of who is asking. Same
reasoning, and the same `SET search_path`, as `agentworld_ensure_partitions`
in migration 0011.

## Two deliberate limits

**`UPDATE`, not `DELETE`.** `workflow_versions` is reachable by
`ON DELETE CASCADE` from `projects` and `workspaces`. A trigger that raised
on delete would make a hard delete of a workspace fail partway through its
cascade - turning an administrative operation into a half-applied one, which
is worse than the hole it closes. What covers deletion instead:

  * `workflow_executions.workflow_version_id` is `ON DELETE RESTRICT`, so a
    version that has ever *run* cannot be deleted at all.
  * the application never hard-deletes a project. `ProjectRepository` has
    `archive()` and no `delete()`.

The residual hole, stated rather than hidden: a published version that has
never run can still be removed by a direct `DELETE` or an administrative
cascade. Editing it - the thing that would silently change what a running
execution means - cannot happen.

**Publishing must insert the graph first.** The trigger refuses an `INSERT`
into `workflow_steps` or `workflow_edges` whose parent is already published,
so a publish has to normalise the draft into those tables and only then set
`published_at`. The reverse order makes the publish refuse its own inserts.
This is a constraint on Phase 3's publish path and is written here so it is
discovered by reading rather than by debugging.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The tables whose rows belong to a version rather than being one.
GRAPH_TABLES: tuple[str, ...] = ("workflow_steps", "workflow_edges")


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION agentworld_refuse_published_version_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $fn$
        BEGIN
          RAISE EXCEPTION
            'workflow version % is published and cannot be changed', OLD.id
            USING ERRCODE = 'restrict_violation';
        END;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION agentworld_refuse_published_graph_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          target uuid;
          published timestamptz;
        BEGIN
          -- COALESCE so one function serves INSERT (NEW only) and UPDATE.
          target := COALESCE(NEW.workflow_version_id, OLD.workflow_version_id);
          SELECT wv.published_at INTO published
            FROM public.workflow_versions wv
           WHERE wv.id = target;
          IF published IS NOT NULL THEN
            RAISE EXCEPTION
              'workflow version % is published; its graph cannot be changed', target
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END;
        $fn$;
        """
    )

    # `WHEN (OLD.published_at IS NOT NULL)` is what lets publishing happen:
    # at that moment the old value is still NULL, so the trigger stays quiet.
    # Re-publishing an already-published version is refused, which is correct.
    op.execute(
        """
        CREATE TRIGGER workflow_version_is_immutable_once_published
        BEFORE UPDATE ON workflow_versions
        FOR EACH ROW
        WHEN (OLD.published_at IS NOT NULL)
        EXECUTE FUNCTION agentworld_refuse_published_version_change();
        """
    )
    for table in GRAPH_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_are_immutable_once_published
            BEFORE INSERT OR UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION agentworld_refuse_published_graph_change();
            """
        )


def downgrade() -> None:
    for table in GRAPH_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_are_immutable_once_published ON {table}")
    op.execute(
        "DROP TRIGGER IF EXISTS workflow_version_is_immutable_once_published ON workflow_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS agentworld_refuse_published_graph_change()")
    op.execute("DROP FUNCTION IF EXISTS agentworld_refuse_published_version_change()")

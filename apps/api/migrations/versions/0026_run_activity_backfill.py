"""`runs.last_activity_at` learns what the trace already knew.

Migration 0025 added the column and backfilled nothing, for a reason it states:
a backfill from `run_steps` would have been "an irreversible guess about rows
nobody reads yet". Stage A2 is when somebody reads them, and the first half of
that sentence turns out not to hold either - a step's `last_event_at` *is* an
activity timestamp for its run, by the identical rule `_stirred` now applies to
every step event, so copying it forward is arithmetic rather than inference.
Nor is it irreversible: `0025 downgrade` drops the column and this backfill
with it, which is the one property that makes a data migration safe to write
against a column that is one revision old.

**What this establishes is an invariant, not a feature.** After it,

    max(run_steps.last_event_at) <= runs.last_activity_at

holds for every run with any timestamped step, in every workspace, at every
status. Nothing reads the column yet; `RunRepository.quiet_in_project` still
filters through its lateral over `run_steps` and the endpoint's answers are
unchanged to the second. That is deliberate. Stage A2.2 is allowed to delete
that lateral and index the run-level expression *because* this invariant holds,
and it cannot be reviewed on its own merits until the invariant it rests on is
established and tested separately from it.

Without the backfill the gap is not academic. A run that has been reporting
steps for an hour but existed before 0025 carries `last_activity_at IS NULL`,
so a reader coalescing to the run's own clock sees the moment it opened and
calls an hour-old busy run quiet - the exact false positive stage D4 exists to
avoid, reintroduced by the stage meant to remove it.

**Monotonic, so it cannot undo the reducers.** The update fires only where the
existing clock is NULL or strictly older, so a value `_stirred` has already
written always wins and running this twice changes nothing. Under READ
COMMITTED that predicate is re-evaluated against the locked row, so a fold
committing between this statement's plan and its write cannot be clobbered
either.

**Every status, not only `running`.** The column records what was heard, not
what some listing may show. Restricting it to live runs would leave settled
rows permanently inconsistent, would weaken the check below into a partial one,
and would strand any run that settles between this migration and its first
reader.

**Two guards, because they catch different failures and neither catches both.**

Migration 0004 chose FORCE over ENABLE so a table's owner is subject to its own
policies, and `settings.py` documents the single-role deployment that makes
possible: `MIGRATION_DATABASE_URL` empty, alembic connecting as the role the
application uses. In that deployment an `UPDATE runs` from a migration matches
no rows at all, because the policy keys on `agentworld.workspace_ids` and
alembic sets nothing - so the backfill reports success having done nothing.
Migration 0022 met this and answered it with a per-workspace `set_config` plus
a self-check for stranded rows.

The stranded check alone does not close it. It reads `runs` through the very
policy that hid the rows from the UPDATE, so with no tenant set it finds
nothing stranded and raises nothing; measured on a scratch database as the
application role, that statement completes successfully and writes nothing.
`workspaces` carries no policy, so the loop still turns - which is what makes
the failure quiet rather than loud. Hence the first guard: after `set_config`,
ask the database what the setting now is. It is data-independent, so it fires
on an empty table and on a full one alike.

The stranded check stays, because it catches what the other cannot: a rule
written wrongly, an aggregate over the wrong column, a predicate that lets a
row slip past. One guard proves the statement could see; the other proves it
did the right thing with what it saw.

Both statements restrict `run_steps` to the workspace in hand, and that has
to stay true of both. Nothing in the schema ties a step's workspace to its
run's - `run_id` and the composite `(workspace_id, project_id)` key are
satisfied independently - so a `run_steps` row whose `workspace_id` disagrees
with its run's is permitted. A check scoped more widely than the update it
validates would strand on exactly that row, on every attempt, failing an
upgrade no retry could clear.

It runs inside the loop, after the update, while the tenant is still selected.
After the loop the setting is `''` - which 0004 made mean *no rows* rather than
every row - so a check placed there would see nothing and pass always.

What this does not touch: `last_event_at`, `started_at`, `finished_at`,
`status`, `step_count`, `tool_call_count`, `error`, every row of `run_steps`,
and every run that has no timestamped step. No DDL, no index, no default.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
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

        -- Guard 1: could this statement see anything at all? Asking the
        -- database rather than trusting the line above, because the failure
        -- this catches is invisible in the data: under FORCE ROW LEVEL
        -- SECURITY an unset tenant makes every UPDATE below match nothing
        -- and every check below find nothing.
        IF current_setting('{SETTING}', true) IS DISTINCT FROM scope::text THEN
            RAISE EXCEPTION
                'the tenant setting did not take for workspace %, so this backfill '
                'would match no rows under FORCE ROW LEVEL SECURITY', scope;
        END IF;

        -- The newest step timestamp per run, copied forward. A run with no
        -- step rows produces no candidate here and is therefore untouched -
        -- the shape is the guarantee, so no predicate states it. `max`
        -- ignores NULLs, and a run whose every step clock is NULL yields a
        -- NULL group, which `newest.latest IS NOT NULL` drops rather than
        -- writing a NULL over a NULL.
        UPDATE runs AS r
           SET last_activity_at = newest.latest
          FROM (SELECT s.run_id, max(s.last_event_at) AS latest
                  FROM run_steps AS s
                 WHERE s.workspace_id = scope
                 GROUP BY s.run_id) AS newest
         WHERE r.id = newest.run_id
           AND r.workspace_id = scope
           AND newest.latest IS NOT NULL
           AND (r.last_activity_at IS NULL OR r.last_activity_at < newest.latest);

        -- Guard 2: is the invariant true now? The predicate is the invariant
        -- itself, negated, so "needed backfilling" and "was backfilled" are
        -- one question asked twice. `max` over no rows is NULL and
        -- `NULL > anything` is NULL, so a run with no steps cannot strand.
        --
        -- `s.workspace_id = scope` is here because the aggregate above has
        -- it: the two statements have to be asked about the same rows, or
        -- the check fails on a step the update deliberately skipped.
        SELECT r.id
          INTO stranded
          FROM runs AS r
         WHERE r.workspace_id = scope
           AND (SELECT max(s.last_event_at)
                  FROM run_steps AS s
                 WHERE s.run_id = r.id
                   AND s.workspace_id = scope)
               > COALESCE(r.last_activity_at, '-infinity'::timestamptz)
         LIMIT 1;

        IF stranded IS NOT NULL THEN
            RAISE EXCEPTION
                'run % still has trace activity its clock does not cover', stranded;
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

    There is no previous value to restore. Every clock this migration writes
    was NULL - but so is every clock `_stirred` has not yet written, and
    nothing in the schema records which NULL is which. The two available
    downgrades are therefore "set them all to NULL", which destroys correct
    data the application wrote since 0025, and "do nothing".

    Leaving the column populated is a state the application already handles:
    it is the state folding produces. Every reader coalesces, nothing requires
    the column to be NULL, and no constraint distinguishes a backfilled value
    from a folded one.

    The real rollback is `0025 downgrade`, which drops the column and takes
    this backfill with it. That is also the answer to 0025's own objection
    that a backfill from `run_steps` would be irreversible: it is reversible
    precisely because the column it writes is one revision old.
    """

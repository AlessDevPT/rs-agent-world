"""Row-level security on the tenant-scoped tables.

`docs/02-DATABASE.md` section 7 calls this defence in depth, and means it: the
primary control is that every repository method takes an explicit
`TenantScope` and the type signature will not let you call one without it.
This migration is what catches the query that satisfies the type checker and
still forgets its `WHERE`.

Brought forward from the planned `0008` on purpose. Enabling RLS on a live
schema means auditing every query already written against every table; doing
it before there is anything to audit costs nothing.

Three decisions recorded here, because each one is the kind that looks
arbitrary a year later:

**FORCE, not just ENABLE.** A table's owner bypasses its own policies.
AgentWorld's application role owns these tables - a self-hosted single-role
deployment is the normal case - so `ENABLE` alone would create policies that
are never consulted and a test suite that passes for the wrong reason. That is
worse than no RLS, because it reads like protection. `FORCE` makes the owner
subject to them too; superusers and roles with `BYPASSRLS` still bypass, which
is what lets an operator run a repair query.

**Keyed on a set of workspaces, not one.** The session variable holds every
workspace the caller can see. `agentworld.infra.db` explains why at length;
the short version is that `GET /v1/projects/{id}` has to read `projects` in
order to learn which workspace it belongs to, and a single-id policy makes
that read return nothing. Every alternative needs a bypass, and a bypass is a
backdoor somebody eventually forgets to close.

**The credential tables are not covered.** The rule is that RLS covers tenant
*data*, and not the tables that establish which tenant a request belongs to.
Those are `users`, `workspace_members`, `workspaces` and `api_keys`, and each
is read before any tenant is known: a token carries a subject, so `users` and
`workspace_members` produce the very set these policies read; a presented API
key carries a globally unique prefix, so `api_keys` is where the ingest plane
learns its workspace. A policy on any of them would need the answer before the
question. Covering them would take a `SECURITY DEFINER` resolver or a role
with `BYPASSRLS`, and either is a bypass that something eventually reaches for
a second time.

What protects them instead is the application layer: `users` and
`workspace_members` are reached only through `GlobalRepository` subclasses,
and `api_keys` is split so that its entire cross-tenant surface is one method
on `ApiKeyVerificationRepository` - a lookup by a unique prefix, which reveals
nothing to a caller who does not already hold the key. Everything else goes
through the scoped repository with an explicit predicate.

`projects` and `audit_log` are where tenant data begins, so they are covered
here, and Phase 2's domain tables join them.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen at this revision. Later migrations add their own tables with their
#: own policies rather than editing this list - see the note in 0002 about why
#: a migration never reads current code.
PROTECTED_TABLES: tuple[str, ...] = ("projects", "audit_log")

SETTING = "agentworld.workspace_ids"

#: NULLIF turns the "no workspaces" case into SQL NULL instead of a cast error
#: on an empty string, and `= ANY (NULL)` evaluates to NULL, which RLS reads
#: as "no rows". The failure mode of an unset session variable is therefore
#: total invisibility rather than total visibility.
_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)


def upgrade() -> None:
    for table in PROTECTED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # One policy for all commands. USING filters what is readable and
        # what UPDATE/DELETE may touch; WITH CHECK stops a row being written
        # into a workspace the caller cannot see, which is the half that an
        # INSERT-only policy would miss.
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} USING ({_VISIBLE}) WITH CHECK ({_VISIBLE})"
        )


def downgrade() -> None:
    for table in reversed(PROTECTED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

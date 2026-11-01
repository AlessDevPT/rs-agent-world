"""Privileges for the application role.

Migrations run as the schema owner; the API connects as `agentworld_app`,
which owns nothing and is neither a superuser nor `BYPASSRLS`. That split is
what makes migration 0004's policies real - see
`infra/compose/postgres-init/01-app-role.sql` for the full reasoning. This
migration is the other half: the owner granting that role exactly the
privileges it needs.

Two details worth the lines:

**The grant is conditional.** A deployment that runs the application as the
owner (the simplest self-hosted setup, where FORCE ROW LEVEL SECURITY is
enough on its own) has no such role, and a first migration that fails because
of a role it does not use would be a poor welcome. A different role name needs
the same four statements run by hand; they are listed in the docstring of
`_grant` rather than hidden behind configuration nobody will find.

**`audit_log` is granted SELECT and INSERT only.** Append-only is a property
worth more than a comment saying so. Without UPDATE or DELETE the application
role *cannot* rewrite history even if a future code path tries, and the
attempt fails loudly at the database rather than succeeding quietly. Phase 2's
`events` and `logs` tables are granted the same way.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "agentworld_app"

#: Tables the application may read and append to, but never modify or delete
#: from. Frozen at this revision; later migrations list their own.
APPEND_ONLY_TABLES: tuple[str, ...] = ("audit_log",)


def upgrade() -> None:
    op.execute(_conditional(_grants()))


def downgrade() -> None:
    op.execute(_conditional(_revocations()))


def _grants() -> list[str]:
    statements = [
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
        # Without these, every future migration's tables would be invisible to
        # the application until someone remembered to grant them.
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}",
    ]
    statements += [
        f"REVOKE UPDATE, DELETE ON {table} FROM {APP_ROLE}" for table in APPEND_ONLY_TABLES
    ]
    return statements


def _revocations() -> list[str]:
    return [
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {APP_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {APP_ROLE}",
    ]


def _conditional(statements: list[str]) -> str:
    """Wrap statements so they run only if the application role exists.

    `EXECUTE` rather than the bare statements: plpgsql accepts most utility
    commands directly, but `ALTER DEFAULT PRIVILEGES` is not among them.
    """
    body = "\n".join(f"    EXECUTE {_quote(statement)};" for statement in statements)
    return (
        "DO $$\nBEGIN\n"
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote(APP_ROLE)}) THEN\n"
        f"{body}\n"
        "  END IF;\nEND $$;"
    )


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

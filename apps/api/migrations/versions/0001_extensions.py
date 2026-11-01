"""Extensions: pgcrypto and citext.

Both are "trusted" extensions from PostgreSQL 13 on, which means the owner of
the database can create them without superuser rights. That matters for
self-hosting and for managed providers: needing superuser to run the first
migration would rule out most of them.

`IF NOT EXISTS` because a managed database may ship with one already present,
and a first migration that fails on a perfectly good database is a bad first
impression.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # gen_random_uuid() for primary keys.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    # Case-insensitive text for emails, slugs and agent keys.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")


def downgrade() -> None:
    # Not dropped. An extension is database-wide: another schema in the same
    # database may depend on it, and dropping it would cascade into tables
    # this migration knows nothing about.
    pass

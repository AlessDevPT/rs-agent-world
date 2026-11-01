"""Native enum types.

Every enum in the system is created here, including the ones whose tables
arrive in later phases, so that the vocabulary does not end up spread across
five migrations.

The labels below are written out as literals rather than read from
`agentworld.domain.enums`. Importing the live enums would be shorter and would
look like it removed a duplication, but it would quietly break the migration
chain: when Phase 2 adds a member to `agent_status`, this migration would
start creating it on a fresh database, and the Phase 2 `ALTER TYPE ... ADD
VALUE` would then fail there while still succeeding on an existing one. A
migration is a frozen record of a change, not a view of current code.

The duplication is instead *checked*: `tests/integration/test_schema.py` reads
`pg_enum` from a migrated database and asserts it matches
`domain.enums.PG_ENUMS` label for label and in order. A Python member added
without a migration fails CI.

Native types rather than check constraints or plain text, because they appear
in `information_schema`: SQLAlchemy reflects them, FastAPI publishes them in
`/openapi.json`, and the generated TypeScript gets a real union type. A typo
in a member becomes a build error in the web app instead of a row that
silently never matches.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen as of this revision. Later revisions ALTER these; they never edit
#: this table.
ENUMS: dict[str, tuple[str, ...]] = {
    "member_role": ("viewer", "member", "admin", "owner"),
    "agent_status": (
        "provisioning",
        "offline",
        "idle",
        "working",
        "blocked",
        "error",
        "maintenance",
    ),
    "task_status": (
        "pending",
        "queued",
        "running",
        "blocked",
        "completed",
        "failed",
        "cancelled",
    ),
    "task_priority": ("low", "normal", "high", "urgent"),
    "run_status": ("running", "succeeded", "failed", "cancelled"),
    "event_severity": ("debug", "info", "warning", "error", "critical"),
    "api_key_scope": ("ingest", "control", "full"),
    "delivery_status": ("pending", "delivered", "failed", "dead"),
}


def upgrade() -> None:
    for type_name, labels in ENUMS.items():
        rendered = ", ".join(f"'{label}'" for label in labels)
        op.execute(f"CREATE TYPE {type_name} AS ENUM ({rendered})")


def downgrade() -> None:
    for type_name in reversed(list(ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")

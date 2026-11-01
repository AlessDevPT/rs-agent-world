"""Native enum types for the ecosystem model.

Phase 2. The thirteen types the workflow, connection, tool, trigger,
credential, approval and dispatch tables bind to, created ahead of those
tables for the same reason `0002` created Phase 1's ahead of theirs: the
vocabulary of the system belongs in one place, not spread across the
migrations that happen to need each word first.

As in `0002`, the labels are written out as **literals** rather than read
from `agentworld.domain.enums`. Importing the live enums would look like
removing a duplication and would quietly break the migration chain: when a
later phase adds a member to `dispatch_status`, this migration would start
creating it on a fresh database and that phase's `ALTER TYPE ... ADD VALUE`
would fail there while still succeeding on an existing one. A migration is a
frozen record of a change, not a view of current code.

The duplication is checked instead. `tests/integration/test_schema.py` reads
`pg_enum` from a migrated database and asserts it matches
`domain.enums.PG_ENUMS` label for label and in declared order, in both
directions - so a Python member added without a migration fails CI, and so
does a type created here that no code knows about.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen as of this revision. Order matters: PostgreSQL compares enum values
#: by declared order, so `ORDER BY status` and any range comparison depend on
#: it, and the drift test asserts the order rather than just the membership.
ENUMS: dict[str, tuple[str, ...]] = {
    # What AgentWorld may do with an agent, and what it must not claim to.
    "agent_interaction_mode": ("managed", "dispatchable", "observed"),
    "step_kind": (
        "agent_task",
        "tool_call",
        "approval",
        "condition",
        "transform",
        "wait",
    ),
    "execution_status": (
        "pending",
        "running",
        "waiting_approval",
        "waiting_input",
        "paused",
        "completed",
        "failed",
        "cancelled",
    ),
    "step_status": (
        "pending",
        "ready",
        "dispatched",
        "running",
        "succeeded",
        "failed",
        "skipped",
        "cancelled",
    ),
    # Two values and no NULL. A system-initiated execution borrows no user's
    # authority, so there is no third case to represent.
    "initiator_kind": ("user", "system"),
    "approval_decision": ("pending", "granted", "denied", "expired"),
    "connection_kind": (
        "grant_tool",
        "grant_knowledge",
        "delegate",
        "trigger",
        "publish",
    ),
    "tool_source": ("builtin", "mcp", "integration"),
    # `consequential` is the class that defaults to requiring approval.
    "side_effect": ("read", "write", "consequential"),
    "trigger_kind": (
        "manual",
        "schedule",
        "webhook",
        "integration_event",
        "workflow_completed",
    ),
    "credential_kind": ("api_key", "oauth2", "basic", "custom"),
    "dispatch_kind": (
        "managed_agent_turn",
        "tool_call",
        "agent_push",
        "agent_task_offer",
        "webhook_emit",
    ),
    # `needs_review` is an ambiguous consequential side effect: the lease
    # expired mid-flight and nobody knows whether it landed. Never retried
    # automatically.
    "dispatch_status": (
        "pending",
        "leased",
        "succeeded",
        "failed",
        "dead",
        "needs_review",
        "cancelled",
    ),
}

#: `agent_status` gains a member rather than being recreated. ALTER TYPE ...
#: ADD VALUE cannot be rolled back, which is why `downgrade` does not try.
NEW_AGENT_STATUSES: tuple[str, ...] = ("paused",)


def upgrade() -> None:
    for type_name, labels in ENUMS.items():
        rendered = ", ".join(f"'{label}'" for label in labels)
        op.execute(f"CREATE TYPE {type_name} AS ENUM ({rendered})")

    for label in NEW_AGENT_STATUSES:
        # IF NOT EXISTS so re-running against a partially migrated database
        # is not a hard failure. Added after `maintenance` so the declared
        # order stays append-only, which the drift test checks.
        op.execute(f"ALTER TYPE agent_status ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    for type_name in reversed(list(ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")

    # The added `agent_status` members are deliberately not removed.
    # PostgreSQL has no ALTER TYPE ... DROP VALUE, and rebuilding the type
    # would mean rewriting every column that uses it - a destructive
    # operation that has no place in a downgrade path.

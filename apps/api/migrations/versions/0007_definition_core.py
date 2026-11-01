"""Definition core: credentials, model profiles, agents, connections.

Phase 2. The four tables everything else in the ecosystem model points at,
and the first tenant tables since Phase 1 - so they are also the first to
exercise the rule that each migration enables row-level security on its own
tables as it creates them, rather than one sweep at the end.

Three things here are load-bearing rather than incidental:

**`agents.model_profile_id` is valid only for a `managed` agent**, enforced by
a check constraint. A `dispatchable` or `observed` agent chooses its own
model, and AgentWorld never calls it - so recording a model profile for one
would imply a choice this system does not make and cannot enforce. The
constraint is how "an agent's runtime and its model provider are separate
axes" stops being a sentence in a design document.

**`credentials` stores recoverable material and `api_keys` does not.** They
are deliberately different mechanisms: an API key must be verifiable and
unreadable even by this server, and an integration credential must be usable
by it. Nothing in this table is readable without the key-encryption key named
by `key_id`, which lives in the environment and never here.

**`connections` uses polymorphic references.** `source_id` and `target_id`
carry no foreign key, because the set of connectable kinds grows with the
product and five pairs of nullable columns would not. The cost is stated
rather than hidden: the database cannot enforce referential integrity on
either end, so the service layer validates that both exist in the same
project, and a later maintenance job reports orphans.

`agents.current_task_id` is a bare uuid for now. The foreign key to `tasks`
is added by the migration that creates that table, because the reference is
circular.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created by 0002 and 0006; `create_type=False` stops Alembic emitting a
# CREATE TYPE as a side effect of each table.
_AGENT_STATUS = ENUM(name="agent_status", create_type=False)
_INTERACTION_MODE = ENUM(name="agent_interaction_mode", create_type=False)
_CONNECTION_KIND = ENUM(name="connection_kind", create_type=False)
_CREDENTIAL_KIND = ENUM(name="credential_kind", create_type=False)

#: Tenant tables created here. Each gets RLS enabled, forced and policied, by
#: the same shape migration 0004 established. Frozen at this revision.
PROTECTED_TABLES: tuple[str, ...] = (
    "credentials",
    "model_profiles",
    "agents",
    "connections",
)

SETTING = "agentworld.workspace_ids"

#: NULLIF turns "no workspaces" into SQL NULL rather than a cast error on an
#: empty string, and `= ANY (NULL)` is NULL, which RLS reads as no rows. The
#: failure mode of an unset session variable is total invisibility.
_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)


def upgrade() -> None:
    _credentials()
    _model_profiles()
    _agents()
    _connections()

    for table in PROTECTED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} USING ({_VISIBLE}) WITH CHECK ({_VISIBLE})"
        )


def downgrade() -> None:
    for table in reversed(PROTECTED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("connections")
    op.drop_table("agents")
    op.drop_table("model_profiles")
    op.drop_table("credentials")


def _credentials() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        # NULL means workspace-wide, which is the common case for a provider
        # key shared by every project.
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", _CREDENTIAL_KIND, nullable=False),
        sa.Column("provider_slug", sa.Text(), nullable=False),
        # Envelope encryption. `ciphertext` is AES-256-GCM over the secret
        # under a per-row data key; `wrapped_key` is that key encrypted by the
        # KEK named in `key_id`, which is NOT in this database. The GCM
        # additional authenticated data binds the ciphertext to
        # workspace_id || id || aad_version, so a row copied into another
        # workspace cannot be decrypted in its new home.
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("aad_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        # Readable facts, so the UI shows something useful without decrypting.
        sa.Column("display_hint", sa.Text(), nullable=True),
        sa.Column("scopes", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("refresh_after", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        # A tombstone. Deleting zeroes the key material and keeps the row,
        # because audit_log references credential ids and a dangling
        # reference in an audit trail is worse than a tombstone.
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="credentials_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="credentials_project_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="credentials_created_by_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["users.id"],
            name="credentials_revoked_by_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="credentials_pkey"),
        sa.UniqueConstraint("workspace_id", "provider_slug", "name", name="credentials_name_key"),
    )
    op.create_index(
        "credentials_refresh_due_idx",
        "credentials",
        ["refresh_after"],
        postgresql_where=sa.text(
            "revoked_at IS NULL AND deleted_at IS NULL AND refresh_after IS NOT NULL"
        ),
    )


def _model_profiles() -> None:
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        # What a beginner picks from: "Fast drafting", "Careful reasoning".
        sa.Column("name", sa.Text(), nullable=False),
        # Opaque to the domain layer: the provider plugin in
        # infra/providers/<slug>.py owns what these mean, which is how
        # domain/ keeps importing no provider SDK.
        sa.Column("provider_slug", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("parameters", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # RESTRICT: a credential in use by a profile cannot be deleted out
        # from under the agents that depend on it. Revoke it instead, which
        # fails the next dispatch loudly with a reason.
        sa.Column("credential_id", sa.Uuid(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="model_profiles_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["credentials.id"],
            name="model_profiles_credential_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="model_profiles_pkey"),
        sa.UniqueConstraint("workspace_id", "name", name="model_profiles_name_key"),
    )
    # Exactly one default per workspace, as a database guarantee rather than a
    # service-layer intention - the same trick as workspace_single_owner.
    op.create_index(
        "one_default_model_profile",
        "model_profiles",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )


def _agents() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # Caller-chosen stable identity, so a restarted or scaled-out worker
        # keeps its identity without persisting anything. citext, so `Worker`
        # and `worker` cannot both exist.
        sa.Column("agent_key", CITEXT(), nullable=False),
        sa.Column("interaction_mode", _INTERACTION_MODE, nullable=False),
        # ---- definition: user-editable, control plane only ----
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        # The system prompt, for a managed agent.
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("model_profile_id", sa.Uuid(), nullable=True),
        sa.Column(
            "model_overrides", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("labels", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # ---- declared interface ----
        # What this agent can be asked to do. The API refuses commands and
        # step assignments outside it rather than issuing them hopefully.
        sa.Column(
            "capabilities", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("dispatch", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # Descriptive only. The core never branches on it, which is what lets
        # a dispatchable agent say "I run on Gemini" without this system
        # needing to support Gemini.
        sa.Column("runtime", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # ---- runtime state: reducers only ----
        sa.Column(
            "status",
            _AGENT_STATUS,
            server_default=sa.text("'provisioning'::agent_status"),
            nullable=False,
        ),
        sa.Column("status_reason", sa.Text(), nullable=True),
        # The FK to tasks is added by the migration that creates tasks; the
        # reference is circular.
        sa.Column("current_task_id", sa.Uuid(), nullable=True),
        sa.Column("last_heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "heartbeat_ttl_seconds", sa.Integer(), server_default=sa.text("60"), nullable=False
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "registered_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Only a managed agent has a model, because it is the only mode where
        # AgentWorld calls one. See the module docstring.
        sa.CheckConstraint(
            "interaction_mode = 'managed' OR model_profile_id IS NULL",
            name="model_profile_only_when_managed",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="agents_workspace_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="agents_project_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_id"],
            ["model_profiles.id"],
            name="agents_model_profile_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name="agents_created_by_fkey", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="agents_pkey"),
        sa.UniqueConstraint("project_id", "agent_key", name="agents_project_key_key"),
    )
    op.create_index(
        "agents_project_status_idx",
        "agents",
        ["project_id", "status"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_index("agents_labels_idx", "agents", ["labels"], postgresql_using="gin")
    # Drives the offline sweep: cheap to scan, no full table pass.
    op.create_index(
        "agents_heartbeat_watch",
        "agents",
        ["last_heartbeat_at"],
        postgresql_where=sa.text(
            "archived_at IS NULL AND status NOT IN ('offline', 'maintenance', 'paused')"
        ),
    )


def _connections() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("kind", _CONNECTION_KIND, nullable=False),
        # Polymorphic, deliberately: see the module docstring for the
        # referential-integrity trade this makes.
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("config", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # A grant may itself be gated: "this agent may send email, but every
        # send needs approval". The gate lives with the grant, not the UI.
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="connections_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="connections_project_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="connections_created_by_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="connections_pkey"),
        sa.UniqueConstraint(
            "project_id",
            "kind",
            "source_kind",
            "source_id",
            "target_kind",
            "target_id",
            name="connections_edge_key",
        ),
    )
    # The lookup the orchestrator makes before every tool_call dispatch:
    # "does this agent hold a grant for this tool".
    op.create_index(
        "connections_source_idx",
        "connections",
        ["project_id", "source_kind", "source_id", "kind"],
        postgresql_where=sa.text("enabled"),
    )
    op.create_index(
        "connections_target_idx", "connections", ["project_id", "target_kind", "target_id"]
    )

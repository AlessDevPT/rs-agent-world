"""SQLAlchemy models for the tenancy tables.

These describe the schema; the migrations in `migrations/versions/` *are* the
schema. Keeping both is the usual trade - hand-written migrations can express
partial indexes, `citext` and check constraints that autogenerate cannot, and
models give the repositories typed access - and the usual risk is that the two
drift apart.

That risk is handled rather than accepted: `tests/integration/test_schema.py`
asks Alembic to diff this metadata against a freshly migrated database and
fails on any difference. A model changed without a migration, or a migration
written without the model, does not survive CI.

Enum columns bind to the native PostgreSQL types with `create_type=False`:
migration `0002_enums` creates them, so a model must never try to create one
as a side effect of creating a table.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentworld.domain import enums


def pg_enum(python_enum: type[StrEnum], name: str) -> ENUM:
    """Bind to the native type created by `0002_enums`.

    `values_callable` is not optional decoration: without it SQLAlchemy sends
    the Python member *names* (`OWNER`) where PostgreSQL expects the labels
    (`owner`), and every insert fails with an invalid-input error.
    """
    return ENUM(
        python_enum,
        name=name,
        create_type=False,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


class Base(DeclarativeBase):
    #: `str` maps to TEXT, not SQLAlchemy's default VARCHAR. In PostgreSQL the
    #: two are the same storage, but an unlengthened VARCHAR in the model
    #: against a TEXT column in the migration is a difference the drift check
    #: reports on every run - noise that would soon be ignored, and with it
    #: the real differences.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: sa.Text(),
        dict[str, Any]: JSONB,
        dt.datetime: sa.TIMESTAMP(timezone=True),
    }


def _uuid_pk() -> Mapped[UUID]:
    return mapped_column(sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(server_default=sa.text("now()"))


class User(Base):
    """Mirror of the Supabase auth user. `id` is the JWT `sub`, verbatim.

    No `server_default` on `id`: a row here only ever exists because a token
    was verified, and inventing an id for one would create a user nobody can
    authenticate as.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    display_name: Mapped[str | None]
    avatar_url: Mapped[str | None]
    created_at: Mapped[dt.datetime] = _created_at()
    last_seen_at: Mapped[dt.datetime | None]


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _created_at()
    archived_at: Mapped[dt.datetime | None]


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[enums.MemberRole] = mapped_column(
        pg_enum(enums.MemberRole, "member_role"),
        nullable=False,
        server_default=sa.text("'member'::member_role"),
    )
    invited_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="SET NULL"))
    joined_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("workspace_members_user_id_idx", "user_id"),
        # Ownership is singular by construction, not by convention. The
        # service layer transfers ownership in one transaction - demote the
        # old owner, promote the new - and this index is what guarantees no
        # code path can ever leave two, or leave a workspace with none that
        # it can delete.
        sa.Index(
            "workspace_single_owner",
            "workspace_id",
            unique=True,
            postgresql_where=sa.text("role = 'owner'"),
        ),
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str | None]
    settings: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _created_at()
    archived_at: Mapped[dt.datetime | None]

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "slug", name="projects_workspace_slug_key"),
    )


class ApiKey(Base):
    """A minted key. `secret_hash` is the only copy of the secret that exists.

    It is read in exactly one place - `ApiKeyRepository.verification_row` -
    and handed straight to the hasher. No entity, schema or log line carries
    it, because none of them has a field for it.
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL means workspace-wide. Ingest keys may not be: see the check below.
    project_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(nullable=False)
    prefix: Mapped[str] = mapped_column(nullable=False, unique=True)
    secret_hash: Mapped[str] = mapped_column(nullable=False)
    scope: Mapped[enums.ApiKeyScope] = mapped_column(
        pg_enum(enums.ApiKeyScope, "api_key_scope"),
        nullable=False,
        server_default=sa.text("'ingest'::api_key_scope"),
    )
    #: NULL means any agent_key in the pinned project.
    agent_key_allowlist: Mapped[list[str] | None] = mapped_column(sa.ARRAY(sa.Text))
    rate_limit_rps: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("50"))
    created_by: Mapped[UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[dt.datetime] = _created_at()
    last_used_at: Mapped[dt.datetime | None]
    expires_at: Mapped[dt.datetime | None]
    revoked_at: Mapped[dt.datetime | None]

    __table_args__ = (
        # An ingest key with no project would be able to emit events into any
        # project in the workspace, which is exactly the blast radius the
        # ingest scope exists to avoid.
        sa.CheckConstraint(
            "scope <> 'ingest' OR project_id IS NOT NULL",
            name="ingest_keys_are_project_scoped",
        ),
        sa.Index(
            "api_keys_workspace_active_idx",
            "workspace_id",
            postgresql_where=sa.text("revoked_at IS NULL"),
        ),
        sa.Index("api_keys_project_idx", "project_id"),
    )


class AuditLogEntry(Base):
    """Append-only record of every mutating control-plane call.

    `bigserial` rather than a uuid: this table is written far more often than
    it is read, always in insertion order, and a monotonic key keeps the index
    appending to one page instead of writing all over it.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: 'user' | 'api_key' | 'system'. Text rather than an enum: the audit log
    #: must be able to record an actor kind that a future version introduced
    #: without a migration standing between an event and its record.
    actor_type: Mapped[str] = mapped_column(nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(sa.Uuid)
    action: Mapped[str] = mapped_column(nullable=False)
    resource_type: Mapped[str] = mapped_column(nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(sa.Uuid)
    request_id: Mapped[str | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None]
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("audit_log_workspace_created_idx", "workspace_id", sa.text("created_at DESC")),
    )


# ----------------------------------------------------- Phase 2: definition ---


class Credential(Base):
    """Recoverable integration material, under envelope encryption.

    A different mechanism from `ApiKey`, not a variant of it: an API key must
    be verifiable and unreadable even by this server, and an integration
    credential must be usable by it. The GCM additional authenticated data
    binds the ciphertext to `workspace_id || id || aad_version`, so a row
    copied into another workspace cannot be decrypted in its new home.

    Only `infra/secrets/` reads the three binary columns. The domain entity
    has no field for them, so there is nowhere for a response model, a log
    line or a `repr` to pick them up.
    """

    __tablename__ = "credentials"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL means workspace-wide, the common case for a provider key.
    project_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(nullable=False)
    kind: Mapped[enums.CredentialKind] = mapped_column(
        pg_enum(enums.CredentialKind, "credential_kind"), nullable=False
    )
    provider_slug: Mapped[str] = mapped_column(nullable=False)

    ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    wrapped_key: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(nullable=False)
    aad_version: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("1")
    )

    display_hint: Mapped[str | None]
    scopes: Mapped[list[str] | None] = mapped_column(sa.ARRAY(sa.Text))
    expires_at: Mapped[dt.datetime | None]
    refresh_after: Mapped[dt.datetime | None]

    created_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    last_used_at: Mapped[dt.datetime | None]
    revoked_at: Mapped[dt.datetime | None]
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Deleting zeroes the key material and keeps the row: `audit_log`
    #: references credential ids, and a dangling reference in an audit trail
    #: is worse than a tombstone.
    deleted_at: Mapped[dt.datetime | None]

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "provider_slug", "name", name="credentials_name_key"),
        sa.Index(
            "credentials_refresh_due_idx",
            "refresh_after",
            postgresql_where=sa.text(
                "revoked_at IS NULL AND deleted_at IS NULL AND refresh_after IS NOT NULL"
            ),
        ),
    )


class ModelProfile(Base):
    """A named model choice, so a beginner never meets a provider setting.

    `provider_slug`, `model_id` and `parameters` are opaque to the domain
    layer - the provider plugin owns their meaning. That is how `domain/`
    keeps importing no provider SDK while the product still calls models.
    """

    __tablename__ = "model_profiles"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(nullable=False)
    provider_slug: Mapped[str] = mapped_column(nullable=False)
    model_id: Mapped[str] = mapped_column(nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    #: RESTRICT: a credential in use cannot be deleted out from under the
    #: agents depending on it. Revoke it instead, which fails the next
    #: dispatch loudly and with a reason.
    credential_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("credentials.id", ondelete="RESTRICT")
    )
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "name", name="model_profiles_name_key"),
        # Exactly one default per workspace, as a database guarantee - the
        # same trick as `workspace_single_owner`.
        sa.Index(
            "one_default_model_profile",
            "workspace_id",
            unique=True,
            postgresql_where=sa.text("is_default"),
        ),
    )


class Agent(Base):
    """An agent: a definition half and a runtime half in one table.

    The split is enforced by which layer may write each column. Everything
    down to `runtime` is user-editable through the control plane; `status` and
    below are written only by reducers, folding events. `PATCH /v1/agents/{id}`
    accepts the first group and never the second.
    """

    __tablename__ = "agents"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: Caller-chosen, so a restarted or scaled-out worker keeps its identity
    #: without persisting anything.
    agent_key: Mapped[str] = mapped_column(CITEXT, nullable=False)
    interaction_mode: Mapped[enums.AgentInteractionMode] = mapped_column(
        pg_enum(enums.AgentInteractionMode, "agent_interaction_mode"), nullable=False
    )

    # ---- definition ----
    display_name: Mapped[str] = mapped_column(nullable=False)
    role: Mapped[str | None]
    description: Mapped[str | None]
    instructions: Mapped[str | None]
    #: Only meaningful for `managed`; see the check constraint below.
    model_profile_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("model_profiles.id", ondelete="RESTRICT")
    )
    model_overrides: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    kind: Mapped[str | None]
    labels: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    # ---- declared interface ----
    capabilities: Mapped[list[str]] = mapped_column(
        sa.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")
    )
    dispatch: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    #: Descriptive only. The core never branches on it, which is what lets a
    #: dispatchable agent report a provider this system does not support.
    runtime: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    # ---- runtime state: reducers only ----
    status: Mapped[enums.AgentStatus] = mapped_column(
        pg_enum(enums.AgentStatus, "agent_status"),
        nullable=False,
        server_default=sa.text("'provisioning'::agent_status"),
    )
    status_reason: Mapped[str | None]
    #: The foreign key to `tasks` is added by the migration that creates it;
    #: the reference is circular.
    current_task_id: Mapped[UUID | None] = mapped_column(sa.Uuid)
    last_heartbeat_at: Mapped[dt.datetime | None]
    last_event_at: Mapped[dt.datetime | None]
    heartbeat_ttl_seconds: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("60"))

    created_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    registered_at: Mapped[dt.datetime] = _created_at()
    archived_at: Mapped[dt.datetime | None]

    __table_args__ = (
        # Only a managed agent has a model, because it is the only mode where
        # AgentWorld calls one. This is what makes "runtime and model provider
        # are separate axes" structural rather than documented.
        sa.CheckConstraint(
            "interaction_mode = 'managed' OR model_profile_id IS NULL",
            name="model_profile_only_when_managed",
        ),
        sa.UniqueConstraint("project_id", "agent_key", name="agents_project_key_key"),
        sa.Index(
            "agents_project_status_idx",
            "project_id",
            "status",
            postgresql_where=sa.text("archived_at IS NULL"),
        ),
        sa.Index("agents_labels_idx", "labels", postgresql_using="gin"),
        # Drives the offline sweep: cheap to scan, no full table pass.
        sa.Index(
            "agents_heartbeat_watch",
            "last_heartbeat_at",
            postgresql_where=sa.text(
                "archived_at IS NULL AND status NOT IN ('offline', 'maintenance', 'paused')"
            ),
        ),
    )


class Connection(Base):
    """A standing grant or route between components.

    Not a workflow edge. An edge is control flow inside one workflow version;
    a connection is a capability that outlives any single workflow. The
    orchestrator reads `grant_tool` rows before dispatching a tool call, so a
    revoked grant takes effect on the next call rather than the next restart.

    `source_id` and `target_id` carry no foreign key on purpose: the set of
    connectable kinds grows with the product, and five pairs of nullable
    columns would not. The service layer validates both ends exist in the
    same project.
    """

    __tablename__ = "connections"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[enums.ConnectionKind] = mapped_column(
        pg_enum(enums.ConnectionKind, "connection_kind"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(nullable=False)
    source_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    target_kind: Mapped[str] = mapped_column(nullable=False)
    target_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    #: A grant may itself be gated. The gate lives with the grant, not the UI.
    requires_approval: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("true"))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.UniqueConstraint(
            "project_id",
            "kind",
            "source_kind",
            "source_id",
            "target_kind",
            "target_id",
            name="connections_edge_key",
        ),
        # The lookup the orchestrator makes before every tool_call dispatch.
        sa.Index(
            "connections_source_idx",
            "project_id",
            "source_kind",
            "source_id",
            "kind",
            postgresql_where=sa.text("enabled"),
        ),
        sa.Index("connections_target_idx", "project_id", "target_kind", "target_id"),
    )


#: Tables protected by row-level security in migration `0004_rls`.
#: `test_rls.py` reads this and asserts the database agrees, so a new
#: tenant-scoped table cannot be added without a decision about it being
#: recorded here.
#:
#: The credential tables - `users`, `workspace_members`, `workspaces`,
#: `api_keys` - are deliberately absent: each is read before the request
#: knows which tenant it belongs to, so a policy on them would need the
#: answer in order to produce it. Migration `0004_rls` sets out the rule and
#: what guards them instead.
RLS_PROTECTED_TABLES: tuple[str, ...] = (
    # 0004
    "projects",
    "audit_log",
    # 0007 - each migration policies its own tables as it creates them
    "credentials",
    "model_profiles",
    "agents",
    "connections",
)

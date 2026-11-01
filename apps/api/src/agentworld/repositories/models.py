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
    #: Declared by migration 0010, once `tasks` exists; the reference is
    #: circular, so it could not be stated here before then.
    current_task_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey(
            "tasks.id", ondelete="SET NULL", use_alter=True, name="agents_current_task_fkey"
        )
    )
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


# ------------------------------------------------------ Phase 2: workflows ---

#: What a step may do when it fails. Text with a check constraint rather than
#: a native enum: adding a fourth value should not need `ALTER TYPE` and the
#: transaction dance that comes with it (migration 0006, and `env.py`).
ON_FAILURE: tuple[str, ...] = ("fail", "continue", "route")


class Workflow(Base):
    """A workflow definition. Executions reference a *version*, never this.

    `current_version_id` is the published version that triggers and manual
    runs use, and its foreign key targets `(workflow_id, id)` so it cannot
    name a version belonging to a different workflow.
    """

    __tablename__ = "workflows"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str | None]
    #: NULL until the first publish, which is why a workflow starts disabled.
    current_version_id: Mapped[UUID | None] = mapped_column(sa.Uuid)
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    archived_at: Mapped[dt.datetime | None]

    __table_args__ = (
        sa.UniqueConstraint("project_id", "slug", name="workflows_project_slug_key"),
        # Composite, so the named version must belong to this workflow.
        #
        # `use_alter` is not cosmetic. `workflows` and `workflow_versions`
        # reference each other, and without it SQLAlchemy cannot sort the two
        # and warns that "foreign key constraints involving these tables will
        # not be considered" - which would mean the schema drift test
        # silently stopped comparing foreign keys on both. A blind check that
        # reports success is worse than no check. `use_alter` tells
        # SQLAlchemy this constraint is emitted after both tables exist,
        # which is exactly what migration 0008 does.
        sa.ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["workflow_versions.workflow_id", "workflow_versions.id"],
            name="workflows_current_version_fkey",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )


class WorkflowVersion(Base):
    """A frozen graph. An execution is bound to one of these for its whole life.

    Editing a definition while it runs must not change what the running copy
    means - otherwise a step can complete against a graph that no longer
    contains it, and replay has nothing stable to fold against.
    """

    __tablename__ = "workflow_versions"

    id: Mapped[UUID] = _uuid_pk()
    workflow_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    #: The authority an execution is bound to. `WorkflowStep` and
    #: `WorkflowEdge` are the queryable projection of this column.
    graph: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    #: NULL means a draft. Only a published version may be executed.
    published_at: Mapped[dt.datetime | None]
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.CheckConstraint("version > 0", name="version_is_positive"),
        sa.UniqueConstraint("workflow_id", "version", name="workflow_versions_number_key"),
        # Redundant alone; the target of `workflows.current_version_id`.
        sa.UniqueConstraint("workflow_id", "id", name="workflow_versions_workflow_id_id_key"),
    )


class WorkflowStep(Base):
    """One node of a frozen graph.

    `step_key` is what an input mapping refers to, so renaming `name` does not
    break a reference. The two check constraints mean an unrunnable step
    cannot be stored at all, which leaves `validate` to report the richer
    problems rather than this one.
    """

    __tablename__ = "workflow_steps"

    id: Mapped[UUID] = _uuid_pk()
    workflow_version_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    step_key: Mapped[str] = mapped_column(CITEXT, nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    kind: Mapped[enums.StepKind] = mapped_column(
        pg_enum(enums.StepKind, "step_kind"), nullable=False
    )
    #: RESTRICT: an agent assigned a step in a published version cannot be
    #: deleted out from under it. Archive it instead.
    agent_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("agents.id", ondelete="RESTRICT"))
    #: The foreign key to `tools` arrives with that table in Phase 6.
    tool_id: Mapped[UUID | None] = mapped_column(sa.Uuid)
    config: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    #: Declarative on purpose: an expression language the orchestrator
    #: evaluates, not code it executes.
    input_mapping: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    max_attempts: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("1"))
    timeout_seconds: Mapped[int | None]
    on_failure: Mapped[str] = mapped_column(nullable=False, server_default=sa.text("'fail'"))

    __table_args__ = (
        sa.CheckConstraint(
            "kind <> 'agent_task' OR agent_id IS NOT NULL", name="agent_task_needs_agent"
        ),
        sa.CheckConstraint(
            "kind <> 'tool_call' OR tool_id IS NOT NULL", name="tool_call_needs_tool"
        ),
        sa.CheckConstraint(
            "on_failure IN ('" + "', '".join(ON_FAILURE) + "')", name="on_failure_is_known"
        ),
        sa.CheckConstraint("max_attempts >= 1", name="max_attempts_is_positive"),
        sa.CheckConstraint(
            "timeout_seconds IS NULL OR timeout_seconds > 0", name="timeout_is_positive"
        ),
        sa.UniqueConstraint("workflow_version_id", "step_key", name="workflow_steps_key_key"),
        # Redundant alone; the target of both edge endpoints, which is what
        # stops an edge wiring steps from two different versions.
        sa.UniqueConstraint("workflow_version_id", "id", name="workflow_steps_version_id_id_key"),
        sa.Index("workflow_steps_agent_idx", "agent_id"),
    )


class WorkflowEdge(Base):
    """One edge of a frozen graph: control flow inside this version only.

    Not a `Connection`. An edge is sequencing within one workflow version; a
    connection is a standing grant between components that outlives any
    workflow. The island draws both as paths, which is where the two get
    conflated.
    """

    __tablename__ = "workflow_edges"

    id: Mapped[UUID] = _uuid_pk()
    workflow_version_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL means an entry edge: this step starts when the execution does.
    from_step_id: Mapped[UUID | None] = mapped_column(sa.Uuid)
    to_step_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    #: NULL means unconditional.
    condition: Mapped[dict[str, Any] | None]
    #: "approved" / "rejected" - drawn on the island and the DAG canvas.
    label: Mapped[str | None]

    __table_args__ = (
        # A self-loop is a cycle, and loops are not a v1 feature.
        sa.CheckConstraint(
            "from_step_id IS NULL OR from_step_id <> to_step_id", name="no_self_edge"
        ),
        # Both endpoints must live in the same version as the edge. MATCH
        # SIMPLE, so a NULL `from_step_id` - an entry edge - passes.
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "from_step_id"],
            ["workflow_steps.workflow_version_id", "workflow_steps.id"],
            name="workflow_edges_from_step_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "to_step_id"],
            ["workflow_steps.workflow_version_id", "workflow_steps.id"],
            name="workflow_edges_to_step_fkey",
            ondelete="CASCADE",
        ),
        # NULLS NOT DISTINCT: an entry edge has a NULL `from_step_id` and
        # usually a NULL `label`, so under the default semantics this key
        # would permit the same entry edge any number of times.
        sa.UniqueConstraint(
            "workflow_version_id",
            "from_step_id",
            "to_step_id",
            "label",
            name="workflow_edges_edge_key",
            postgresql_nulls_not_distinct=True,
        ),
        sa.Index("workflow_edges_from_idx", "workflow_version_id", "from_step_id"),
        sa.Index("workflow_edges_to_idx", "workflow_version_id", "to_step_id"),
    )


# ------------------------------------------------------ Phase 2: execution ---

#: Statuses in which an execution may still progress. The no-stuck-executions
#: invariant reads this: a live execution with no live dispatch, no pending
#: approval and no terminal step is a bug.
LIVE_EXECUTION_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "waiting_approval",
    "waiting_input",
    "paused",
)


class WorkflowExecution(Base):
    """One run of one frozen workflow version.

    Bound to a `workflow_version_id` for its whole life, so editing the
    definition cannot change what a running copy means.

    The initiator is immutable and total: written once at creation, never
    recomputed, and never resolved through a mutable row. Authority for a
    `system` initiator comes from the published version rather than from a
    user, so the run borrows no account's permissions and nothing goes stale
    after a demotion or a departure.
    """

    __tablename__ = "workflow_executions"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    #: RESTRICT: the frozen version cannot be deleted while an execution
    #: references it, or the record would describe a graph nobody can read.
    workflow_version_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workflow_versions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[enums.ExecutionStatus] = mapped_column(
        pg_enum(enums.ExecutionStatus, "execution_status"),
        nullable=False,
        server_default=sa.text("'pending'::execution_status"),
    )
    #: The FK arrives with `triggers` in Phase 7.
    trigger_id: Mapped[UUID | None] = mapped_column(sa.Uuid)

    # ---- the immutable initiator ----
    initiator_kind: Mapped[enums.InitiatorKind] = mapped_column(
        pg_enum(enums.InitiatorKind, "initiator_kind"), nullable=False
    )
    #: RESTRICT: a user who has initiated an execution cannot be hard deleted
    #: out from under the record.
    initiator_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="RESTRICT")
    )
    initiator_trigger_id: Mapped[UUID | None] = mapped_column(sa.Uuid)
    #: Plain text, written once, so an audit two years later answers "what
    #: started this" without a join that may have gone.
    initiator_label: Mapped[str] = mapped_column(nullable=False)

    #: A trigger firing twice yields one execution.
    idempotency_key: Mapped[str | None] = mapped_column(CITEXT)
    input: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    output: Mapped[dict[str, Any] | None]
    error: Mapped[dict[str, Any] | None]
    started_at: Mapped[dt.datetime | None]
    finished_at: Mapped[dt.datetime | None]
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        # Exactly one of the two ids, matching the kind. A `system` initiator
        # names no user, which is the whole point.
        sa.CheckConstraint(
            "(initiator_kind = 'user' AND initiator_user_id IS NOT NULL "
            "AND initiator_trigger_id IS NULL) "
            "OR (initiator_kind = 'system' AND initiator_user_id IS NULL)",
            name="initiator_user_xor_system",
        ),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="workflow_executions_idempotency_key"
        ),
        # Redundant alone; the target of `execution_steps`, which is what
        # stops a step being recorded against a version this execution is not
        # bound to.
        sa.UniqueConstraint("id", "workflow_version_id", name="workflow_executions_id_version_key"),
        sa.Index(
            "executions_live_idx",
            "project_id",
            "status",
            "created_at",
            postgresql_where=sa.text("status IN ('" + "', '".join(LIVE_EXECUTION_STATUSES) + "')"),
        ),
        sa.Index("executions_initiator_idx", "project_id", "initiator_kind", "created_at"),
        sa.Index("executions_workflow_idx", "workflow_id", "created_at"),
    )


class ExecutionStep(Base):
    """One attempt at one step of one execution.

    The two composite foreign keys together say "this step belongs to the
    version its execution is bound to" - the same bug class as 0008's
    cross-version edge, one level down. Without them a step record could be
    attributed to a step absent from the graph being executed.
    """

    __tablename__ = "execution_steps"

    id: Mapped[UUID] = _uuid_pk()
    execution_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised so the composite foreign keys below can be expressed.
    workflow_version_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    workflow_step_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    #: Denormalised, so a timeline needs no join.
    step_key: Mapped[str] = mapped_column(CITEXT, nullable=False)
    status: Mapped[enums.StepStatus] = mapped_column(
        pg_enum(enums.StepStatus, "step_status"),
        nullable=False,
        server_default=sa.text("'pending'::step_status"),
    )
    attempt: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("0"))
    agent_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("agents.id", ondelete="SET NULL"))
    #: Declared by migration 0010, once `tasks` exists.
    task_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey(
            "tasks.id", ondelete="SET NULL", use_alter=True, name="execution_steps_task_fkey"
        )
    )
    input: Mapped[dict[str, Any] | None]
    output: Mapped[dict[str, Any] | None]
    error: Mapped[dict[str, Any] | None]
    started_at: Mapped[dt.datetime | None]
    finished_at: Mapped[dt.datetime | None]

    __table_args__ = (
        sa.CheckConstraint("attempt >= 0", name="step_attempt_is_not_negative"),
        # This row's version must be the one its execution is bound to.
        sa.ForeignKeyConstraint(
            ["execution_id", "workflow_version_id"],
            [
                "workflow_executions.id",
                "workflow_executions.workflow_version_id",
            ],
            name="execution_steps_execution_version_fkey",
            ondelete="CASCADE",
        ),
        # And the step named must live in that version.
        sa.ForeignKeyConstraint(
            ["workflow_version_id", "workflow_step_id"],
            ["workflow_steps.workflow_version_id", "workflow_steps.id"],
            name="execution_steps_step_fkey",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "execution_id", "workflow_step_id", "attempt", name="execution_steps_attempt_key"
        ),
        sa.Index("execution_steps_execution_idx", "execution_id"),
        sa.Index(
            "execution_steps_live_idx",
            "project_id",
            "status",
            postgresql_where=sa.text("status IN ('pending', 'ready', 'dispatched', 'running')"),
        ),
    )


class Dispatch(Base):
    """The transactional outbox: work to be performed, exactly once asked for.

    **Not part of the event log and never replayed.** One transaction appends
    an event, folds the state change, and inserts a row here; a worker then
    performs the side effect. That separation is what lets the log be replayed
    to rebuild state without re-sending the emails.

    Operational state rather than history: rows are updated as they progress
    and pruned after completion. The history is in `events`.
    """

    __tablename__ = "dispatches"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    execution_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("workflow_executions.id", ondelete="CASCADE")
    )
    execution_step_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("execution_steps.id", ondelete="CASCADE")
    )
    kind: Mapped[enums.DispatchKind] = mapped_column(
        pg_enum(enums.DispatchKind, "dispatch_kind"), nullable=False
    )
    status: Mapped[enums.DispatchStatus] = mapped_column(
        pg_enum(enums.DispatchStatus, "dispatch_status"),
        nullable=False,
        server_default=sa.text("'pending'::dispatch_status"),
    )
    #: Copied at dispatch time, not looked up at retry time: the retry policy
    #: must not change because somebody edited a tool while work was in
    #: flight.
    side_effect_class: Mapped[enums.SideEffect] = mapped_column(
        pg_enum(enums.SideEffect, "side_effect"), nullable=False
    )
    #: The whole instruction, resolved: credential *references*, never
    #: credentials.
    instruction: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    #: Forwarded outbound as `X-AgentWorld-Idempotency-Key`, stable across
    #: attempts of the same work. A human-initiated retry gets a new one,
    #: because that is new work rather than a repeat.
    idempotency_token: Mapped[UUID] = mapped_column(
        sa.Uuid, nullable=False, server_default=sa.text("gen_random_uuid()")
    )
    attempt: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("3"))
    #: A lease, not a lock: a worker that dies holds nothing.
    leased_by: Mapped[str | None]
    leased_until: Mapped[dt.datetime | None]
    #: Set before the outbound call and cleared after it returns. A lease that
    #: expires with this set means the side effect was in flight when the
    #: worker died and nobody knows whether it landed - which is what sends a
    #: consequential dispatch to `needs_review` rather than a retry.
    side_effect_started_at: Mapped[dt.datetime | None]
    run_after: Mapped[dt.datetime] = _created_at()
    last_error: Mapped[str | None]
    last_error_code: Mapped[str | None]
    created_at: Mapped[dt.datetime] = _created_at()
    finished_at: Mapped[dt.datetime | None]

    __table_args__ = (
        sa.CheckConstraint("attempt >= 0", name="dispatch_attempt_is_not_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="dispatch_max_attempts_is_positive"),
        # A row marked leased with no lease could never be reclaimed by the
        # reaper, so it would be stuck forever. Make it unrepresentable.
        sa.CheckConstraint(
            "status <> 'leased' OR (leased_by IS NOT NULL AND leased_until IS NOT NULL)",
            name="leased_dispatch_has_a_lease",
        ),
        # The idempotency guarantee: one dispatch per step attempt. Two
        # concurrent `advance` calls hit this rather than sending twice.
        sa.UniqueConstraint(
            "execution_step_id", "kind", "attempt", name="dispatches_step_attempt_key"
        ),
        sa.Index(
            "dispatches_claimable_idx",
            "run_after",
            "id",
            postgresql_where=sa.text("status = 'pending'"),
        ),
        sa.Index(
            "dispatches_expired_lease_idx",
            "leased_until",
            postgresql_where=sa.text("status = 'leased'"),
        ),
        # What the attention queue reads: the two states that need a human.
        sa.Index(
            "dispatches_attention_idx",
            "project_id",
            "status",
            postgresql_where=sa.text("status IN ('dead', 'needs_review')"),
        ),
    )


# ----------------------------------------------------- Phase 2: agent work ---

#: Computed by PostgreSQL, so no writer can disagree with it and no reader
#: has to recompute it.
DURATION_MS = "(EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000)::int"


class Task(Base):
    """The unit of work handed to an agent.

    No longer the top-level concept it was in the previous design: an
    `agent_task` step materialises one, and `execution_step_id` records where
    it came from. A standalone task - a plain work queue with no workflow -
    has NULL there. That keeps the whole claim and heartbeat pipeline,
    including the `FOR UPDATE SKIP LOCKED` claim, and gives it a place in the
    new product rather than a competing one.
    """

    __tablename__ = "tasks"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: The caller's own id, so an integrator can address a task by the
    #: reference their system already uses.
    task_ref: Mapped[str | None] = mapped_column(CITEXT)
    #: NULL for a standalone task; set when a workflow step materialised it.
    execution_step_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("execution_steps.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str | None]
    status: Mapped[enums.TaskStatus] = mapped_column(
        pg_enum(enums.TaskStatus, "task_status"),
        nullable=False,
        server_default=sa.text("'pending'::task_status"),
    )
    priority: Mapped[enums.TaskPriority] = mapped_column(
        pg_enum(enums.TaskPriority, "task_priority"),
        nullable=False,
        server_default=sa.text("'normal'::task_priority"),
    )
    assigned_agent_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("agents.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_agent_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("agents.id", ondelete="SET NULL")
    )
    #: Arrives from an agent over the wire and is rendered directly as a
    #: progress bar, so it is checked rather than trusted.
    progress_percent: Mapped[int | None] = mapped_column(sa.SmallInteger)
    progress_message: Mapped[str | None]
    input: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    result: Mapped[dict[str, Any] | None]
    error: Mapped[dict[str, Any] | None]
    attempt: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("1"))
    labels: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    queued_at: Mapped[dt.datetime | None]
    started_at: Mapped[dt.datetime | None]
    finished_at: Mapped[dt.datetime | None]
    deadline_at: Mapped[dt.datetime | None]
    updated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.CheckConstraint(
            "progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100",
            name="progress_is_a_percentage",
        ),
        sa.CheckConstraint("attempt >= 0", name="task_attempt_is_not_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="task_max_attempts_is_positive"),
        sa.UniqueConstraint("project_id", "task_ref", name="tasks_project_ref_key"),
        # Redundant alone; the target of `task_dependencies`, which is what
        # stops a dependency crossing projects.
        sa.UniqueConstraint("project_id", "id", name="tasks_project_id_id_key"),
        # The claim query's index: pending work in priority then arrival order.
        sa.Index(
            "tasks_claimable_idx",
            "project_id",
            "status",
            sa.text("priority DESC"),
            "created_at",
        ),
        sa.Index("tasks_assigned_idx", "assigned_agent_id", "status"),
        sa.Index("tasks_labels_idx", "labels", postgresql_using="gin"),
        sa.Index("tasks_execution_step_idx", "execution_step_id"),
    )


class TaskDependency(Base):
    """ "This task waits on that one."

    Both ends are reached through `(project_id, id)`, so a dependency cannot
    cross projects - which would be nonsense and a cross-tenant information
    channel.
    """

    __tablename__ = "task_dependencies"

    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    task_id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    depends_on_id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)

    __table_args__ = (
        sa.CheckConstraint("task_id <> depends_on_id", name="no_self_dependency"),
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
            name="task_dependencies_task_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "depends_on_id"],
            ["tasks.project_id", "tasks.id"],
            name="task_dependencies_depends_on_fkey",
            ondelete="CASCADE",
        ),
        sa.Index("task_dependencies_depends_on_idx", "depends_on_id"),
    )


class Run(Base):
    """One attempt at a task, carrying the agent's own trace."""

    __tablename__ = "runs"

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_ref: Mapped[str | None] = mapped_column(CITEXT)
    agent_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("tasks.id", ondelete="SET NULL"))
    #: NULL for a run outside any workflow - an agent doing its own thing.
    execution_step_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("execution_steps.id", ondelete="SET NULL")
    )
    status: Mapped[enums.RunStatus] = mapped_column(
        pg_enum(enums.RunStatus, "run_status"),
        nullable=False,
        server_default=sa.text("'running'::run_status"),
    )
    step_count: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("0"))
    tool_call_count: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("0"))
    error: Mapped[dict[str, Any] | None]
    started_at: Mapped[dt.datetime] = _created_at()
    finished_at: Mapped[dt.datetime | None]
    #: Computed by PostgreSQL, so no writer can disagree with it.
    duration_ms: Mapped[int | None] = mapped_column(
        sa.Integer, sa.Computed(DURATION_MS, persisted=True)
    )

    __table_args__ = (
        sa.CheckConstraint("step_count >= 0", name="step_count_is_not_negative"),
        sa.CheckConstraint("tool_call_count >= 0", name="tool_call_count_is_not_negative"),
        sa.UniqueConstraint("project_id", "run_ref", name="runs_project_ref_key"),
        sa.Index("runs_task_idx", "task_id", sa.text("started_at DESC")),
        sa.Index("runs_agent_idx", "agent_id", sa.text("started_at DESC")),
    )


class RunStep(Base):
    """One step of a run: an LLM turn, a tool call, a decision.

    `bigserial` rather than a uuid: written far more often than read, always
    in order, and a monotonic key keeps the index appending to one page.
    """

    __tablename__ = "run_steps"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    #: Free text. The core never branches on it.
    kind: Mapped[str | None]
    status: Mapped[enums.RunStatus] = mapped_column(
        pg_enum(enums.RunStatus, "run_status"),
        nullable=False,
        server_default=sa.text("'running'::run_status"),
    )
    input: Mapped[dict[str, Any] | None]
    output: Mapped[dict[str, Any] | None]
    error: Mapped[dict[str, Any] | None]
    started_at: Mapped[dt.datetime] = _created_at()
    finished_at: Mapped[dt.datetime | None]

    __table_args__ = (
        sa.CheckConstraint("seq >= 0", name="run_step_seq_is_not_negative"),
        sa.UniqueConstraint("run_id", "seq", name="run_steps_seq_key"),
    )


class AgentStatusHistory(Base):
    """Every status transition an agent has made.

    Low-volume and the most useful forensic table, so it outlives the
    partitioned `events` it refers to - which is why `event_id` is a plain
    uuid and not a foreign key. A dangling reference here is correct; a
    cascade would delete the forensic record along with the partition.
    """

    __tablename__ = "agent_status_history"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL for the first transition, out of nothing.
    from_status: Mapped[enums.AgentStatus | None] = mapped_column(
        pg_enum(enums.AgentStatus, "agent_status")
    )
    to_status: Mapped[enums.AgentStatus] = mapped_column(
        pg_enum(enums.AgentStatus, "agent_status"), nullable=False
    )
    reason: Mapped[str | None]
    event_id: Mapped[UUID | None] = mapped_column(sa.Uuid)
    changed_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("agent_status_history_agent_idx", "agent_id", sa.text("changed_at DESC")),
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
    # 0008
    "workflows",
    "workflow_versions",
    "workflow_steps",
    "workflow_edges",
    # 0009
    "workflow_executions",
    "execution_steps",
    "dispatches",
    # 0010
    "tasks",
    "task_dependencies",
    "runs",
    "run_steps",
    "agent_status_history",
)

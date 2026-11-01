"""Domain entities.

Plain frozen dataclasses: no ORM, no framework, no serialisation. The API
layer has its own pydantic schemas and the repository layer its own SQLAlchemy
models, and both convert at their boundary. That is three definitions of
"project" instead of one, which is the price of being able to change the
database or the wire format without the other noticing.

One property is load-bearing: `ApiKey` has no field for the secret or its
hash. A key's hash is read inside the repository layer, handed straight to the
verifier, and never placed in an entity - so no accidental log line, response
model or `repr` can carry it, because there is nowhere for it to live.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from agentworld.domain.enums import (
    AgentInteractionMode,
    AgentStatus,
    ApiKeyScope,
    ConnectionKind,
    MemberRole,
)


@dataclass(frozen=True, slots=True)
class User:
    """A mirror of the Supabase auth user. Never holds credentials."""

    id: UUID
    email: str
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Workspace:
    id: UUID
    slug: str
    name: str
    created_by: UUID
    settings: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Membership:
    workspace_id: UUID
    user_id: UUID
    role: MemberRole
    invited_by: UUID | None = None
    joined_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MembershipView:
    """A membership joined with the user it belongs to, for member listings."""

    membership: Membership
    user: User


@dataclass(frozen=True, slots=True)
class WorkspaceMembership:
    """A workspace joined with the caller's own role in it, for `GET /v1/me`."""

    workspace: Workspace
    role: MemberRole


@dataclass(frozen=True, slots=True)
class Project:
    id: UUID
    workspace_id: UUID
    slug: str
    name: str
    description: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A key as anyone other than its creator ever sees it.

    `prefix` is the public handle; there is deliberately no secret field. The
    plaintext exists once, in the response to `POST /v1/workspaces/{ws}/api-keys`,
    and is returned by the service as a separate value rather than on this
    entity.
    """

    id: UUID
    workspace_id: UUID
    project_id: UUID | None
    name: str
    prefix: str
    scope: ApiKeyScope
    agent_key_allowlist: Sequence[str] | None
    rate_limit_rps: int
    created_by: UUID
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        """Whether the key would be accepted, ignoring the secret itself.

        `expires_at` is compared by the verifier against the database clock,
        not this process's, so this property reports revocation only. It
        exists for listings, not for authentication decisions.
        """
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """A named model configuration: provider, model, parameters, credential.

    Workspace-scoped rather than project-scoped, because one API key and one
    choice of model is normally a company decision rather than a per-project
    one. No field holds credential material - `credential_id` names a row in
    `credentials`, whose secret nothing in this layer can read.
    """

    id: UUID
    workspace_id: UUID
    name: str
    provider_slug: str
    model_id: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    credential_id: UUID | None = None
    is_default: bool = False
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Agent:
    """A participant in an ecosystem.

    The fields divide into three groups, and the division is the permission
    model rather than tidiness. **Definition** - name, role, description,
    instructions, labels - is what a builder edits and a `member` may change.
    **Configuration** - the model profile, its overrides, and dispatch - is
    the narrow part that spends money and reaches outside, and takes
    `agent.configure`, which is `admin`. **Derived** - status, heartbeat,
    current task - is folded from events and no API writes it at all; there
    is deliberately no `agent.set_status` action, because a human asserting
    "this agent is working" would be asserting something only the agent can
    know.
    """

    id: UUID
    workspace_id: UUID
    project_id: UUID
    agent_key: str
    interaction_mode: AgentInteractionMode
    display_name: str

    # ---- definition ----
    role: str | None = None
    description: str | None = None
    instructions: str | None = None
    kind: str | None = None
    labels: Mapping[str, Any] = field(default_factory=dict)

    # ---- configuration ----
    model_profile_id: UUID | None = None
    model_overrides: Mapping[str, Any] = field(default_factory=dict)
    dispatch: Mapping[str, Any] = field(default_factory=dict)
    heartbeat_ttl_seconds: int = 60

    # ---- derived: read-only here, written only by folding events ----
    status: AgentStatus = AgentStatus.PROVISIONING
    status_reason: str | None = None
    capabilities: Sequence[str] = field(default_factory=tuple)
    runtime: Mapping[str, Any] = field(default_factory=dict)
    current_task_id: UUID | None = None
    last_heartbeat_at: datetime | None = None
    last_event_at: datetime | None = None
    registered_at: datetime | None = None
    archived_at: datetime | None = None

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


@dataclass(frozen=True, slots=True)
class Connection:
    """A standing grant or route between components.

    Not a workflow edge. An edge is control flow inside one workflow version;
    a connection is a capability that outlives any single workflow. The
    island draws both as paths, and keeping them apart here is what stops a
    line someone drew becoming a permission nobody granted.
    """

    id: UUID
    workspace_id: UUID
    project_id: UUID
    kind: ConnectionKind
    source_kind: str
    source_id: UUID
    target_kind: str
    target_id: UUID
    config: Mapping[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    enabled: bool = True
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Workflow:
    id: UUID
    workspace_id: UUID
    project_id: UUID
    slug: str
    name: str
    description: str | None = None
    #: NULL until the first publish, which is why a workflow starts disabled.
    current_version_id: UUID | None = None
    enabled: bool = False
    created_at: datetime | None = None
    archived_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDraft:
    """An unpublished version: the graph somebody is currently editing.

    The graph is held whole rather than normalised into steps and edges,
    because a draft changes as a whole and nothing reads its parts until it
    is published.
    """

    id: UUID
    workflow_id: UUID
    version: int
    graph: Mapping[str, Any] = field(default_factory=dict)
    published_at: datetime | None = None

    @property
    def is_published(self) -> bool:
        return self.published_at is not None

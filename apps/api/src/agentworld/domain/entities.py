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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agentworld.domain.enums import (
    AgentInteractionMode,
    AgentLiveness,
    AgentStatus,
    ApiKeyScope,
    ConnectionKind,
    CredentialKind,
    MemberRole,
    RunStatus,
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
class Credential:
    """An integration credential as anyone ever sees it: no material.

    The same property `ApiKey` relies on, for a stronger reason. There is no
    field for the ciphertext, the nonce, the wrapped key or the plaintext, so
    no response model, `repr`, log line or serialiser can carry one - not
    because each of those was careful, but because the attribute does not
    exist. The four binary columns are read in `repositories.credentials`,
    handed to `infra/secrets/` as an `EncryptedBlob`, and never travel in an
    entity.

    `display_hint` is derived from the material at write time - a few
    characters an operator can recognise a key by - and is the only thing
    about the secret that any caller is ever shown. It is not accepted from a
    request body, because a caller-supplied hint could carry the whole secret.
    """

    id: UUID
    workspace_id: UUID
    project_id: UUID | None
    name: str
    kind: CredentialKind
    provider_slug: str
    #: Which KEK wraps this row's data key. Metadata, not material: it names
    #: a key held in the environment, and is here so an operator can see
    #: which rows a rotation has still to touch.
    key_id: str
    display_hint: str | None = None
    scopes: Sequence[str] | None = None
    expires_at: datetime | None = None
    refresh_after: datetime | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    deleted_at: datetime | None = None

    @property
    def is_usable(self) -> bool:
        """Whether an outbound call may still use this credential.

        Expiry is compared against this process's clock, which is good enough
        for a listing and for refusing an obviously dead credential before
        making a request. It is not an authentication decision: the remote
        service is the authority on whether its own credential still works.
        """
        if self.revoked_at is not None or self.deleted_at is not None:
            return False
        return self.expires_at is None or self.expires_at > datetime.now(UTC)


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

    def seconds_since_heartbeat(self, now: datetime) -> int | None:
        """Whole seconds since the last heartbeat, or None if there was none.

        `now` is a parameter rather than a clock read here, for the reason
        no reducer reads one either: a value that depends on when it was
        computed cannot be tested against anything, and a caller that
        renders a page should judge every agent on it against one instant
        rather than against however long the page took to build.

        Floored, and never negative. A heartbeat posted through
        `/v1/ingest/events` carries the producer's own `occurred_at`, which
        may legitimately sit a little ahead of this server's clock - ingest
        bounds that skew rather than correcting it - and "last seen -4
        seconds ago" reads as a bug in whatever printed it.
        """
        if self.last_heartbeat_at is None:
            return None
        return max(0, int((now - self.last_heartbeat_at).total_seconds()))

    def liveness(self, now: datetime) -> AgentLiveness:
        """Whether this agent's heartbeat is current. Nothing more.

        The threshold is this agent's own `heartbeat_ttl_seconds` - the
        interval it declared at registration or an operator set on it - and
        not a project or global default. Two of those exist in the codebase
        and neither has ever had a reader; choosing between them is a
        product decision about where thresholds belong, and a method that
        compares a column to a clock is not the place to make it.

        Measured in the same floored seconds `seconds_since_heartbeat`
        returns, so the number shown and the verdict beside it can never
        disagree. The comparison is `>` rather than `>=`: an agent heard
        from exactly its TTL ago has kept its promise, and the effective
        cut-off is therefore "more than `heartbeat_ttl_seconds` whole
        seconds of silence".
        """
        if self.interaction_mode is AgentInteractionMode.MANAGED:
            return AgentLiveness.NOT_APPLICABLE
        elapsed = self.seconds_since_heartbeat(now)
        if elapsed is None:
            return AgentLiveness.NEVER_REPORTED
        if elapsed > self.heartbeat_ttl_seconds:
            return AgentLiveness.STALE
        return AgentLiveness.FRESH


@dataclass(frozen=True, slots=True)
class AgentStatusChange:
    """One transition in an agent's status, as `agent_status_history` holds it.

    Derived, like the status column itself: a row exists because an event was
    folded, and nothing in the application writes one directly. `from_status`
    is NULL for the first transition - there was no status to leave - and
    `reason` and `event_id` are NULL when the event carried neither.

    No `id`. The table's primary key is a bigint sequence, which tells a
    reader about volume and ordering they cannot use; if this ever needs a
    cursor it is `(changed_at, id)` and can be added then.
    """

    from_status: AgentStatus | None
    to_status: AgentStatus
    changed_at: datetime
    reason: str | None = None
    #: The event this transition was folded from, as a correlation handle.
    #: There is no route that reads an event yet, so it is a value to quote
    #: in a support question rather than one to follow.
    event_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Run:
    """One attempt an external agent made at something, as `runs` holds it.

    Entirely derived: every column here is folded from events the agent sent
    about itself. Nothing in the control plane writes a run, which is the
    difference between this and `WorkflowExecution` - that one AgentWorld
    creates and drives, this one is a report of work that happened elsewhere.

    Created by exactly one event. `run.started` appends the row and every
    later `run.*` and `tool.*` event patches it, which is why a run that was
    never announced has no record here even if its steps were reported: the
    producer will not invent a `started_at` it was not told.

    `run_ref` is the agent's own name for the attempt and the key every event
    about it carries; it is nullable in the schema, so it is optional here.
    `duration_ms` is computed by PostgreSQL from the two timestamps and is
    NULL while the run is in flight - which is why it is read rather than
    derived here, and why no code may write it.

    No `agent_id`: a run is only ever read through the agent it belongs to,
    so the id would repeat the question. No `workspace_id` or `project_id`,
    for the same reason every other entity leaves them out of a nested read.
    """

    id: UUID
    status: RunStatus
    started_at: datetime
    #: The agent's own identifier for this attempt.
    run_ref: str | None = None
    #: The task this run was working on, when it was working on one.
    task_id: UUID | None = None
    #: Set when the run is one step of a workflow execution rather than the
    #: agent acting on its own. The link is how the two halves of D2 meet.
    execution_step_id: UUID | None = None
    #: Counters the reducers increment, not queries. `runs.step_count` is
    #: read per frame by the island, and counting rows per agent per frame
    #: is the kind of query that looks free until there are a thousand.
    step_count: int = 0
    tool_call_count: int = 0
    error: Mapping[str, Any] | None = None
    finished_at: datetime | None = None
    #: Computed by the database, so no writer can disagree with it. NULL
    #: until the run finishes.
    duration_ms: int | None = None


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
    #: Set means this is that agent's test harness rather than something a
    #: person authored: hidden from the ordinary listing, and refused by the
    #: ordinary draft editor. Carried on the entity because the services
    #: that enforce both read it.
    test_for_agent_id: UUID | None = None
    created_at: datetime | None = None
    archived_at: datetime | None = None

    @property
    def is_test_harness(self) -> bool:
        return self.test_for_agent_id is not None


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


@dataclass(frozen=True, slots=True)
class WorkflowVersion:
    """A published version read as history rather than as a graph to run.

    The same row `WorkflowDraft` carries, minus the graph. A draft is read
    for its `graph` because that is what the editor saves and what the
    orchestrator folds; a version is read for what it says about the
    workflow's past - which version, frozen when - and a list of whole
    graphs would be a large answer to that small question. The one version
    something actually executes is still read through
    `published_version_by_id`, which returns the graph because it is about
    to be run.

    `published_at` is not optional here, unlike on the draft: the only query
    that builds one of these filters on `published_at IS NOT NULL`, so the
    type carries the predicate instead of leaving every reader to re-check
    it.
    """

    id: UUID
    workflow_id: UUID
    version: int
    published_at: datetime
    created_at: datetime | None = None

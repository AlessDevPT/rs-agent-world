"""The enumerated vocabulary of the system.

These are the single source of truth. PostgreSQL gets native types with the
same names and members (migration `0002_enums`), the SQLAlchemy models bind to
those types, and FastAPI publishes them in `/openapi.json`, from which the
TypeScript is generated. One definition, four consumers.

Drift between this module and the database is not a thing to hope about:
`tests/integration/test_schema.py` reads `pg_enum` and asserts the members
match member-for-member, in order.

Every member's `value` is the wire representation. Names are uppercase only to
satisfy Python convention; nothing ever serialises a name.
"""

from __future__ import annotations

from enum import StrEnum


class MemberRole(StrEnum):
    """A user's role within one workspace.

    Ordered weakest to strongest; `at_least` relies on that order, so new
    roles must be inserted at the right rank rather than appended.
    """

    VIEWER = "viewer"
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, other: MemberRole) -> bool:
        return self.rank >= other.rank


_ROLE_RANK: dict[MemberRole, int] = {
    MemberRole.VIEWER: 0,
    MemberRole.MEMBER: 1,
    MemberRole.ADMIN: 2,
    MemberRole.OWNER: 3,
}


class AgentStatus(StrEnum):
    PROVISIONING = "provisioning"  # registered, never seen
    OFFLINE = "offline"  # heartbeat TTL exceeded
    IDLE = "idle"  # connected, no active task
    WORKING = "working"  # executing a task
    BLOCKED = "blocked"  # waiting on input or a dependency
    ERROR = "error"  # last run failed, not recovered
    MAINTENANCE = "maintenance"  # operator-silenced
    # Appended in Phase 2 (migration 0006), and appended rather than inserted
    # because ALTER TYPE ... ADD VALUE puts it last and the drift test
    # asserts declared order, not just membership.
    PAUSED = "paused"  # a managed agent an operator really stopped


class TaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ApiKeyScope(StrEnum):
    """What plane and breadth a key may act on.

    `ingest` is the one agents get: emit events, read their own rows. `control`
    is for CI and automation that manages tasks but must never be able to mint
    further keys. `full` is both, and exists so that "an admin's personal
    token" does not become a reason to widen `control`.
    """

    INGEST = "ingest"
    CONTROL = "control"
    FULL = "full"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"


# --------------------------------------------------------------- ecosystem ---
# Phase 2. Every one of these has a native PostgreSQL type created by
# migration 0006 and is checked against the database by the drift test.


class AgentInteractionMode(StrEnum):
    """What AgentWorld may do with an agent, and what it must not claim to.

    The distinction `docs/proposals/.../01-DOMAIN-AND-PROTOCOL.md` section 2
    calls honesty: an `observed` agent reports and cannot be given work, and
    attempting to assign it a step is a 422 naming the mode rather than a
    line drawn on a diagram and hoped over.
    """

    MANAGED = "managed"  # AgentWorld runs the loop and may really pause it
    DISPATCHABLE = "dispatchable"  # the customer runs it; we can hand it work
    OBSERVED = "observed"  # it only reports; no step may be assigned


class StepKind(StrEnum):
    AGENT_TASK = "agent_task"  # hand work to an agent
    TOOL_CALL = "tool_call"  # call one tool, no agent
    APPROVAL = "approval"  # stop and wait for a human
    CONDITION = "condition"  # branch on prior outputs
    TRANSFORM = "transform"  # reshape data between steps
    WAIT = "wait"  # for a duration or a signal


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_live(self) -> bool:
        """Whether the execution may still progress.

        Used by the no-stuck-executions invariant: a live execution with no
        live dispatch, no pending approval and no terminal step is a bug.
        """
        return self in _LIVE_EXECUTION_STATUSES


_LIVE_EXECUTION_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.PENDING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.WAITING_APPROVAL,
        ExecutionStatus.WAITING_INPUT,
        ExecutionStatus.PAUSED,
    }
)


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STEP_STATUSES


_TERMINAL_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.CANCELLED,
    }
)


class InitiatorKind(StrEnum):
    """Who started an execution.

    Two values and no NULL. A system-initiated execution borrows no user's
    authority: what it may do comes from the published workflow version, not
    from whoever configured or enabled its trigger. An execution with no
    recorded initiator is an audit hole, which is why the column is NOT NULL
    and the id columns are constrained by XOR.
    """

    USER = "user"
    SYSTEM = "system"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


class ConnectionKind(StrEnum):
    """A standing grant or route between components.

    Not a workflow edge. An edge is control flow inside one workflow version;
    a connection is a capability that outlives any single workflow. The island
    draws both as paths, which is where the two get conflated.
    """

    GRANT_TOOL = "grant_tool"  # agent -> tool: may call it
    GRANT_KNOWLEDGE = "grant_knowledge"  # agent -> source: may search it
    DELEGATE = "delegate"  # agent -> agent: may hand work over
    TRIGGER = "trigger"  # workflow -> workflow: completion starts it
    PUBLISH = "publish"  # component -> integration: may send through


class ToolSource(StrEnum):
    BUILTIN = "builtin"
    MCP = "mcp"
    INTEGRATION = "integration"


class SideEffect(StrEnum):
    """How much a capability can be regretted.

    `consequential` is the class that defaults to requiring approval, and it
    is deliberately narrower than "writes something": it means an effect that
    reaches outside AgentWorld and cannot be taken back.
    """

    READ = "read"
    WRITE = "write"
    CONSEQUENTIAL = "consequential"


class TriggerKind(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    INTEGRATION_EVENT = "integration_event"
    WORKFLOW_COMPLETED = "workflow_completed"


class CredentialKind(StrEnum):
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    BASIC = "basic"
    CUSTOM = "custom"


class DispatchKind(StrEnum):
    MANAGED_AGENT_TURN = "managed_agent_turn"
    TOOL_CALL = "tool_call"
    AGENT_PUSH = "agent_push"
    AGENT_TASK_OFFER = "agent_task_offer"
    WEBHOOK_EMIT = "webhook_emit"


class DispatchStatus(StrEnum):
    """The outbox state machine.

    `needs_review` is the one worth reading twice: a lease that expired while
    a consequential side effect was in flight means nobody knows whether it
    landed. Retrying might send the email twice and failing might not send it
    at all, so it is escalated to a person rather than guessed at. Nothing
    retries it automatically - no worker, no timer.
    """

    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


class WorldNodeKind(StrEnum):
    """What kind of object a placed node stands for.

    Structural rather than decorative: this is what decides which table
    `world_nodes.node_id` names. Three of the six have no table yet, and are
    here because the island can place a node for something before Phase 6
    builds its backend.
    """

    AGENT = "agent"
    WORKFLOW = "workflow"
    INTEGRATION = "integration"
    KNOWLEDGE_SOURCE = "knowledge_source"
    TRIGGER = "trigger"
    APPROVAL_DESK = "approval_desk"


class WorldRouteKind(StrEnum):
    """Which domain relationship a drawn line stands for.

    The relationship itself lives in `connections` or `workflow_edges`; only
    its path is presentation, which is what makes rerouting a road a change
    that grants nothing.
    """

    CONNECTION = "connection"
    WORKFLOW_EDGE = "workflow_edge"


#: Every enum that has a native PostgreSQL type, keyed by that type's name.
#: The migration and the drift test both read this, so adding an enum in one
#: place adds it in both.
PG_ENUMS: dict[str, type[StrEnum]] = {
    # Phase 1
    "member_role": MemberRole,
    "agent_status": AgentStatus,
    "task_status": TaskStatus,
    "task_priority": TaskPriority,
    "run_status": RunStatus,
    "event_severity": EventSeverity,
    "api_key_scope": ApiKeyScope,
    "delivery_status": DeliveryStatus,
    # Phase 2
    "agent_interaction_mode": AgentInteractionMode,
    "step_kind": StepKind,
    "execution_status": ExecutionStatus,
    "step_status": StepStatus,
    "initiator_kind": InitiatorKind,
    "approval_decision": ApprovalDecision,
    "connection_kind": ConnectionKind,
    "tool_source": ToolSource,
    "side_effect": SideEffect,
    "trigger_kind": TriggerKind,
    "credential_kind": CredentialKind,
    "dispatch_kind": DispatchKind,
    "dispatch_status": DispatchStatus,
    # Phase 2, presentation. Only the two structural vocabularies are native
    # types; everything about appearance is text, because an art asset must
    # not need a migration.
    "world_node_kind": WorldNodeKind,
    "world_route_kind": WorldRouteKind,
}

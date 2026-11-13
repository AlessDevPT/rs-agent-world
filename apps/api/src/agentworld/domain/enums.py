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
    """Where one step of an execution has got to.

    `WAITING_APPROVAL` was added in migration 0014, while writing the
    reducers, because without it a step held by a human gate is
    indistinguishable from one that has not been reached. The execution
    carries a status of the same name, but that answers "is this blocked?"
    and the approval desk needs "blocked *where*?" - and deriving it by
    joining against the approvals table would make the island's hottest query
    depend on a table that only sometimes has a row.

    Which status is written by whom matters here: this one is set by folding
    an `approval.*` event, while the execution's status is derived from its
    steps by `advance()`. One writer per field, because two is how the two
    disagree.
    """

    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
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

    @property
    def is_unsettled(self) -> bool:
        """Whether this dispatch still owes somebody an outcome.

        Added for T6c, whose reconciler asks "has this execution anything in
        flight?" and must get the same answer as the index that makes the
        question cheap. One predicate rather than two lists that can drift.

        `needs_review` is in the set even though no worker will ever claim it
        again. An execution waiting on a human decision is not an execution
        anything may advance past - that is the whole point of the state.
        """
        return self in _UNSETTLED_DISPATCH_STATUSES

    @property
    def is_terminal(self) -> bool:
        """Whether this dispatch is over, whatever the outcome was.

        The exact complement of `is_unsettled`, written as one so the two can
        never disagree, and named separately because call sites read better
        for it: `settle` stamps `finished_at` when a dispatch is *terminal*,
        which is a statement about being finished rather than about having
        nothing in flight. `StepStatus` carries the same property for the
        same reason.
        """
        return not self.is_unsettled


_UNSETTLED_DISPATCH_STATUSES: frozenset[DispatchStatus] = frozenset(
    {
        DispatchStatus.PENDING,
        DispatchStatus.LEASED,
        DispatchStatus.NEEDS_REVIEW,
    }
)


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


# ------------------------------------------------- derived at read time ---
# The first enum here with no native PostgreSQL type, and the comment is the
# point: nothing persists one of these, so it is absent from `PG_ENUMS` and
# the drift test has nothing to check it against. A column would be the
# wrong shape for it anyway - the answer depends on the clock at the moment
# of the question, so a stored copy would start aging the instant it was
# written.


class AgentLiveness(StrEnum):
    """Whether an agent's heartbeat is current. **Nothing more than that.**

    Four outcomes rather than a boolean, because three of them are not
    "stale" and collapsing them would publish a claim the data does not
    support.

    What this deliberately does *not* say: that a `stale` agent is dead,
    disconnected, or unable to make progress. An agent's heartbeat and its
    work are reported by different event types and folded into different
    columns - `agent.heartbeat` moves `agents.last_heartbeat_at`, while
    `run.*` events never touch the agent row at all - so an agent can be
    mid-run, reporting steps every second, and still be `stale` here because
    it never sends the beat. That is a true statement about its heartbeat
    and not a statement about its health. `GET .../agents/{id}/runs` is
    where the work is.
    """

    #: A `managed` agent, whose loop AgentWorld runs itself. There is no
    #: external worker to hear from, so there is no silence to measure -
    #: and calling every managed agent `stale` for ever would be noise
    #: rather than information.
    NOT_APPLICABLE = "not_applicable"
    #: No heartbeat has ever been folded. An agent that was never seen is
    #: not an agent that went quiet, and `AgentStatus.PROVISIONING` already
    #: means "registered, never seen" - this must not contradict it.
    NEVER_REPORTED = "never_reported"
    #: Heard from within its own `heartbeat_ttl_seconds`.
    FRESH = "fresh"
    #: Silent for longer than its own `heartbeat_ttl_seconds`.
    STALE = "stale"


class ActivityEvidence(StrEnum):
    """Which timestamp an external run's newest activity came from.

    **Provenance, not health.** It says which clock is newest and nothing
    else: not whether the run has steps, not whether the agent is working,
    and never that a run has failed.
    """

    #: The run row's own `last_event_at` is newest. That column is moved by
    #: `run.started`, `run.finished` and `run.failed` and by nothing else -
    #: so this is also the answer when a step's timestamp ties with it, and
    #: when a late step's is older. It does **not** mean "no steps", and it
    #: is not evidence that no tool calls were made: tool calls move no
    #: clock at all, so they can never produce `TRACE_STEP`.
    RUN_EVENT = "run_event"
    #: A step's `last_event_at` is *strictly* newer than the run row's, so
    #: the trace knows something the run row does not.
    TRACE_STEP = "trace_step"
    #: Neither clock is set and `started_at` is all there is. No ingested
    #: event can produce this - `DerivedStateRepository._append_run` writes
    #: `last_event_at` in the same INSERT that creates the row - so it
    #: survives for rows written directly: a seeded fixture, or a run from
    #: before the producer existed.
    NONE = "none"


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

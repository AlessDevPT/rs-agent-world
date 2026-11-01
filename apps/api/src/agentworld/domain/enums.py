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


#: Every enum that has a native PostgreSQL type, keyed by that type's name.
#: The migration and the drift test both read this, so adding an enum in one
#: place adds it in both.
PG_ENUMS: dict[str, type[StrEnum]] = {
    "member_role": MemberRole,
    "agent_status": AgentStatus,
    "task_status": TaskStatus,
    "task_priority": TaskPriority,
    "run_status": RunStatus,
    "event_severity": EventSeverity,
    "api_key_scope": ApiKeyScope,
    "delivery_status": DeliveryStatus,
}

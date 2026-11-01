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

from agentworld.domain.enums import ApiKeyScope, MemberRole


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

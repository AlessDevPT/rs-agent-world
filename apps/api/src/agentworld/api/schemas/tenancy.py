"""Request and response models for the tenancy endpoints.

Separate from `domain.entities` on purpose: these are the wire contract, and
the wire contract has to be able to stay still while the entities change. They
also carry the examples and constraints that end up in `/openapi.json`, and
from there in the generated TypeScript client.

**PATCH semantics.** Every optional field means "leave this alone" when
omitted. Explicitly clearing a field to null is deliberately *not* expressible
yet: supporting both would need every field typed as a three-state value
(absent / null / set), which is a real feature with a real cost, and nothing in
Phase 1 needs it. Stated here rather than discovered later.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from agentworld.domain.enums import ApiKeyScope, MemberRole

Name = Annotated[str, Field(min_length=1, max_length=200)]
Slug = Annotated[str, Field(min_length=2, max_length=48, examples=["support-automation"])]


class Schema(BaseModel):
    """Base for every model here.

    `extra="forbid"` on requests is a deliberate asymmetry with the protocol's
    tolerance rules in `docs/03-API.md` section 5: an *event* with unknown
    fields is stored, because an agent SDK may be newer than the server. A
    control-plane request with an unknown field is a mistake - a typo'd
    `discription` that silently does nothing is worse than a 422.
    """

    model_config = ConfigDict(extra="forbid")


class ReadSchema(BaseModel):
    """Base for responses, which are built from entities rather than parsed."""

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- users ---


class UserOut(ReadSchema):
    id: UUID
    email: str
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None


class MePatch(Schema):
    display_name: Name | None = None
    avatar_url: Annotated[str, Field(max_length=2048)] | None = None


# ---------------------------------------------------------------- workspaces ---


class WorkspaceOut(ReadSchema):
    id: UUID
    slug: str
    name: str
    created_by: UUID
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


class WorkspaceMembershipOut(ReadSchema):
    """A workspace plus the caller's own role in it."""

    workspace: WorkspaceOut
    role: MemberRole


class MeOut(ReadSchema):
    user: UserOut
    workspaces: list[WorkspaceMembershipOut]
    needs_onboarding: bool = Field(
        description="True when the user belongs to no workspace yet, which the web app "
        "turns into the create-workspace screen rather than an empty dashboard."
    )


class WorkspaceCreate(Schema):
    name: Name
    slug: Slug | None = Field(
        default=None,
        description="Derived from the name when omitted, so a client need not think "
        "about slugs at all.",
    )


class WorkspacePatch(Schema):
    name: Name | None = None
    slug: Slug | None = None


class MemberOut(ReadSchema):
    user: UserOut
    role: MemberRole
    joined_at: dt.datetime | None = None


class MemberAdd(Schema):
    email: EmailStr = Field(
        description="The address of a user who has already signed up. Inviting someone "
        "without an account needs a pending-invitation table and an email path, "
        "neither of which exists yet."
    )
    role: MemberRole = Field(
        default=MemberRole.MEMBER,
        description="May not be 'owner': ownership is transferred through a role "
        "change, never granted here.",
    )


class MemberRolePatch(Schema):
    role: MemberRole


# ------------------------------------------------------------------ projects ---


class ProjectOut(ReadSchema):
    id: UUID
    workspace_id: UUID
    slug: str
    name: str
    description: str | None = None
    settings: dict[str, object] = Field(default_factory=dict)
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


class ProjectCreate(Schema):
    name: Name
    slug: Slug | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    settings: dict[str, object] | None = Field(
        default=None,
        examples=[{"heartbeat_ttl_seconds": 60, "event_retention_days": 90}],
    )


class ProjectPatch(Schema):
    name: Name | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    settings: dict[str, object] | None = Field(
        default=None,
        description="Replaces the whole object rather than merging into it, so a "
        "setting can be removed.",
    )


# ------------------------------------------------------------------ api keys ---


class ApiKeyOut(ReadSchema):
    """A key as it is ever shown again after minting: prefix, never secret."""

    id: UUID
    workspace_id: UUID
    project_id: UUID | None = None
    name: str
    prefix: str = Field(
        description="The public half of the key, and the handle to revoke it by.",
        examples=["aw_live_8f2c1a3d5e70"],
    )
    scope: ApiKeyScope
    agent_key_allowlist: list[str] | None = None
    rate_limit_rps: int
    created_by: UUID
    created_at: dt.datetime | None = None
    last_used_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    """The 201 response, and the only place the plaintext key ever appears.

    Not stored, not logged, not recoverable. A client that does not capture it
    here has to mint another key.
    """

    key: str = Field(
        description="The full key. Shown exactly once, in this response, ever.",
        examples=["aw_live_8f2c1a3d5e70.Zm9vYmFyYmF6cXV1eA"],
    )


class ApiKeyCreate(Schema):
    name: Name
    scope: ApiKeyScope = ApiKeyScope.INGEST
    project_id: UUID | None = Field(
        default=None,
        description="Required for the 'ingest' scope: an unpinned ingest key could "
        "emit events into every project in the workspace.",
    )
    agent_key_allowlist: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None,
        description="Agent keys this credential may speak for. A single trailing "
        "'*' is the only pattern supported. Omit to permit any agent in the "
        "pinned project.",
        examples=[["etl-worker-eu-*"]],
    )
    rate_limit_rps: Annotated[int, Field(ge=1, le=10_000)] = 50
    expires_at: dt.datetime | None = None

"""Who is making a request.

Two types, never one. A `UserPrincipal` comes from a verified Supabase JWT; a
`KeyPrincipal` comes from a verified API key. They are separate classes rather
than one class with a `kind` field precisely so that a control-plane function
cannot accidentally accept an agent's key: the type checker refuses it before
any test does.

`docs/01-ARCHITECTURE.md` section 5.4 calls mixing the planes "the single most
likely source of a privilege bug". This module is the structural answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from agentworld.domain.enums import ApiKeyScope, MemberRole


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    """A human (or the web app acting for one), authenticated by JWT."""

    user_id: UUID
    email: str
    #: workspace_id -> role. Resolved once per request; the absence of a key
    #: means "this workspace is not visible to this user", which the policy
    #: turns into 404 rather than 403.
    memberships: Mapping[UUID, MemberRole] = field(default_factory=dict)

    def role_in(self, workspace_id: UUID) -> MemberRole | None:
        return self.memberships.get(workspace_id)


@dataclass(frozen=True, slots=True)
class KeyPrincipal:
    """An agent or automation, authenticated by API key.

    Pinned to exactly one workspace and (for ingest keys) one project. The
    optional `agent_key_allowlist` narrows it further to specific agent
    identities, so a key leaked from one worker cannot impersonate another.
    """

    api_key_id: UUID
    workspace_id: UUID
    project_id: UUID | None
    scope: ApiKeyScope
    agent_key_allowlist: Sequence[str] | None = None
    rate_limit_rps: int = 50

    def may_act_as(self, agent_key: str) -> bool:
        """Whether this key is allowed to speak for `agent_key`.

        A NULL allowlist means any agent in the pinned project; patterns
        support a single trailing `*` only. Richer globbing was considered and
        rejected: an allowlist is a security control, and a control whose
        behaviour nobody can predict is not one.
        """
        if self.agent_key_allowlist is None:
            return True
        folded = agent_key.casefold()
        return any(
            folded.startswith(pattern[:-1].casefold())
            if pattern.endswith("*")
            else folded == pattern.casefold()
            for pattern in self.agent_key_allowlist
        )


Principal = UserPrincipal | KeyPrincipal

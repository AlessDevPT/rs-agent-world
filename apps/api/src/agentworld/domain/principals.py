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


@dataclass(frozen=True, slots=True)
class SystemPrincipal:
    """AgentWorld running work nobody is currently asking for.

    A scheduled trigger fires at 07:00 and an execution starts. Whose
    authority is it running on? The tempting answer - whoever last enabled
    the trigger - is wrong, and wrong in a way that gets worse with time: that
    person may have left, had their role reduced, or never intended their
    account to be the standing authority for work they configured once. Worse,
    it makes *enabling a trigger* a way to launder a permission, since the
    work would then run as whoever touched the switch last.

    So the authority comes from the **published workflow version** instead.
    That version is immutable, was published by someone who held
    `workflow.publish` at the time, and names exactly the steps, tools and
    connections the run may use. Nothing about this principal is read from a
    mutable row.

    What follows is the point of the class: it can perform **no** control-plane
    action. `authorize()` refuses every member of `Action` for it, and a test
    asserts that exhaustively. A scheduled run may do what its frozen version
    says and may not create an agent, mint a key, or widen a grant - however
    the workflow is written and whoever wrote it.

    `label` is a human-readable description of the cause, copied at execution
    time, so the audit trail still reads "nightly competitor brief" after the
    trigger that caused it has been deleted.
    """

    workspace_id: UUID
    project_id: UUID
    #: The frozen definition this run's authority comes from.
    workflow_version_id: UUID
    #: What caused it, in words, copied rather than referenced.
    label: str
    #: The trigger that fired, for the audit trail only. Never consulted for
    #: authority, and nullable because the trigger may since have been
    #: deleted while the execution record must survive it.
    trigger_id: UUID | None = None


#: Every kind of caller. `authorize()` narrows on this union and refuses
#: two of the three outright, so adding a fourth is a change you cannot make
#: without the type checker pointing at every place that has to decide.
Principal = UserPrincipal | KeyPrincipal | SystemPrincipal

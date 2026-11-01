"""What a service knows about the call it is serving.

Separate from the principal: the principal is *who*, this is *from where* and
*as part of which request*. Both end up in `audit_log`, and keeping them apart
means a background job can act with a principal and an empty call context
without inventing a fake IP address.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agentworld.domain.principals import Principal, UserPrincipal


@dataclass(frozen=True, slots=True)
class CallContext:
    """Request metadata recorded alongside every mutation."""

    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None

    @classmethod
    def background(cls) -> CallContext:
        """For workers and jobs: no request, so nothing to record."""
        return cls()


def actor_of(principal: Principal) -> tuple[str, UUID | None]:
    """The `(actor_type, actor_id)` pair for an audit row.

    A key's own id, not its workspace, is the actor: after an incident the
    question is which credential did this, and revoking that one key is the
    answer.

    `Principal` is exactly these two types, so there is no fallback branch.
    The third actor kind the column allows, `system`, is written by the
    workers in later phases, which act with no principal at all rather than
    with a placeholder one.
    """
    if isinstance(principal, UserPrincipal):
        return "user", principal.user_id
    return "api_key", principal.api_key_id

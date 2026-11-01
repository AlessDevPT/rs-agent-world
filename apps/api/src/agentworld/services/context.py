"""What a service knows about the call it is serving.

Separate from the principal: the principal is *who*, this is *from where* and
*as part of which request*. Both end up in `audit_log`, and keeping them apart
means a background job can act with a principal and an empty call context
without inventing a fake IP address.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agentworld.domain.principals import Principal, SystemPrincipal, UserPrincipal


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

    A `SystemPrincipal` has no actor id, and that is not a gap. There is no
    user to name - the whole point of the type is that a scheduled run
    borrows nobody's authority - and writing the id of whoever enabled the
    trigger would put a person's name against work they did not do. What
    identifies the run instead is the execution's own immutable initiator:
    `initiator_kind = 'system'` and `initiator_label`, which survive the
    trigger being deleted.
    """
    if isinstance(principal, UserPrincipal):
        return "user", principal.user_id
    if isinstance(principal, SystemPrincipal):
        return "system", None
    return "api_key", principal.api_key_id

"""Writing audit rows.

One function, so that every mutating control-plane call records the same
shape. `docs/01-ARCHITECTURE.md` section 5.4 requires the row; this is what
makes writing it a one-liner at each call site rather than ten lines of
plumbing that some route will eventually skip.

The write happens inside the caller's transaction on purpose. A rolled-back
mutation leaves no claim that it happened, and a successful one cannot fail to
be recorded - which is the only version of an audit log worth consulting.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from agentworld.domain.principals import Principal
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork
from agentworld.services.context import CallContext, actor_of


async def record(
    work: UnitOfWork,
    principal: Principal,
    context: CallContext,
    *,
    workspace_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Append one row describing what the caller just did.

    `resource_id` defaults to the workspace, which is the right answer for
    the actions whose resource *is* the workspace and saves every caller
    passing it twice.
    """
    actor_type, actor_id = actor_of(principal)
    await work.audit(TenantScope(workspace_id)).append(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id if resource_id is not None else workspace_id,
        request_id=context.request_id,
        metadata=metadata or {},
        ip=context.ip,
        user_agent=context.user_agent,
    )

"""The ingest plane's self-description.

`GET /v1/self/key` answers "is this key working, and what may it do". It is
the first call an integrator makes after minting a key, and the only ingest
endpoint Phase 1 has: `GET /v1/self` proper returns the calling agent's own
row, and there are no agents until Phase 2.

It is also what makes the plane separation testable end to end. A control
token sent here is a 401, a key sent to a control route is a 401, and both
are asserted over real HTTP rather than only at the service level.

Module named `self_` because `self` is a keyword; the route path is not.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agentworld.api.deps import CurrentKey
from agentworld.domain.enums import ApiKeyScope

router = APIRouter(prefix="/v1/self", tags=["ingest"])


class KeyIdentity(BaseModel):
    """What a key is, as the key itself can see it.

    Deliberately no name, no creator and no created_at: those belong to the
    human who minted it, and an agent holding the key has no business
    enumerating facts about the workspace it sits in.
    """

    api_key_id: UUID
    workspace_id: UUID
    project_id: UUID | None = Field(
        default=None, description="The project this key is pinned to, if any."
    )
    scope: ApiKeyScope
    rate_limit_rps: int
    agent_key_allowlist: list[str] | None = Field(
        default=None,
        description="Agent keys this credential may speak for. Null means any agent "
        "in the pinned project.",
    )


@router.get("/key", response_model=KeyIdentity, summary="What this API key is")
async def get_key_identity(principal: CurrentKey) -> KeyIdentity:
    return KeyIdentity(
        api_key_id=principal.api_key_id,
        workspace_id=principal.workspace_id,
        project_id=principal.project_id,
        scope=principal.scope,
        rate_limit_rps=principal.rate_limit_rps,
        agent_key_allowlist=list(principal.agent_key_allowlist)
        if principal.agent_key_allowlist is not None
        else None,
    )

"""The ticket route. The stream it opens is in `api/ws/gateway.py`.

One endpoint, and it is here rather than in `api/ws/` on purpose: this is an
ordinary authorized control-plane route - a JWT in, a role checked, an audit
row written - and it belongs with the others that look like that. What makes
it exist at all is that the socket cannot be one: a browser cannot set a
header on a WebSocket, and a token in a query string reaches every access log
on the way (`03-API.md` §6).
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from agentworld.api.deps import CallContextDep, CurrentUser, RealtimeTicketServiceDep

router = APIRouter(prefix="/v1/projects/{project_id}", tags=["realtime"])


class TicketView(BaseModel):
    """A ticket, once. There is no route that returns it again."""

    ticket: str = Field(
        description=(
            "Present it as the `ticket` query parameter of "
            "`GET /v1/ws/projects/{project_id}`. Single-use: a second "
            "connection with the same ticket is refused."
        )
    )
    expires_at: dt.datetime = Field(
        description="After this instant the ticket is refused, spent or not."
    )
    project_id: UUID
    stream_path: str = Field(
        description=(
            "Where to connect. Returned so a client does not have to build the "
            "path itself, which is how a client ends up pinned to a URL shape."
        )
    )


@router.post(
    "/ws-ticket",
    response_model=TicketView,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a single-use WebSocket ticket for this project",
    description=(
        "Needs `realtime.subscribe`, which is `viewer`: watching is reading, and "
        "every channel on the stream carries entities this role can already read "
        "over REST. The ticket is valid for `WS_TICKET_TTL_SECONDS` and for one "
        "connection."
    ),
)
async def mint_ticket(
    project_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: RealtimeTicketServiceDep,
) -> TicketView:
    minted = await service.mint(principal, context, project_id)
    return TicketView(
        ticket=minted.ticket,
        expires_at=minted.expires_at,
        project_id=minted.project_id,
        stream_path=f"/v1/ws/projects/{minted.project_id}",
    )

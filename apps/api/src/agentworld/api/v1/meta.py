"""GET /v1/meta - what this server is and what it speaks.

An integrator's first call. It reports the protocol version separately from the
application version, because the protocol is the thing they build against.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agentworld.domain.protocol.version import PROTOCOL_VERSION

router = APIRouter(prefix="/v1", tags=["meta"])

API_VERSION = "0.1.0"


class Meta(BaseModel):
    api_version: str = Field(examples=["0.1.0"])
    protocol_version: str = Field(examples=["1.0"])
    adapters: list[str] = Field(
        default_factory=list,
        description="Adapter slugs this server has registered. Populated in Phase 7.",
    )
    server_time: datetime = Field(
        description="Server clock. Clients may use it to measure their own skew "
        "before sending occurred_at values."
    )


@router.get("/meta", response_model=Meta, summary="Server and protocol metadata")
async def meta() -> Meta:
    return Meta(
        api_version=API_VERSION,
        protocol_version=PROTOCOL_VERSION,
        adapters=[],
        server_time=datetime.now(UTC),
    )

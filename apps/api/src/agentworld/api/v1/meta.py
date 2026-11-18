"""GET /v1/meta - what this server is and what it speaks.

An integrator's first call. It reports the protocol version separately from the
application version, because the protocol is the thing they build against.

**`providers` is here rather than at a route of its own.** It answers "what
can this build do", which is what this endpoint is for, and it is one list of
slugs with no per-tenant content: a dedicated route would need a role-matrix
row and an authorization decision for information that is the same for every
caller. If a provider ever carries more than a slug - models, limits,
capabilities - that is the moment it earns its own route, and nothing here
stops that.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agentworld.domain.protocol.version import PROTOCOL_VERSION
from agentworld.infra.providers import available as available_providers

router = APIRouter(prefix="/v1", tags=["meta"])

API_VERSION = "0.1.0"


class Meta(BaseModel):
    api_version: str = Field(examples=["0.1.0"])
    protocol_version: str = Field(examples=["1.0"])
    adapters: list[str] = Field(
        default_factory=list,
        description="Adapter slugs this server has registered. Populated in Phase 7.",
    )
    providers: list[str] = Field(
        default_factory=list,
        description=(
            "Model providers this build can serve, sorted. A model profile may name "
            "only a `provider_slug` from this list - anything else is a 422 on the "
            "field - so these are the choices a client should offer."
        ),
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
        # Read from the registry rather than listed here: the registry is what
        # `build()` consults at dispatch time, so this cannot drift from what
        # the server will actually do.
        providers=sorted(available_providers()),
        server_time=datetime.now(UTC),
    )

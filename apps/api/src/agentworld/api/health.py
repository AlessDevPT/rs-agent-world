"""Liveness and readiness.

Kept apart on purpose: /healthz answers "is this process alive" and must never
touch a dependency, or a database blip restarts every container. /readyz
answers "should traffic come here" and does check dependencies.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from agentworld.api.deps import get_database
from agentworld.infra.db import Database

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]


class Readiness(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]


@router.get("/healthz", response_model=Health, summary="Liveness")
async def healthz() -> Health:
    return Health(status="ok")


@router.get("/readyz", response_model=Readiness, summary="Readiness")
async def readyz(
    response: Response, database: Annotated[Database, Depends(get_database)]
) -> Readiness:
    # Each dependency is added here as it lands and reports by name, so a
    # degraded readiness says which one is at fault. Redis joins in Phase 4.
    checks: dict[str, str] = {
        "postgres": "ok" if await database.ping() else "unreachable",
    }
    degraded = [name for name, state in checks.items() if state != "ok"]
    if degraded:
        response.status_code = 503
        return Readiness(status="degraded", checks=checks)
    return Readiness(status="ready", checks=checks)

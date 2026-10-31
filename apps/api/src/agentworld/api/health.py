"""Liveness and readiness.

Kept apart on purpose: /healthz answers "is this process alive" and must never
touch a dependency, or a database blip restarts every container. /readyz
answers "should traffic come here" and does check dependencies.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

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
async def readyz(response: Response) -> Readiness:
    # Phase 0 has no dependencies wired yet. Each is added here as it lands
    # (Postgres in Phase 1, Redis in Phase 4) and reports by name, so a
    # degraded readiness says which dependency is at fault.
    checks: dict[str, str] = {}
    degraded = [name for name, state in checks.items() if state != "ok"]
    if degraded:
        response.status_code = 503
        return Readiness(status="degraded", checks=checks)
    return Readiness(status="ready", checks=checks)

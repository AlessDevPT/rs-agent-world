"""Application factory.

`create_app()` takes settings as an argument so tests can build an app with a
different configuration without touching the environment or a module-level
global. `app` at the bottom is what uvicorn imports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentworld.api.errors import register_error_handlers
from agentworld.api.health import router as health_router
from agentworld.api.middleware import RequestContextMiddleware
from agentworld.api.v1.meta import API_VERSION
from agentworld.api.v1.meta import router as meta_router
from agentworld.infra.logging import configure_logging, get_logger
from agentworld.settings import Settings, get_settings

DESCRIPTION = """
Universal AI agent monitoring and management.

Two authentication planes, never interchangeable:

- **Control plane** - a Supabase Auth JWT in `Authorization: Bearer`, for humans
  and the web app.
- **Ingest plane** - an API key in `X-AgentWorld-Key`, for monitored agents.

The canonical agent protocol is documented in `docs/03-API.md` and versioned
independently of this API.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger("api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "api_starting",
            environment=settings.environment,
            event_bus=settings.event_bus,
            version=API_VERSION,
        )
        # Phase 1 opens the database pool here, Phase 4 the event bus.
        yield
        log.info("api_stopped")

    app = FastAPI(
        title="AgentWorld API",
        description=DESCRIPTION,
        version=API_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "Retry-After"],
    )

    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(meta_router)

    return app


app = create_app()

"""Application factory.

`create_app()` takes settings as an argument so tests can build an app with a
different configuration without touching the environment or a module-level
global. `app` at the bottom is what uvicorn imports.

Resources - the connection pool, the JWKS cache - are built in the lifespan
and hung on `app.state`, which is what the dependencies in `api/deps.py` read.
Building them at import time instead would mean a test that only wanted a
route table opened a connection pool, and a module-level engine bound to
whichever event loop happened to import it first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentworld.api.errors import register_error_handlers
from agentworld.api.health import router as health_router
from agentworld.api.middleware import RequestContextMiddleware
from agentworld.api.v1 import (
    agents,
    api_keys,
    ingest,
    me,
    projects,
    self_,
    workflows,
    workspaces,
)
from agentworld.api.v1.meta import API_VERSION
from agentworld.api.v1.meta import router as meta_router
from agentworld.infra.auth.provider import AuthProvider
from agentworld.infra.auth.supabase import SupabaseAuthProvider
from agentworld.infra.db import Database
from agentworld.infra.logging import configure_logging, get_logger
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.settings import Settings, get_settings

DESCRIPTION = """
Universal AI agent monitoring and management.

Two authentication planes, never interchangeable:

- **Control plane** - a Supabase Auth JWT in `Authorization: Bearer`, for humans
  and the web app.
- **Ingest plane** - an API key in `X-AgentWorld-Key`, for monitored agents.

A credential for one plane is rejected on the other with `401`, not downgraded.

The canonical agent protocol is documented in `docs/03-API.md` and versioned
independently of this API.
"""


def _build_auth_provider(settings: Settings) -> AuthProvider | None:
    """The identity provider, or None when it is not configured.

    None is a legitimate local-development state: `SUPABASE_JWKS_URL` is empty
    in a fresh checkout, and the control plane then answers 503 rather than
    pretending to authenticate. Deployed environments cannot reach it -
    `Settings` refuses to construct without the URL outside local and test.
    """
    if not settings.supabase_jwks_url:
        return None
    issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1" if settings.supabase_url else None
    return SupabaseAuthProvider(
        jwks_url=settings.supabase_jwks_url,
        audience=settings.supabase_jwt_audience,
        issuer=issuer,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger("api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.database_url)
        auth_provider = _build_auth_provider(settings)

        app.state.settings = settings
        app.state.database = database
        app.state.unit_of_work_factory = UnitOfWorkFactory(database)
        app.state.auth_provider = auth_provider

        log.info(
            "api_starting",
            environment=settings.environment,
            event_bus=settings.event_bus,
            version=API_VERSION,
            auth_configured=auth_provider is not None,
        )
        try:
            yield
        finally:
            # Ordered: stop accepting work, then release the pool. Phase 4
            # closes the event bus between the two.
            if auth_provider is not None:
                await auth_provider.close()
            await database.dispose()
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
    app.include_router(me.router)
    app.include_router(workspaces.router)
    # Projects and keys each expose a workspace-nested collection router and an
    # id-addressed item router, matching the two shapes in docs/03-API.md.
    app.include_router(projects.workspace_router)
    app.include_router(projects.router)
    app.include_router(api_keys.workspace_router)
    app.include_router(api_keys.router)
    app.include_router(self_.router)
    app.include_router(ingest.router)
    app.include_router(agents.project_router)
    app.include_router(agents.profiles_router)
    app.include_router(workflows.connections_router)
    app.include_router(workflows.workflows_router)

    return app


app = create_app()

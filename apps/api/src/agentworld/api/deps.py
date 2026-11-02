"""FastAPI dependencies, including the two credential planes.

The plane separation from `docs/01-ARCHITECTURE.md` section 5.4 is implemented
here, and the shape is the point: `current_user` can only ever return a
`UserPrincipal`, `current_key` can only ever return a `KeyPrincipal`, and
there is no dependency that returns either. A control-plane route cannot
accidentally accept an agent's key, because no dependency exists that would
hand it one.

Both planes also reject the *other* plane's credential outright rather than
ignoring it. A caller who sends an API key to a control-plane route has made
a mistake worth being told about; silently treating it as anonymous would
produce a confusing 401 about a missing token they did send.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated

from fastapi import Depends, Request

from agentworld.domain.errors import AuthenticationFailedError, DependencyUnavailableError
from agentworld.domain.principals import KeyPrincipal, UserPrincipal
from agentworld.infra.auth.provider import AuthProvider
from agentworld.infra.auth.supabase import CHALLENGE as BEARER_CHALLENGE
from agentworld.infra.db import Database
from agentworld.infra.secrets import EnvelopeSecretStore
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.api_keys import CHALLENGE as KEY_CHALLENGE
from agentworld.services.api_keys import ApiKeyService
from agentworld.services.context import CallContext
from agentworld.services.credentials import CredentialService
from agentworld.services.definitions import (
    AgentService,
    ConnectionService,
    ModelProfileService,
    WorkflowService,
)
from agentworld.services.executions import ExecutionService
from agentworld.services.identity import IdentityService, resolve_principal
from agentworld.services.ingest import IngestService
from agentworld.services.projects import ProjectService
from agentworld.services.workspaces import WorkspaceService
from agentworld.settings import Settings

API_KEY_HEADER = "X-AgentWorld-Key"


# ------------------------------------------------------------- app resources ---


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_unit_of_work_factory(request: Request) -> UnitOfWorkFactory:
    factory: UnitOfWorkFactory = request.app.state.unit_of_work_factory
    return factory


def get_auth_provider(request: Request) -> AuthProvider:
    """The configured identity provider, or a 503.

    An unconfigured `SUPABASE_JWKS_URL` is a server problem, not a caller
    problem, so it is 503 rather than 401. Deployed environments cannot reach
    this state: `Settings` refuses to construct without the URL outside local
    and test.
    """
    provider: AuthProvider | None = getattr(request.app.state, "auth_provider", None)
    if provider is None:
        raise DependencyUnavailableError(
            "Authentication is not configured on this server (SUPABASE_JWKS_URL is unset)"
        )
    return provider


SettingsDep = Annotated[Settings, Depends(get_settings)]
FactoryDep = Annotated[UnitOfWorkFactory, Depends(get_unit_of_work_factory)]
AuthProviderDep = Annotated[AuthProvider, Depends(get_auth_provider)]


# ------------------------------------------------------------------- context ---


def get_call_context(request: Request) -> CallContext:
    """Request metadata for the audit log.

    The client IP is taken from the socket, not from `X-Forwarded-For`: that
    header is caller-controlled unless a trusted proxy rewrites it, and an
    audit log recording an attacker's chosen IP is worse than one recording
    the load balancer's. Honouring the header is a deployment decision, made
    in Phase 8 alongside the proxy configuration it depends on.
    """
    return CallContext(
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def _client_ip(request: Request) -> str | None:
    """The peer address, but only if it really is one.

    `audit_log.ip` is a PostgreSQL `inet` column, and asyncpg refuses to
    encode a value that is not an address - so a peer name that is not an IP
    would raise mid-insert and turn the audit write into a 500 on a request
    that was otherwise fine. Starlette reports a non-address host in more
    cases than one might expect: a Unix-socket deployment, and the test
    client, whose peer is literally "testclient".

    An audit field must never be able to fail the request it is recording, so
    an unparseable host is dropped rather than stored or raised on.
    """
    if request.client is None:
        return None
    host = request.client.host
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


CallContextDep = Annotated[CallContext, Depends(get_call_context)]


# --------------------------------------------------------------- the planes ---


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def current_user(
    request: Request,
    provider: AuthProviderDep,
    factory: FactoryDep,
) -> UserPrincipal:
    """The control plane. A verified human, with their memberships resolved.

    Resolving memberships here rather than lazily is what lets `authorize`
    stay a pure function and what provides the row-level security visibility
    set for the rest of the request. It costs one query per request.
    """
    if request.headers.get(API_KEY_HEADER):
        raise AuthenticationFailedError(
            "This endpoint is on the control plane and needs a user token, not an API key",
            challenge=BEARER_CHALLENGE,
            reason="wrong_plane",
        )

    token = _bearer_token(request)
    if token is None:
        raise AuthenticationFailedError(
            "A bearer token is required",
            challenge=BEARER_CHALLENGE,
            reason="missing_token",
        )

    identity = await provider.verify(token)
    async with factory.begin() as work:
        # No visibility set: `users` and `workspace_members` carry no RLS
        # policy precisely because this is the query that produces the set.
        return await resolve_principal(work, identity)


async def current_key(request: Request, factory: FactoryDep, settings: SettingsDep) -> KeyPrincipal:
    """The ingest plane. A verified API key, pinned to its project."""
    if request.headers.get("authorization"):
        raise AuthenticationFailedError(
            "This endpoint is on the ingest plane and needs an API key, not a user token",
            challenge=KEY_CHALLENGE,
            reason="wrong_plane",
        )

    presented = request.headers.get(API_KEY_HEADER, "").strip()
    if not presented:
        raise AuthenticationFailedError(
            f"An API key is required in the {API_KEY_HEADER} header",
            challenge=KEY_CHALLENGE,
            reason="missing_key",
        )

    service = ApiKeyService(
        factory, pepper=settings.api_key_pepper, environment=settings.environment
    )
    return await service.authenticate(presented)


CurrentUser = Annotated[UserPrincipal, Depends(current_user)]
CurrentKey = Annotated[KeyPrincipal, Depends(current_key)]


# -------------------------------------------------------------- the services ---


def get_identity_service(factory: FactoryDep) -> IdentityService:
    return IdentityService(factory)


def get_workspace_service(factory: FactoryDep) -> WorkspaceService:
    return WorkspaceService(factory)


def get_project_service(factory: FactoryDep) -> ProjectService:
    return ProjectService(factory)


def get_agent_service(factory: FactoryDep) -> AgentService:
    return AgentService(factory)


def get_model_profile_service(factory: FactoryDep) -> ModelProfileService:
    return ModelProfileService(factory)


def get_connection_service(factory: FactoryDep) -> ConnectionService:
    return ConnectionService(factory)


def get_workflow_service(factory: FactoryDep) -> WorkflowService:
    return WorkflowService(factory)


def get_ingest_service(factory: FactoryDep, settings: SettingsDep) -> IngestService:
    """The batch limits are settings, because they are an operator's call.

    A fleet of chatty agents and a handful of careful ones want different
    numbers, and both are reasonable. What is not reasonable is an unbounded
    batch, which is why there is no "0 means unlimited".
    """
    return IngestService(
        factory,
        max_batch_events=settings.ingest_max_batch_events,
        max_future_skew_seconds=settings.ingest_max_future_skew_seconds,
    )


def get_api_key_service(factory: FactoryDep, settings: SettingsDep) -> ApiKeyService:
    return ApiKeyService(factory, pepper=settings.api_key_pepper, environment=settings.environment)


def get_execution_service(factory: FactoryDep) -> ExecutionService:
    """Publishing, running, and applying decisions.

    No settings: every limit this service respects is either a database
    constraint or a decision `advance()` already took.
    """
    return ExecutionService(factory)


def get_credential_service(factory: FactoryDep, settings: SettingsDep) -> CredentialService:
    """The keyring comes from settings, which refused to boot without it.

    Built per request like every other service here. The keyring is a few
    microseconds of base64 and holding one on `app.state` instead would keep
    key material alive in a longer-lived object for no gain.
    """
    return CredentialService(factory, store=EnvelopeSecretStore(settings.credential_keyring))


IdentityServiceDep = Annotated[IdentityService, Depends(get_identity_service)]
WorkspaceServiceDep = Annotated[WorkspaceService, Depends(get_workspace_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
ApiKeyServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]
IngestServiceDep = Annotated[IngestService, Depends(get_ingest_service)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
ModelProfileServiceDep = Annotated[ModelProfileService, Depends(get_model_profile_service)]
ConnectionServiceDep = Annotated[ConnectionService, Depends(get_connection_service)]
WorkflowServiceDep = Annotated[WorkflowService, Depends(get_workflow_service)]
CredentialServiceDep = Annotated[CredentialService, Depends(get_credential_service)]
ExecutionServiceDep = Annotated[ExecutionService, Depends(get_execution_service)]

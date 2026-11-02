"""Outbound HTTP: the one place a request leaves this process.

Every caller that reaches a user-supplied destination - the `http_request`
tool, `agent_push`, `webhook_emit`, an MCP server connection, a knowledge
fetch - goes through `GuardedClient`. So does every caller whose destination
comes from operator configuration rather than a user, not because those need
the protection but because one client means one place to review and one place
that can be wrong.

The single exemption is `infra/auth/supabase.py`, whose JWKS URL is operator
configuration and which runs before any tenant context exists. It is named in
the import contract that forbids `httpx` everywhere else, so the exemption is
a line in `pyproject.toml` rather than a convention.

`06-OUTBOUND-SECURITY.md` is the boundary document, and its section 5 is the
part worth reading before trusting this: the real protection is a network
egress policy at the deployment, and everything here is defence in depth.
"""

from __future__ import annotations

from agentworld.infra.net.addresses import (
    Address,
    blocking_rule,
    is_blocked,
    network,
    normalise,
    parse_literal,
)
from agentworld.infra.net.errors import OutboundError, OutboundFailure
from agentworld.infra.net.guarded import (
    Destination,
    GuardedClient,
    GuardedResponse,
    Resolver,
    guarded_client,
    system_resolver,
    validate,
)
from agentworld.infra.net.policy import (
    ALLOWED_SCHEMES,
    BLOCKED_HOSTNAMES,
    BLOCKED_IPV4,
    BLOCKED_IPV6,
    DEFAULT_ALLOWED_PORTS,
    EgressPolicy,
)

__all__ = [
    "ALLOWED_SCHEMES",
    "BLOCKED_HOSTNAMES",
    "BLOCKED_IPV4",
    "BLOCKED_IPV6",
    "DEFAULT_ALLOWED_PORTS",
    "Address",
    "Destination",
    "EgressPolicy",
    "GuardedClient",
    "GuardedResponse",
    "OutboundError",
    "OutboundFailure",
    "Resolver",
    "blocking_rule",
    "guarded_client",
    "is_blocked",
    "network",
    "normalise",
    "parse_literal",
    "system_resolver",
    "validate",
]

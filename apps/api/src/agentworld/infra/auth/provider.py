"""The authentication provider interface.

AgentWorld uses Supabase Auth, but nothing above this interface knows that.
The reason is not hypothetical portability: self-hosters routinely already run
an identity provider, and goal G1's "no provider in the core" applies to
identity providers just as much as to model providers. A deployment that
swaps in Auth0, Keycloak or a static test issuer implements this Protocol and
changes nothing else.

What a provider is responsible for: proving the token was issued by the
expected issuer, is in date, and is addressed to this audience. What it is
*not* responsible for: anything about workspaces, roles or the local `users`
row. Authentication answers "who is this"; authorization is
`agentworld.domain.policy` and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """The outcome of a successful verification.

    `subject` is the external identity provider's user id and becomes
    `users.id` verbatim - the mirror row has no id of its own, so a token can
    be resolved to a local user with no extra lookup, and a stale mirror can
    always be rebuilt from the provider.
    """

    subject: UUID
    email: str
    display_name: str | None = None
    avatar_url: str | None = None
    #: The raw verified claims, for diagnostics. Not consulted for decisions:
    #: anything load-bearing gets a field above, so that adding a provider
    #: means filling in known fields rather than guessing at claim names.
    claims: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AuthProvider(Protocol):
    """Verifies a bearer token and reports who presented it."""

    async def verify(self, token: str) -> VerifiedIdentity:
        """Return the identity, or raise `AuthenticationFailedError`.

        Implementations must not distinguish failure modes to the caller: a
        token signed by the wrong key, one that expired and one addressed to
        another audience are all the same 401. The detail that says which is
        for the server's logs.
        """
        ...

    async def close(self) -> None:
        """Release any client or cache held. Called from the app's lifespan."""
        ...

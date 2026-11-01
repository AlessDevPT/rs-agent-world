"""Identity: turning a verified token into a principal.

This is the bridge between authentication (who does the identity provider say
this is) and authorization (what may they do here). It runs on every
authenticated control-plane request, so it is deliberately two queries: upsert
the mirror row, then read the memberships.

`GET /v1/me` is also the registration path. There is no sign-up endpoint
because there is no sign-up to do: Supabase creates the account, and the first
authenticated call to this service materialises the local mirror. A user who
has no workspace yet is not an error state - it is `needs_onboarding`, which
the web app turns into the create-workspace screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentworld.domain.entities import User, WorkspaceMembership
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.auth.provider import VerifiedIdentity
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class Me:
    """The `GET /v1/me` answer."""

    user: User
    workspaces: list[WorkspaceMembership]

    @property
    def needs_onboarding(self) -> bool:
        """True when the user belongs to no workspace.

        A separate field rather than leaving the client to check for an empty
        list: it is the difference between "you have no workspaces" and "the
        list failed to load", and a client that guesses will eventually guess
        wrong on a slow network.
        """
        return not self.workspaces


async def resolve_principal(work: UnitOfWork, identity: VerifiedIdentity) -> UserPrincipal:
    """Mirror the verified identity locally and load what it can see.

    A module function rather than a service method because the authentication
    dependency already has a unit of work open when it needs this, and
    threading a factory through would mean opening a second transaction to do
    two queries.

    The resulting `memberships` map is both the authorization input for
    `domain.policy` and the value written into the row-level security session
    variable, which is why it is resolved once per request rather than per
    query.
    """
    user = await work.users.upsert_from_identity(identity)
    memberships = await work.memberships.visible_to(user.id)
    return UserPrincipal(user_id=user.id, email=user.email, memberships=memberships)


class IdentityService:
    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    async def me(self, principal: UserPrincipal) -> Me:
        async with self._factory.begin() as work:
            user = await work.users.get(principal.user_id)
            if user is None:  # pragma: no cover - authentication guarantees it
                raise RuntimeError("Authenticated principal has no mirror row")
            workspaces = await work.memberships.workspaces_for(principal.user_id)
            return Me(user=user, workspaces=workspaces)

    async def update_profile(
        self,
        principal: UserPrincipal,
        *,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> Me:
        """Update the caller's own profile.

        No authorization call: the only resource a user can reach here is
        themselves, because the id comes from the verified token and not from
        the request. There is no `PATCH /v1/users/{id}` to protect.

        Not audited either - `audit_log` is workspace-scoped, and a profile
        belongs to no workspace. A user changing their own display name is
        also not the kind of act an audit log exists to answer questions
        about.
        """
        async with self._factory.begin() as work:
            await work.users.update_profile(
                principal.user_id, display_name=display_name, avatar_url=avatar_url
            )
        return await self.me(principal)

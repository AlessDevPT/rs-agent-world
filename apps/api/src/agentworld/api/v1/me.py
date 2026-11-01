"""GET and PATCH /v1/me - the identity endpoint, and the registration path.

There is no sign-up route. Supabase Auth creates the account client-side and
AgentWorld never sees a password; the first authenticated `GET /v1/me` is what
materialises the local mirror row. A user who belongs to no workspace is not
an error - `needs_onboarding` says so, and the web app shows the
create-workspace screen.
"""

from __future__ import annotations

from fastapi import APIRouter

from agentworld.api.deps import CurrentUser, IdentityServiceDep
from agentworld.api.schemas.tenancy import MeOut, MePatch
from agentworld.services.identity import Me

router = APIRouter(prefix="/v1", tags=["identity"])


@router.get("/me", response_model=MeOut, summary="The current user and their workspaces")
async def get_me(principal: CurrentUser, identity: IdentityServiceDep) -> MeOut:
    return _to_schema(await identity.me(principal))


@router.patch("/me", response_model=MeOut, summary="Update your own profile")
async def patch_me(body: MePatch, principal: CurrentUser, identity: IdentityServiceDep) -> MeOut:
    updated = await identity.update_profile(
        principal, display_name=body.display_name, avatar_url=body.avatar_url
    )
    return _to_schema(updated)


def _to_schema(me: Me) -> MeOut:
    # `needs_onboarding` is a property on the service's result rather than a
    # field, so it is spelled out here instead of coming along with
    # from_attributes.
    return MeOut.model_validate(
        {
            "user": me.user,
            "workspaces": me.workspaces,
            "needs_onboarding": me.needs_onboarding,
        }
    )

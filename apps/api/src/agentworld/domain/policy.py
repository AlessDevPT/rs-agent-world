"""Authorization: one function, one table.

`docs/01-ARCHITECTURE.md` section 5.4 requires that authorization be a single
call in the service layer rather than role checks scattered across routes. The
reason is auditability: the table below is the whole access-control model, and
a reviewer can read it in one sitting.

Two behaviours worth stating explicitly, because both are easy to get wrong:

1. **No membership means 404, not 403.** Telling a stranger "you lack
   permission on workspace X" confirms that workspace X exists and that they
   guessed its id. `docs/03-API.md` section 2 makes 404 cover both cases.
2. **An API key cannot perform control-plane actions.** The route dependencies
   already keep the planes apart, but this function fails closed anyway, so
   the guarantee does not rest on the routing table staying correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from agentworld.domain.enums import ApiKeyScope, MemberRole
from agentworld.domain.errors import NotFoundError, PermissionDeniedError
from agentworld.domain.principals import KeyPrincipal, Principal, UserPrincipal


class Action(StrEnum):
    """Every authorizable control-plane action.

    Later phases add their own members (agents, tasks, integrations); the
    minimum-role table below must gain a row at the same time, and
    `tests/unit/test_policy.py` fails if it does not.
    """

    WORKSPACE_READ = "workspace.read"
    WORKSPACE_UPDATE = "workspace.update"
    WORKSPACE_DELETE = "workspace.delete"

    MEMBER_READ = "member.read"
    MEMBER_INVITE = "member.invite"
    MEMBER_UPDATE_ROLE = "member.update_role"
    MEMBER_REMOVE = "member.remove"

    PROJECT_READ = "project.read"
    PROJECT_CREATE = "project.create"
    PROJECT_UPDATE = "project.update"
    PROJECT_DELETE = "project.delete"

    API_KEY_READ = "api_key.read"
    API_KEY_CREATE = "api_key.create"
    API_KEY_REVOKE = "api_key.revoke"
    API_KEY_DELETE = "api_key.delete"


#: The access-control model, transcribed from the role table in
#: `docs/03-API.md` section 3.2. Read it as "this action needs at least this
#: role in the resource's workspace".
MINIMUM_ROLE: dict[Action, MemberRole] = {
    Action.WORKSPACE_READ: MemberRole.VIEWER,
    Action.WORKSPACE_UPDATE: MemberRole.ADMIN,
    Action.WORKSPACE_DELETE: MemberRole.OWNER,
    Action.MEMBER_READ: MemberRole.VIEWER,
    Action.MEMBER_INVITE: MemberRole.ADMIN,
    Action.MEMBER_UPDATE_ROLE: MemberRole.ADMIN,
    Action.MEMBER_REMOVE: MemberRole.ADMIN,
    Action.PROJECT_READ: MemberRole.VIEWER,
    Action.PROJECT_CREATE: MemberRole.MEMBER,
    Action.PROJECT_UPDATE: MemberRole.MEMBER,
    Action.PROJECT_DELETE: MemberRole.ADMIN,
    Action.API_KEY_READ: MemberRole.ADMIN,
    Action.API_KEY_CREATE: MemberRole.ADMIN,
    Action.API_KEY_REVOKE: MemberRole.ADMIN,
    Action.API_KEY_DELETE: MemberRole.ADMIN,
}

#: Actions a member may always perform on themselves regardless of role - a
#: viewer can leave a workspace without needing an admin to evict them.
SELF_SERVICE_ACTIONS: frozenset[Action] = frozenset({Action.MEMBER_REMOVE})


@dataclass(frozen=True, slots=True)
class Resource:
    """What an action is being performed on.

    Only the tenant discriminator and, where a rule says "or self", the user
    the action is aimed at. Deliberately not the entity itself: a policy that
    can read entity fields grows conditions that no table can summarise.
    """

    workspace_id: UUID
    subject_user_id: UUID | None = None


def can(principal: Principal, action: Action, resource: Resource) -> bool:
    """Whether `principal` may perform `action`. Never raises."""
    try:
        authorize(principal, action, resource)
    except (NotFoundError, PermissionDeniedError):
        return False
    return True


def authorize(principal: Principal, action: Action, resource: Resource) -> MemberRole:
    """Assert `principal` may perform `action`, returning their effective role.

    Raises `NotFoundError` when the workspace is not visible to the principal
    and `PermissionDeniedError` when it is visible but the role is too weak.
    """
    if isinstance(principal, KeyPrincipal):
        # A key is scoped to a project and a plane. Control-plane management is
        # categorically outside what any key may do, including a `full` key:
        # keys that can mint keys turn one leak into permanent access.
        raise PermissionDeniedError(
            f"An API key may not perform control-plane action '{action.value}'",
            api_key_id=str(principal.api_key_id),
            scope=principal.scope.value,
        )

    role = principal.role_in(resource.workspace_id)
    if role is None:
        raise NotFoundError(
            "Workspace not found",
            workspace_id=str(resource.workspace_id),
            user_id=str(principal.user_id),
        )

    if action in SELF_SERVICE_ACTIONS and resource.subject_user_id == principal.user_id:
        return role

    required = MINIMUM_ROLE[action]
    if not role.at_least(required):
        raise PermissionDeniedError(
            f"'{action.value}' requires the '{required.value}' role; you have '{role.value}'",
            action=action.value,
            required_role=required.value,
            actual_role=role.value,
        )
    return role


def authorize_key_scope(
    principal: KeyPrincipal, *, required: ApiKeyScope, workspace_id: UUID
) -> None:
    """Assert an API key is in the right workspace and carries the right scope.

    `full` satisfies both `ingest` and `control`; the two narrow scopes do not
    satisfy each other. A key presented against the wrong workspace is a 404,
    for the same reason a foreign workspace id is.
    """
    if principal.workspace_id != workspace_id:
        raise NotFoundError("Resource not found", workspace_id=str(workspace_id))
    if principal.scope is ApiKeyScope.FULL or principal.scope is required:
        return
    raise PermissionDeniedError(
        f"This key's scope is '{principal.scope.value}'; '{required.value}' is required",
        required_scope=required.value,
        actual_scope=principal.scope.value,
    )


def assert_user(principal: Principal) -> UserPrincipal:
    """Narrow a principal to a user, or refuse.

    Used where a signature cannot be narrowed at the type level, such as a
    FastAPI dependency shared by both planes.
    """
    if not isinstance(principal, UserPrincipal):
        raise PermissionDeniedError("This endpoint requires a user credential, not an API key")
    return principal

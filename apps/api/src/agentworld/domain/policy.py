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
from agentworld.domain.principals import (
    KeyPrincipal,
    Principal,
    SystemPrincipal,
    UserPrincipal,
)


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

    # ---- Phase 2 ----------------------------------------------------------
    #
    # Split rather than bundled, wherever one action would have carried a
    # harmless capability together with a dangerous one. `agent.update` is
    # everything a builder edits; `agent.configure` is the narrow remainder -
    # model profile, overrides and dispatch - that spends money and reaches
    # outside. `integration.read` says a Slack connection exists;
    # `integration.read_config` says which credential it uses.

    AGENT_READ = "agent.read"
    AGENT_CREATE = "agent.create"
    AGENT_UPDATE = "agent.update"
    AGENT_CONFIGURE = "agent.configure"
    AGENT_ARCHIVE = "agent.archive"
    #: The inverse of `agent.archive`, and `admin` for the same reason: a
    #: restored managed agent can be dispatched again, so bringing one back
    #: is as privileged as putting it away.
    AGENT_RESTORE = "agent.restore"
    AGENT_PAUSE = "agent.pause"
    AGENT_TEST = "agent.test"
    AGENT_READ_RUNS = "agent.read_runs"

    MODEL_PROFILE_READ = "model_profile.read"
    MODEL_PROFILE_CREATE = "model_profile.create"
    MODEL_PROFILE_UPDATE = "model_profile.update"
    MODEL_PROFILE_DELETE = "model_profile.delete"

    WORKFLOW_READ = "workflow.read"
    WORKFLOW_CREATE = "workflow.create"
    #: The name and description. Distinct from `WORKFLOW_UPDATE_DRAFT`, which
    #: is about the graph: renaming a workflow changes what it is called and
    #: nothing about what it would do, so the two are not one permission even
    #: though both are `member`.
    WORKFLOW_UPDATE = "workflow.update"
    WORKFLOW_UPDATE_DRAFT = "workflow.update_draft"
    WORKFLOW_VALIDATE = "workflow.validate"
    WORKFLOW_PUBLISH = "workflow.publish"
    WORKFLOW_ARCHIVE = "workflow.archive"
    WORKFLOW_RESTORE = "workflow.restore"
    WORKFLOW_RUN = "workflow.run"

    EXECUTION_READ = "execution.read"
    EXECUTION_CANCEL = "execution.cancel"
    EXECUTION_PAUSE_RESUME = "execution.pause_resume"
    EXECUTION_RETRY_STEP = "execution.retry_step"
    #: Resolving a dispatch whose outcome nobody knows. Not a retry: the
    #: person is asserting what happened to an effect this system could not
    #: observe, which is why it is admin rather than member.
    DISPATCH_RESOLVE_REVIEW = "dispatch.resolve_review"
    EXECUTION_PROVIDE_INPUT = "execution.provide_input"

    APPROVAL_READ = "approval.read"
    APPROVAL_DECIDE = "approval.decide"
    APPROVAL_POLICY_READ = "approval_policy.read"
    APPROVAL_POLICY_UPDATE = "approval_policy.update"

    CONNECTION_READ = "connection.read"
    CONNECTION_CREATE = "connection.create"
    CONNECTION_UPDATE = "connection.update"
    CONNECTION_DELETE = "connection.delete"

    TOOL_READ = "tool.read"
    TOOL_TRY = "tool.try"
    TOOL_REFRESH = "tool.refresh"

    PROVIDER_READ = "provider.read"

    INTEGRATION_READ = "integration.read"
    INTEGRATION_READ_CONFIG = "integration.read_config"
    INTEGRATION_CREATE = "integration.create"
    INTEGRATION_UPDATE = "integration.update"
    INTEGRATION_DELETE = "integration.delete"
    INTEGRATION_CONNECT = "integration.connect"
    INTEGRATION_TEST = "integration.test"

    CREDENTIAL_READ_METADATA = "credential.read_metadata"
    CREDENTIAL_CREATE = "credential.create"
    CREDENTIAL_ROTATE = "credential.rotate"
    CREDENTIAL_REVOKE = "credential.revoke"
    CREDENTIAL_DELETE = "credential.delete"

    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_CREATE = "knowledge.create"
    KNOWLEDGE_UPLOAD = "knowledge.upload"
    KNOWLEDGE_DELETE = "knowledge.delete"

    TRIGGER_READ = "trigger.read"
    TRIGGER_CREATE = "trigger.create"
    TRIGGER_UPDATE = "trigger.update"
    TRIGGER_DELETE = "trigger.delete"
    TRIGGER_ENABLE = "trigger.enable"
    TRIGGER_ROTATE_TOKEN = "trigger.rotate_token"

    WORLD_READ = "world.read"
    WORLD_UPDATE = "world.update"

    BUILDER_USE = "builder.use"
    BUILDER_APPLY = "builder.apply"


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
    # ---- Phase 2 ----------------------------------------------------------
    #
    # The default role is the weakest that keeps the product usable: a
    # beginner building their first ecosystem is a `member` and needs an
    # admin for nothing in the happy path. Everything above `member` spends
    # money unexpectedly, widens a permission, acts autonomously, or reveals
    # a secret - and each one below that is `admin` says which.
    Action.AGENT_READ: MemberRole.VIEWER,
    Action.AGENT_CREATE: MemberRole.MEMBER,
    Action.AGENT_UPDATE: MemberRole.MEMBER,
    # Model profile, overrides and dispatch: spends money and reaches out.
    Action.AGENT_CONFIGURE: MemberRole.ADMIN,
    Action.AGENT_ARCHIVE: MemberRole.ADMIN,
    Action.AGENT_RESTORE: MemberRole.ADMIN,
    Action.AGENT_PAUSE: MemberRole.MEMBER,
    Action.AGENT_TEST: MemberRole.MEMBER,
    Action.AGENT_READ_RUNS: MemberRole.VIEWER,
    Action.MODEL_PROFILE_READ: MemberRole.VIEWER,
    # A model profile names a credential and a spend ceiling.
    Action.MODEL_PROFILE_CREATE: MemberRole.ADMIN,
    Action.MODEL_PROFILE_UPDATE: MemberRole.ADMIN,
    Action.MODEL_PROFILE_DELETE: MemberRole.ADMIN,
    Action.WORKFLOW_READ: MemberRole.VIEWER,
    Action.WORKFLOW_CREATE: MemberRole.MEMBER,
    Action.WORKFLOW_UPDATE: MemberRole.MEMBER,
    Action.WORKFLOW_UPDATE_DRAFT: MemberRole.MEMBER,
    Action.WORKFLOW_VALIDATE: MemberRole.MEMBER,
    # Publishing and running are `member` because a workflow is only as
    # dangerous as the grants its steps already hold, and those were
    # authorized separately. Running it *autonomously* is `trigger.enable`.
    Action.WORKFLOW_PUBLISH: MemberRole.MEMBER,
    Action.WORKFLOW_ARCHIVE: MemberRole.ADMIN,
    Action.WORKFLOW_RESTORE: MemberRole.ADMIN,
    Action.WORKFLOW_RUN: MemberRole.MEMBER,
    Action.EXECUTION_READ: MemberRole.VIEWER,
    Action.EXECUTION_CANCEL: MemberRole.MEMBER,
    Action.EXECUTION_PAUSE_RESUME: MemberRole.MEMBER,
    Action.EXECUTION_RETRY_STEP: MemberRole.MEMBER,
    Action.DISPATCH_RESOLVE_REVIEW: MemberRole.ADMIN,
    Action.EXECUTION_PROVIDE_INPUT: MemberRole.MEMBER,
    Action.APPROVAL_READ: MemberRole.VIEWER,
    # The floor, not the answer. An approval carries its own
    # `required_role`, frozen when it was created, and that may only be
    # stricter - see `approval_floor` below.
    Action.APPROVAL_DECIDE: MemberRole.MEMBER,
    Action.APPROVAL_POLICY_READ: MemberRole.VIEWER,
    # Loosening a gate is a permissions change.
    Action.APPROVAL_POLICY_UPDATE: MemberRole.ADMIN,
    Action.CONNECTION_READ: MemberRole.VIEWER,
    # `member`, unless the connection grants a consequential tool - see
    # ELEVATED_ROLE.
    Action.CONNECTION_CREATE: MemberRole.MEMBER,
    Action.CONNECTION_UPDATE: MemberRole.MEMBER,
    Action.CONNECTION_DELETE: MemberRole.MEMBER,
    Action.TOOL_READ: MemberRole.VIEWER,
    Action.TOOL_TRY: MemberRole.MEMBER,
    Action.TOOL_REFRESH: MemberRole.ADMIN,
    Action.PROVIDER_READ: MemberRole.VIEWER,
    # `member`, not `viewer`: that a Slack connection exists is operational
    # information a member needs, and a viewer has no reason to enumerate
    # which external systems a company has wired up.
    Action.INTEGRATION_READ: MemberRole.MEMBER,
    # Endpoints, scopes, and which credential.
    Action.INTEGRATION_READ_CONFIG: MemberRole.ADMIN,
    Action.INTEGRATION_CREATE: MemberRole.ADMIN,
    Action.INTEGRATION_UPDATE: MemberRole.ADMIN,
    Action.INTEGRATION_DELETE: MemberRole.ADMIN,
    Action.INTEGRATION_CONNECT: MemberRole.ADMIN,
    Action.INTEGRATION_TEST: MemberRole.ADMIN,
    # Every credential action is admin. There is no viewer row and no member
    # row, and `credential.read_secret` does not exist at any role.
    Action.CREDENTIAL_READ_METADATA: MemberRole.ADMIN,
    Action.CREDENTIAL_CREATE: MemberRole.ADMIN,
    Action.CREDENTIAL_ROTATE: MemberRole.ADMIN,
    Action.CREDENTIAL_REVOKE: MemberRole.ADMIN,
    Action.CREDENTIAL_DELETE: MemberRole.ADMIN,
    Action.KNOWLEDGE_READ: MemberRole.VIEWER,
    Action.KNOWLEDGE_CREATE: MemberRole.MEMBER,
    Action.KNOWLEDGE_UPLOAD: MemberRole.MEMBER,
    Action.KNOWLEDGE_DELETE: MemberRole.MEMBER,
    Action.TRIGGER_READ: MemberRole.VIEWER,
    Action.TRIGGER_CREATE: MemberRole.MEMBER,
    Action.TRIGGER_UPDATE: MemberRole.MEMBER,
    Action.TRIGGER_DELETE: MemberRole.MEMBER,
    # Creating a trigger is drafting; enabling it is deciding that work may
    # happen with nobody watching.
    Action.TRIGGER_ENABLE: MemberRole.ADMIN,
    Action.TRIGGER_ROTATE_TOKEN: MemberRole.ADMIN,
    Action.WORLD_READ: MemberRole.VIEWER,
    # Position, size, appearance, district and route - and nothing
    # operational. That is the payoff of keeping visual state in its own
    # tables: dragging a building needs the weakest role that can change
    # anything at all.
    Action.WORLD_UPDATE: MemberRole.MEMBER,
    Action.BUILDER_USE: MemberRole.MEMBER,
    # The floor. `builder.apply` additionally requires every action in the
    # plan it is applying, which the builder service checks one by one.
    Action.BUILDER_APPLY: MemberRole.MEMBER,
}


class Condition(StrEnum):
    """A fact about what is being acted on that can raise the bar.

    A closed vocabulary, deliberately. The alternative - letting `Resource`
    carry the entity so the policy can read its fields - is how an
    access-control model stops being a table anyone can review. Each member
    here is a yes/no the *caller* establishes and passes in, and there are
    two of them.
    """

    #: The connection being created or updated grants a tool whose effects
    #: reach outside AgentWorld and cannot be taken back.
    GRANTS_CONSEQUENTIAL_TOOL = "grants_consequential_tool"


#: Where a condition raises the required role above the table's baseline.
#:
#: Granting a capability is at least as privileged as using it: a `member`
#: may call a tool an agent already holds, but wiring an agent to one that
#: sends email or moves money is an `admin` decision.
ELEVATED_ROLE: dict[tuple[Action, Condition], MemberRole] = {
    (Action.CONNECTION_CREATE, Condition.GRANTS_CONSEQUENTIAL_TOOL): MemberRole.ADMIN,
    (Action.CONNECTION_UPDATE, Condition.GRANTS_CONSEQUENTIAL_TOOL): MemberRole.ADMIN,
}

#: Actions that expose or modify secrets, permissions, spend, or behaviour
#: visible outside AgentWorld.
#:
#: Not decoration: this drives which actions appear in the audit digest,
#: which prompt for re-authentication, and which the conversational builder
#: must obtain explicit consent for before including in a plan. Keeping it
#: beside the role table is what stops the three drifting apart.
SENSITIVE_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.WORKSPACE_DELETE,
        Action.MEMBER_INVITE,
        Action.MEMBER_UPDATE_ROLE,
        Action.MEMBER_REMOVE,
        Action.PROJECT_DELETE,
        Action.API_KEY_READ,
        Action.API_KEY_CREATE,
        Action.API_KEY_REVOKE,
        Action.API_KEY_DELETE,
        Action.AGENT_CONFIGURE,
        Action.AGENT_TEST,
        Action.MODEL_PROFILE_CREATE,
        Action.MODEL_PROFILE_UPDATE,
        Action.WORKFLOW_PUBLISH,
        Action.WORKFLOW_RUN,
        Action.EXECUTION_RETRY_STEP,
        Action.DISPATCH_RESOLVE_REVIEW,
        Action.APPROVAL_DECIDE,
        Action.APPROVAL_POLICY_UPDATE,
        Action.CONNECTION_CREATE,
        Action.CONNECTION_UPDATE,
        Action.TOOL_TRY,
        Action.INTEGRATION_READ_CONFIG,
        Action.INTEGRATION_CREATE,
        Action.INTEGRATION_UPDATE,
        Action.INTEGRATION_DELETE,
        Action.INTEGRATION_CONNECT,
        Action.INTEGRATION_TEST,
        Action.CREDENTIAL_READ_METADATA,
        Action.CREDENTIAL_CREATE,
        Action.CREDENTIAL_ROTATE,
        Action.CREDENTIAL_REVOKE,
        Action.CREDENTIAL_DELETE,
        Action.TRIGGER_ENABLE,
        Action.TRIGGER_ROTATE_TOKEN,
        Action.BUILDER_APPLY,
    }
)
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
    #: Facts the caller establishes that can raise the required role. See
    #: `Condition`, and `ELEVATED_ROLE` for where they apply.
    conditions: frozenset[Condition] = frozenset()


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
    if isinstance(principal, SystemPrincipal):
        # The system acts on the authority of a published workflow version,
        # which is not a role and never becomes one. Every action in this
        # table is a human changing the ecosystem, so there is no member of
        # it a scheduled run may perform - including the read actions, since
        # reading on nobody's behalf has no meaning here.
        raise PermissionDeniedError(
            f"The system principal may not perform control-plane action '{action.value}'",
            initiator_label=principal.label,
            workflow_version_id=str(principal.workflow_version_id),
        )

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

    required = required_role(action, resource.conditions)
    if not role.at_least(required):
        raise PermissionDeniedError(
            f"'{action.value}' requires the '{required.value}' role; you have '{role.value}'",
            action=action.value,
            required_role=required.value,
            actual_role=role.value,
        )
    return role


def required_role(action: Action, conditions: frozenset[Condition] = frozenset()) -> MemberRole:
    """The role `action` needs, once its conditions are taken into account.

    The strictest applicable row wins. Separate from `authorize` so the UI
    can ask "would this be allowed?" without constructing a principal, and so
    the elevation rules are testable on their own.
    """
    required = MINIMUM_ROLE[action]
    for condition in conditions:
        if (elevated := ELEVATED_ROLE.get((action, condition))) and elevated.at_least(required):
            required = elevated
    return required


def approval_floor(action: Action, required_by_policy: MemberRole) -> MemberRole:
    """The role an approver needs, which a policy may raise and never lower.

    Two hard floors, neither of which is in the role table and neither of
    which a policy can reach under. An approver's role may not fall below the
    role required to perform the action being approved - otherwise approval
    would be a way to launder a permission nobody holds. And a policy
    produces a *requirement*, never a capability: `authorize` has already run
    independently by the time this is consulted.
    """
    baseline = MINIMUM_ROLE[action]
    return required_by_policy if required_by_policy.at_least(baseline) else baseline


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

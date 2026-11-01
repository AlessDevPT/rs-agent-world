"""The authorization policy.

`docs/05-ROADMAP.md` asks Phase 1 for a role matrix covering every endpoint by
every role. Half of that lives here, as a pure matrix over every action by
every role with no HTTP and no database, which is where the matrix is cheap
enough to be exhaustive. The other half - that each route actually asks for
the action it should - is in `tests/integration/test_role_matrix.py`.
"""

from __future__ import annotations

import uuid

import pytest

from agentworld.domain.enums import ApiKeyScope, MemberRole
from agentworld.domain.errors import NotFoundError, PermissionDeniedError
from agentworld.domain.policy import (
    MINIMUM_ROLE,
    SELF_SERVICE_ACTIONS,
    SENSITIVE_ACTIONS,
    Action,
    Condition,
    Resource,
    approval_floor,
    assert_user,
    authorize,
    authorize_key_scope,
    can,
    required_role,
)
from agentworld.domain.principals import KeyPrincipal, SystemPrincipal, UserPrincipal
from agentworld.services.context import actor_of

WORKSPACE = uuid.uuid4()
OTHER_WORKSPACE = uuid.uuid4()
USER = uuid.uuid4()
OTHER_USER = uuid.uuid4()


def user_with(role: MemberRole | None, *, user_id: uuid.UUID = USER) -> UserPrincipal:
    memberships = {} if role is None else {WORKSPACE: role}
    return UserPrincipal(user_id=user_id, email="a@example.com", memberships=memberships)


def test_every_action_has_a_minimum_role() -> None:
    """A new action without a row in the table would authorize by KeyError.

    This is the test that makes the policy table authoritative rather than
    merely conventional: `authorize` indexes `MINIMUM_ROLE[action]`, so an
    action added to the enum and forgotten here would raise `KeyError` at
    runtime - a 500, not a 403.
    """
    missing = [action for action in Action if action not in MINIMUM_ROLE]
    assert missing == []


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("role", list(MemberRole))
def test_role_matrix(action: Action, role: MemberRole) -> None:
    """Every action by every role, against the table in docs/03-API.md 3.2."""
    principal = user_with(role)
    expected = role.at_least(MINIMUM_ROLE[action])
    assert can(principal, action, Resource(WORKSPACE)) is expected


def test_the_matrix_is_not_trivially_true() -> None:
    """Guard against a matrix that happens to allow (or deny) everything.

    A role table where every cell agreed would make the test above pass while
    testing nothing, so assert that both outcomes actually occur.
    """
    outcomes = {
        can(user_with(role), action, Resource(WORKSPACE))
        for role in MemberRole
        for action in Action
    }
    assert outcomes == {True, False}


@pytest.mark.parametrize("action", list(Action))
def test_a_non_member_gets_not_found_rather_than_forbidden(action: Action) -> None:
    """Existence is information, so it is withheld.

    `docs/03-API.md` section 2 makes 404 cover "not found" and "not visible to
    this tenant" alike. A 403 here would confirm to a stranger that the
    workspace exists and that they guessed its id.
    """
    with pytest.raises(NotFoundError):
        authorize(user_with(None), action, Resource(WORKSPACE))


def test_a_member_of_another_workspace_is_also_not_found() -> None:
    principal = UserPrincipal(
        user_id=USER, email="a@example.com", memberships={OTHER_WORKSPACE: MemberRole.OWNER}
    )
    with pytest.raises(NotFoundError):
        authorize(principal, Action.WORKSPACE_READ, Resource(WORKSPACE))


def test_insufficient_role_is_forbidden_not_not_found() -> None:
    """Once membership is established, the distinction is safe to make.

    A viewer already knows the workspace exists, so 403 leaks nothing and is
    far more useful than a 404 that would read as "your workspace vanished".
    """
    with pytest.raises(PermissionDeniedError) as caught:
        authorize(user_with(MemberRole.VIEWER), Action.PROJECT_CREATE, Resource(WORKSPACE))
    # The message names what is required, which is actionable and not secret.
    assert "member" in str(caught.value)


def test_authorize_returns_the_effective_role() -> None:
    assert (
        authorize(user_with(MemberRole.ADMIN), Action.PROJECT_READ, Resource(WORKSPACE))
        is MemberRole.ADMIN
    )


# ---------------------------------------------------------------- self-service ---


def test_a_viewer_may_remove_themselves() -> None:
    """Leaving a workspace must not require an admin's help."""
    principal = user_with(MemberRole.VIEWER)
    assert can(principal, Action.MEMBER_REMOVE, Resource(WORKSPACE, subject_user_id=USER))


def test_a_viewer_may_not_remove_someone_else() -> None:
    principal = user_with(MemberRole.VIEWER)
    assert not can(principal, Action.MEMBER_REMOVE, Resource(WORKSPACE, subject_user_id=OTHER_USER))


def test_self_service_does_not_leak_into_other_actions() -> None:
    """Aiming an action at yourself must not become a general bypass.

    Only `MEMBER_REMOVE` is self-service. If the check were on the subject
    alone, a viewer could pass their own id to any action and be allowed.
    """
    principal = user_with(MemberRole.VIEWER)
    for action in Action:
        if action in SELF_SERVICE_ACTIONS:
            continue
        allowed = can(principal, action, Resource(WORKSPACE, subject_user_id=USER))
        assert allowed is MemberRole.VIEWER.at_least(MINIMUM_ROLE[action])


def test_self_service_still_requires_membership() -> None:
    with pytest.raises(NotFoundError):
        authorize(user_with(None), Action.MEMBER_REMOVE, Resource(WORKSPACE, subject_user_id=USER))


# ----------------------------------------------------------------- key plane ---


def key(scope: ApiKeyScope = ApiKeyScope.INGEST) -> KeyPrincipal:
    return KeyPrincipal(
        api_key_id=uuid.uuid4(), workspace_id=WORKSPACE, project_id=uuid.uuid4(), scope=scope
    )


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("scope", list(ApiKeyScope))
def test_no_api_key_may_perform_any_control_action(action: Action, scope: ApiKeyScope) -> None:
    """Including a `full` key, and including read-only actions.

    A key that can mint keys turns one leak into permanent access, so key
    management is categorically outside what a key may do. The route
    dependencies keep the planes apart already; this asserts the policy fails
    closed regardless of how a route is wired.
    """
    with pytest.raises(PermissionDeniedError):
        authorize(key(scope), action, Resource(WORKSPACE))


def test_full_scope_satisfies_both_narrow_scopes() -> None:
    authorize_key_scope(key(ApiKeyScope.FULL), required=ApiKeyScope.INGEST, workspace_id=WORKSPACE)
    authorize_key_scope(key(ApiKeyScope.FULL), required=ApiKeyScope.CONTROL, workspace_id=WORKSPACE)


def test_narrow_scopes_do_not_satisfy_each_other() -> None:
    with pytest.raises(PermissionDeniedError):
        authorize_key_scope(
            key(ApiKeyScope.INGEST), required=ApiKeyScope.CONTROL, workspace_id=WORKSPACE
        )
    with pytest.raises(PermissionDeniedError):
        authorize_key_scope(
            key(ApiKeyScope.CONTROL), required=ApiKeyScope.INGEST, workspace_id=WORKSPACE
        )


def test_a_key_against_a_foreign_workspace_is_not_found() -> None:
    """Not 403: the same reasoning as for a user, for the same reason."""
    with pytest.raises(NotFoundError):
        authorize_key_scope(
            key(ApiKeyScope.FULL), required=ApiKeyScope.INGEST, workspace_id=OTHER_WORKSPACE
        )


def test_assert_user_rejects_a_key() -> None:
    with pytest.raises(PermissionDeniedError):
        assert_user(key())
    principal = user_with(MemberRole.MEMBER)
    assert assert_user(principal) is principal


# ------------------------------------------------------------------- ranking ---


def test_roles_rank_from_viewer_to_owner() -> None:
    """`at_least` depends on this order, so an inserted role must be ranked."""
    ranks = [
        role.rank
        for role in (MemberRole.VIEWER, MemberRole.MEMBER, MemberRole.ADMIN, MemberRole.OWNER)
    ]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(MemberRole), "every role needs a distinct rank"


def test_every_role_is_ranked() -> None:
    for role in MemberRole:
        assert isinstance(role.rank, int)


# ------------------------------------------------- the system principal ---


def system() -> SystemPrincipal:
    return SystemPrincipal(
        workspace_id=WORKSPACE,
        project_id=uuid.uuid4(),
        workflow_version_id=uuid.uuid4(),
        label="nightly competitor brief",
        trigger_id=uuid.uuid4(),
    )


@pytest.mark.parametrize("action", list(Action))
def test_the_system_principal_may_perform_no_control_action(action: Action) -> None:
    """Exhaustive over every action, for the same reason the key test is.

    A scheduled run acts on the authority of a frozen workflow version, which
    is not a role and never becomes one. There is no member of this enum a
    trigger firing at 07:00 may perform - including the read actions, because
    reading on nobody's behalf has no meaning in a table whose rows are all
    "a human changes the ecosystem".
    """
    with pytest.raises(PermissionDeniedError):
        authorize(system(), action, Resource(WORKSPACE))


def test_the_refusal_names_the_version_rather_than_a_person() -> None:
    """Which is the whole argument for the type.

    If the error named whoever enabled the trigger, that person would be
    accountable for work they did not do - and enabling a trigger would have
    become a way to lend out a permission.
    """
    with pytest.raises(PermissionDeniedError) as caught:
        authorize(system(), Action.AGENT_CREATE, Resource(WORKSPACE))
    assert "workflow_version_id" in caught.value.context
    assert "user_id" not in caught.value.context


def test_a_system_run_is_not_a_member_of_anything() -> None:
    """It carries a workspace, so the temptation is to treat it as a member."""
    assert not can(system(), Action.WORKFLOW_READ, Resource(WORKSPACE))


def test_the_system_actor_has_no_id_to_record() -> None:
    """There is no user to name, and naming one would be a lie."""
    assert actor_of(system()) == ("system", None)


# ------------------------------------------------ conditional elevation ---


def test_a_connection_is_a_member_action_until_it_grants_a_real_tool() -> None:
    """Granting a capability is at least as privileged as using it."""
    plain = Resource(WORKSPACE)
    consequential = Resource(WORKSPACE, conditions=frozenset({Condition.GRANTS_CONSEQUENTIAL_TOOL}))

    assert can(user_with(MemberRole.MEMBER), Action.CONNECTION_CREATE, plain)
    assert not can(user_with(MemberRole.MEMBER), Action.CONNECTION_CREATE, consequential)
    assert can(user_with(MemberRole.ADMIN), Action.CONNECTION_CREATE, consequential)


def test_an_irrelevant_condition_changes_nothing() -> None:
    """Elevation is keyed on the pair, so a flag cannot raise the bar for an
    action nobody wrote a rule for."""
    flagged = Resource(WORKSPACE, conditions=frozenset({Condition.GRANTS_CONSEQUENTIAL_TOOL}))
    assert required_role(Action.WORLD_UPDATE, flagged.conditions) is MemberRole.MEMBER


@pytest.mark.parametrize("action", list(Action))
def test_no_condition_ever_lowers_the_required_role(action: Action) -> None:
    """An elevation that weakened a rule would be a privilege escalation
    dressed as a refinement."""
    everything = frozenset(Condition)
    assert required_role(action, everything).at_least(MINIMUM_ROLE[action])


# -------------------------------------------------------- approval floor ---


def test_a_policy_may_raise_the_approver_bar() -> None:
    """A workflow that sends money can require an admin to approve it."""
    assert approval_floor(Action.WORKFLOW_RUN, MemberRole.ADMIN) is MemberRole.ADMIN


def test_a_policy_may_not_lower_it_past_the_underlying_action() -> None:
    """Otherwise approval would launder a permission nobody holds: a viewer
    could wave through work they could not perform themselves."""
    assert approval_floor(Action.WORKFLOW_RUN, MemberRole.VIEWER) is MemberRole.MEMBER
    assert approval_floor(Action.CREDENTIAL_ROTATE, MemberRole.MEMBER) is MemberRole.ADMIN


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("policy_role", list(MemberRole))
def test_an_approval_floor_is_never_below_the_action_itself(
    action: Action, policy_role: MemberRole
) -> None:
    """Exhaustive, because this is the rule a configuration mistake would
    otherwise quietly defeat."""
    assert approval_floor(action, policy_role).at_least(MINIMUM_ROLE[action])


# ----------------------------------------------------------- the counts ---


def test_the_matrix_is_the_size_the_proposal_says_it_is() -> None:
    """77 actions, 62 of them new in Phase 2.

    A count is a weak test of anything except itself - but this one is quoted
    in `05-PERMISSIONS.md` and in the decision record, and a silent drift
    between the document a reviewer reads and the table the code enforces is
    exactly what an access-control model cannot afford.
    """
    phase_one = {
        action
        for action in Action
        if action.value.split(".", 1)[0] in {"workspace", "member", "project", "api_key"}
    }
    assert len(phase_one) == 15
    assert len(Action) == 77
    assert len(Action) - len(phase_one) == 62


def test_every_sensitive_action_exists_and_is_not_merely_a_read() -> None:
    """The column drives the audit digest and the builder's consent prompts,
    so a typo here is a prompt that never appears."""
    assert set(Action) >= SENSITIVE_ACTIONS
    harmless = {Action.WORKSPACE_READ, Action.AGENT_READ, Action.WORLD_READ}
    assert SENSITIVE_ACTIONS.isdisjoint(harmless)


def test_the_dangerous_actions_are_all_marked_sensitive() -> None:
    """Spot-checked against the categories the proposal names: secrets,
    permissions, spend, and anything externally visible."""
    for action in Action:
        if action.value.startswith(("credential.", "integration.")):
            if action is Action.INTEGRATION_READ:
                continue  # that a Slack connection exists is not a secret
            assert action in SENSITIVE_ACTIONS, action


def test_no_action_can_read_a_secret() -> None:
    """`credential.read_secret` does not exist at any role, which is what
    makes "no role can read credential material" true rather than policy."""
    assert not any("read_secret" in action.value for action in Action)
    assert not any(action.value == "agent.set_status" for action in Action)
    assert not any(action.value == "execution.edit" for action in Action)

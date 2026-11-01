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
    Action,
    Resource,
    assert_user,
    authorize,
    authorize_key_scope,
    can,
)
from agentworld.domain.principals import KeyPrincipal, UserPrincipal

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

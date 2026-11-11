"""Editing a model profile: the key it names, the model it calls, the default.

Stage B2c. A profile is the one row in this product that points at a
credential, so most of this file is about that pointer: an id from another
workspace, one whose material has been destroyed, one that is revoked but
still real, one scoped to a project. The rest is the ordinary PATCH
contract - absent is not null, unknown fields are refused by name, the
duplicate-name check does not count the row being renamed - and the two
concurrency facts the partial unique index is responsible for.

`admin` throughout, unchanged from `create`: this endpoint adds a verb, not
a rule. No test here reads credential material, because there is no field
on any entity or view through which it could.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agentworld.domain.enums import MemberRole
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.definitions import ModelProfileService
from helpers import Actor, seed_membership, seed_user

pytestmark = pytest.mark.integration

SECRET = "sk-live-do-not-log-me-0f9f3c"


class Workspace:
    """A workspace with one project and one model profile."""

    def __init__(self, client: TestClient, actor: Actor, name: str = "Profiles") -> None:
        self.client = client
        self.actor = actor
        created = client.post("/v1/workspaces", json={"name": name}, headers=actor.headers)
        assert created.status_code == 201, created.text
        self.workspace = created.json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()
        self.profile = self.make_profile("Default")

    @property
    def id(self) -> str:
        return str(self.workspace["id"])

    @property
    def project_id(self) -> str:
        return str(self.project["id"])

    @property
    def profile_id(self) -> str:
        return str(self.profile["id"])

    def make_profile(self, name: str, **overrides: Any) -> Any:
        body = {"name": name, "provider_slug": "anthropic", "model_id": "claude-opus-5"}
        body.update(overrides)
        response = self.client.post(
            f"/v1/workspaces/{self.id}/model-profiles", json=body, headers=self.actor.headers
        )
        assert response.status_code == 201, response.text
        return response.json()

    def make_credential(self, name: str, **overrides: Any) -> Any:
        body: dict[str, Any] = {
            "name": name,
            "kind": "api_key",
            "provider_slug": "anthropic",
            "secret": SECRET,
        }
        body.update(overrides)
        response = self.client.post(
            f"/v1/workspaces/{self.id}/credentials", json=body, headers=self.actor.headers
        )
        assert response.status_code == 201, response.text
        return response.json()

    def patch(self, body: Any, profile_id: str | None = None, actor: Actor | None = None) -> Any:
        return self.client.patch(
            f"/v1/workspaces/{self.id}/model-profiles/{profile_id or self.profile_id}",
            json=body,
            headers=(actor or self.actor).headers,
        )

    def listed(self) -> list[Any]:
        return list(
            self.client.get(
                f"/v1/workspaces/{self.id}/model-profiles", headers=self.actor.headers
            ).json()
        )


@pytest.fixture
def workspace(client: TestClient, actor: Actor) -> Workspace:
    return Workspace(client, actor)


async def _defaults(owner_database: Database, workspace_id: str) -> list[str]:
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id FROM model_profiles "
                    "WHERE workspace_id = :w AND is_default ORDER BY name"
                ),
                {"w": uuid.UUID(workspace_id)},
            )
        ).all()
    return [str(row.id) for row in rows]


async def _audit(owner_database: Database, workspace_id: str) -> list[Any]:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT resource_id, metadata FROM audit_log WHERE workspace_id = :w "
                    "AND action = 'model_profile.update' ORDER BY created_at, id"
                ),
                {"w": uuid.UUID(workspace_id)},
            )
        ).all()


# ------------------------------------------------------------- the editable ---


def test_every_editable_field_can_be_changed(workspace: Workspace) -> None:
    """One request touching all six, because they are one row."""
    credential = workspace.make_credential("Anthropic")

    response = workspace.patch(
        {
            "name": "Renamed",
            "provider_slug": "openai",
            "model_id": "gpt-5",
            "parameters": {"max_output_tokens": 2048},
            "credential_id": credential["id"],
            "is_default": True,
        }
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["provider_slug"] == "openai"
    assert body["model_id"] == "gpt-5"
    assert body["parameters"] == {"max_output_tokens": 2048}
    assert body["credential_id"] == credential["id"]
    assert body["is_default"] is True
    assert workspace.listed() == [body]


def test_an_absent_field_is_left_alone(workspace: Workspace) -> None:
    workspace.patch({"parameters": {"temperature": 0.2}, "model_id": "claude-opus-5"})

    renamed = workspace.patch({"name": "Only the name"})

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["parameters"] == {"temperature": 0.2}
    assert renamed.json()["model_id"] == "claude-opus-5"


def test_an_empty_body_changes_nothing(workspace: Workspace) -> None:
    before = workspace.listed()

    response = workspace.patch({})

    assert response.status_code == 200, response.text
    assert workspace.listed() == before


def test_a_null_credential_detaches_it_and_other_nulls_are_refused(
    workspace: Workspace,
) -> None:
    """`credential_id` is the only nullable column on the row.

    Clearing it leaves a profile a managed agent cannot run on - a state
    `create` can produce too, by omitting the field. A `null` for any of the
    others is a 422 naming the field, rather than the 409 a `NOT NULL`
    violation would arrive as.
    """
    credential = workspace.make_credential("Anthropic")
    workspace.patch({"credential_id": credential["id"]})

    detached = workspace.patch({"credential_id": None})
    assert detached.status_code == 200, detached.text
    assert detached.json()["credential_id"] is None

    for field in ("name", "provider_slug", "model_id", "parameters", "is_default"):
        refused = workspace.patch({field: None})
        assert refused.status_code == 422, f"{field}: {refused.text}"
        assert refused.json()["field"] == field


def test_unknown_and_immutable_fields_are_refused_by_name(workspace: Workspace) -> None:
    for body in (
        {"workspace_id": str(uuid.uuid4())},
        {"id": str(uuid.uuid4())},
        {"created_at": "2026-01-01T00:00:00Z"},
        {"secret": SECRET},
        {"display_hint": "…9f3c"},
    ):
        response = workspace.patch({"name": "Fine", **body})
        assert response.status_code == 422, f"{body}: {response.text}"
        assert next(iter(body)) in response.text


# ------------------------------------------------------------- the refusals ---


def test_an_unknown_profile_is_a_404(workspace: Workspace) -> None:
    assert workspace.patch({"name": "Nobody"}, profile_id=str(uuid.uuid4())).status_code == 404


def test_another_workspaces_profile_is_a_404(
    workspace: Workspace, client: TestClient, actor: Actor
) -> None:
    """The repository's `workspace_id` predicate, from the outside.

    The caller is an owner of both workspaces, so this is not a membership
    refusal: it is the profile not being in the workspace the path names.
    """
    elsewhere = Workspace(client, actor, name="Somewhere else")

    response = client.patch(
        f"/v1/workspaces/{workspace.id}/model-profiles/{elsewhere.profile_id}",
        json={"name": "Reached across"},
        headers=actor.headers,
    )

    assert response.status_code == 404, response.text
    assert elsewhere.listed()[0]["name"] == "Default"


async def test_a_member_may_not_edit_a_profile(
    workspace: Workspace, owner_database: Database, make_actor: Any
) -> None:
    """403 naming the action: `model_profile.update` is `admin`.

    A profile names a credential and the model every managed agent using it
    will call, which is why editing one is not a `member` act - the same
    reason `create` is not.
    """
    member = make_actor("profile-member@example.com")
    await seed_user(owner_database, member.user_id, member.email)
    await seed_membership(
        owner_database, uuid.UUID(workspace.id), member.user_id, MemberRole.MEMBER
    )

    refused = workspace.patch({"name": "Member's idea"}, actor=member)

    assert refused.status_code == 403, refused.text
    assert "model_profile.update" in refused.json()["detail"]
    assert workspace.listed()[0]["name"] == "Default"


def test_a_stranger_gets_a_404(workspace: Workspace, make_actor: Any) -> None:
    stranger = make_actor("profile-outsider@example.com")

    assert workspace.patch({"name": "Theirs"}, actor=stranger).status_code == 404


# ----------------------------------------------------------------- the name ---


def test_a_duplicate_name_is_a_409(workspace: Workspace) -> None:
    workspace.make_profile("Taken")

    refused = workspace.patch({"name": "Taken"})

    assert refused.status_code == 409, refused.text
    assert "Taken" in refused.json()["detail"]
    assert workspace.listed()[0]["name"] == "Default"


def test_the_duplicate_check_does_not_count_the_profile_being_renamed(
    workspace: Workspace,
) -> None:
    """`excluding=profile_id`: a profile is not in conflict with itself.

    Renaming to the name it already has has to succeed, or every form that
    submits every field would fail on the field nobody changed.
    """
    response = workspace.patch({"name": "Default", "model_id": "claude-haiku-5"})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Default"
    assert response.json()["model_id"] == "claude-haiku-5"


# ----------------------------------------------------------- the credential ---


def test_an_unknown_credential_is_refused(workspace: Workspace) -> None:
    refused = workspace.patch({"credential_id": str(uuid.uuid4())})

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "credential_id"
    assert workspace.listed()[0]["credential_id"] is None


def test_a_deleted_credential_is_refused(workspace: Workspace) -> None:
    """Destroying the material leaves a tombstone, and a tombstone is not a key.

    `CredentialRepository.get` filters `deleted_at IS NULL`, so this is the
    same refusal an id that never existed gets - which is the right answer:
    there is nothing left to call with.
    """
    credential = workspace.make_credential("Doomed")
    destroyed = workspace.client.delete(
        f"/v1/workspaces/{workspace.id}/credentials/{credential['id']}",
        headers=workspace.actor.headers,
    )
    assert destroyed.status_code == 204, destroyed.text

    refused = workspace.patch({"credential_id": credential["id"]})

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "credential_id"


def test_another_workspaces_credential_is_refused(
    workspace: Workspace, client: TestClient, actor: Actor
) -> None:
    """The one this stage exists to prevent.

    The caller owns both workspaces, so nothing stops them *naming* the id;
    what stops it being stored is that the credential is read back through
    this workspace's own repository, which filters on `workspace_id`.
    """
    elsewhere = Workspace(client, actor, name="Somewhere else")
    theirs = elsewhere.make_credential("Theirs")

    refused = workspace.patch({"credential_id": theirs["id"]})

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "credential_id"
    assert workspace.listed()[0]["credential_id"] is None


def test_a_revoked_credential_is_accepted(workspace: Workspace) -> None:
    """Accepted, because the repository still returns one.

    An operator mid-rotation legitimately points a profile at a key that has
    been revoked; the runtime is where a revoked key stops working, and it
    says so there. Refusing here would make the API stricter than the thing
    it is describing.
    """
    credential = workspace.make_credential("Rotating")
    revoked = workspace.client.post(
        f"/v1/workspaces/{workspace.id}/credentials/{credential['id']}/revoke",
        headers=workspace.actor.headers,
    )
    assert revoked.status_code == 200, revoked.text

    response = workspace.patch({"credential_id": credential["id"]})

    assert response.status_code == 200, response.text
    assert response.json()["credential_id"] == credential["id"]


def test_a_project_scoped_credential_is_accepted(workspace: Workspace) -> None:
    """Consistent with how the runtime resolves one.

    `CredentialService.use` reads with a workspace-only scope, so a
    project-scoped credential does resolve at dispatch. Refusing it here
    would describe a rule the runtime does not have.
    """
    credential = workspace.make_credential("Project only", project_id=workspace.project_id)
    assert credential["project_id"] == workspace.project_id

    response = workspace.patch({"credential_id": credential["id"]})

    assert response.status_code == 200, response.text
    assert response.json()["credential_id"] == credential["id"]


def test_no_credential_material_reaches_the_response(workspace: Workspace) -> None:
    """Structural, not careful: `ModelProfileView` has no field for it.

    The profile names the credential and says nothing else about it - not
    the secret, not the hint, not the ciphertext - and the same is true of
    the listing the UI reads afterwards.
    """
    credential = workspace.make_credential("Anthropic")

    response = workspace.patch({"credential_id": credential["id"]})

    assert SECRET not in response.text
    assert SECRET not in json.dumps(workspace.listed())
    assert set(response.json()) == {
        "id",
        "name",
        "provider_slug",
        "model_id",
        "parameters",
        "credential_id",
        "is_default",
    }


# -------------------------------------------------------------- the default ---


async def test_making_a_profile_default_moves_the_default(
    workspace: Workspace, owner_database: Database
) -> None:
    """`clear_default` first, so there is no moment at which two exist."""
    first = workspace.patch({"is_default": True}).json()
    second = workspace.make_profile("Second")
    assert await _defaults(owner_database, workspace.id) == [first["id"]]

    moved = workspace.patch({"is_default": True}, profile_id=str(second["id"]))

    assert moved.status_code == 200, moved.text
    assert await _defaults(owner_database, workspace.id) == [str(second["id"])]


async def test_standing_the_default_down_leaves_none(
    workspace: Workspace, owner_database: Database
) -> None:
    """The existing contract: nothing promotes a replacement.

    The partial unique index permits zero defaults - a workspace that never
    set one is in exactly this state - and no code reads "the default" to
    decide anything. An agent names its profile by id.
    """
    workspace.patch({"is_default": True})

    response = workspace.patch({"is_default": False})

    assert response.status_code == 200, response.text
    assert response.json()["is_default"] is False
    assert await _defaults(owner_database, workspace.id) == []


async def test_two_callers_racing_for_the_default_leave_exactly_one(
    workspace: Workspace, factory: UnitOfWorkFactory, actor: Actor, owner_database: Database
) -> None:
    """The index is the backstop, and the invariant is what is asserted.

    Two transactions, through the service rather than the route, because
    this is a claim about PostgreSQL and nothing short of two transactions
    can hold it. Which of them wins is the scheduler's business, and so is
    whether the loser's `clear_default` happened to see the winner's row: if
    it did, both succeed and the second simply moves the default again; if it
    did not, the second write of a default violates
    `one_default_model_profile` and the loser's transaction is aborted.

    **The loser raises `IntegrityError`, not `ConflictError`, and that is
    the design.** A service may not catch one - PostgreSQL has aborted the
    transaction, and `services` may not import SQLAlchemy at all - so the
    translation to a 409 is a registered application handler, which
    `tests/unit/test_problem_details.py` covers for every route at once.
    What belongs here is the two things that must hold whichever way the
    race falls: the constraint is what refused, and the database ends with
    exactly one default.
    """
    second = workspace.make_profile("Second")
    service = ModelProfileService(factory)
    principal = UserPrincipal(
        user_id=actor.user_id,
        email=actor.email,
        memberships={uuid.UUID(workspace.id): MemberRole.OWNER},
    )

    async def claim(profile_id: str) -> Any:
        return await service.update(
            principal,
            CallContext(request_id="race", ip=None, user_agent=None),
            uuid.UUID(workspace.id),
            uuid.UUID(profile_id),
            is_default=True,
        )

    outcomes = await asyncio.gather(
        claim(workspace.profile_id), claim(str(second["id"])), return_exceptions=True
    )

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(failures) <= 1, outcomes
    for failure in failures:
        assert isinstance(failure, IntegrityError), failure
        assert "one_default_model_profile" in str(failure), failure
    assert len(await _defaults(owner_database, workspace.id)) == 1


# ------------------------------------------------------------------- audit ---


async def test_one_audit_row_naming_the_fields(
    workspace: Workspace, owner_database: Database
) -> None:
    """`model_profile.update`: field names, the provider and the model.

    The provider and model are what an incident asks about and `create`
    records them too. `parameters` values are operator configuration and the
    credential's id is on the row already - neither belongs in a digest.
    """
    credential = workspace.make_credential("Anthropic")
    workspace.patch(
        {
            "provider_slug": "openai",
            "model_id": "gpt-5",
            "parameters": {"base_url": "https://gateway.internal"},
            "credential_id": credential["id"],
        }
    )

    rows = await _audit(owner_database, workspace.id)

    assert len(rows) == 1
    assert str(rows[0].resource_id) == workspace.profile_id
    assert rows[0].metadata == {
        "fields": ["credential_id", "model_id", "parameters", "provider_slug"],
        "provider": "openai",
        "model": "gpt-5",
    }
    recorded = json.dumps(rows[0].metadata)
    assert SECRET not in recorded
    assert "gateway.internal" not in recorded, "parameter values are not audited"


async def test_a_refused_edit_records_nothing(
    workspace: Workspace, owner_database: Database
) -> None:
    workspace.make_profile("Taken")

    assert workspace.patch({"name": "Taken"}).status_code == 409
    assert workspace.patch({"credential_id": str(uuid.uuid4())}).status_code == 422

    assert await _audit(owner_database, workspace.id) == []

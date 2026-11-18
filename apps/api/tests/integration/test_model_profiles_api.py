"""Writing a model profile: the key it names, the model it calls, the default.

Stage B2c. A profile is the one row in this product that points at a
credential, so most of this file is about that pointer: an id from another
workspace, one whose material has been destroyed, one that is revoked but
still real, one scoped to a project. The rest is the ordinary PATCH
contract - absent is not null, unknown fields are refused by name, the
duplicate-name check does not count the row being renamed - and the two
concurrency facts the partial unique index is responsible for.

The last two sections are about the two things a profile may not say, and
they cover `create` as well as `update` because the rule is about what a
profile may be rather than which request made it one:

- a `provider_slug` this build has no implementation for, which nothing
  reads until `registry.build` is asked for it mid-dispatch;
- a credential issued for a different provider from the one the profile
  names, which nothing reads until that key is sent to the wrong vendor.

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
from agentworld.infra.providers import registry
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.definitions import ModelProfileService
from helpers import Actor, seed_membership, seed_user

pytestmark = pytest.mark.integration

SECRET = "sk-live-do-not-log-me-0f9f3c"

#: A slug no build serves. Not a near-miss of a real one on purpose: the
#: service compares exactly and folds nothing, so a typo is refused rather
#: than quietly turned into the provider it resembles.
UNSERVABLE = "provider-that-does-not-exist"

#: What `second_provider` registers. Named here so a test can ask for the
#: slug without the fixture, which is what the credential cases want: a
#: credential may be stored for any provider, including one no build has.
SECOND_PROVIDER = "aurora"

#: A provider that exists only as a credential's label - the asymmetry this
#: milestone keeps. A credential records who its key is *for* and is never
#: checked against the registry; a profile is what gets called, and is.
OTHER_PROVIDER = "some-other-provider"


def _never_built(*args: Any, **kwargs: Any) -> Any:
    """A registry entry that is real enough to be listed and never called.

    No test in this file dispatches a step, so what `available()` reports is
    the whole of what the registration has to provide.
    """
    raise AssertionError("no test in this file builds a provider")


@pytest.fixture
def second_provider(monkeypatch: pytest.MonkeyPatch) -> str:
    """A provider a future build ships, registered the way a real one is.

    Patched into the registry rather than into the service, so these tests
    exercise the production path rather than a stand-in: the service asks
    the registry, and a slug the registry gained is accepted with nothing
    changing here. One entry in `_BUILDERS` is also literally how a provider
    is added - `registry` has no other seam, and inventing one for the tests
    would be a seam in shipped code that only tests use.

    `monkeypatch.undo()` in a test that wants the opposite: a build that
    retired the provider, and the rows left naming it.
    """
    monkeypatch.setattr(
        registry, "_BUILDERS", {**registry._BUILDERS, SECOND_PROVIDER: _never_built}
    )
    return SECOND_PROVIDER


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

    def post_profile(self, name: str, **overrides: Any) -> Any:
        """The create request itself, for the tests about refusing one."""
        body: dict[str, Any] = {
            "name": name,
            "provider_slug": "anthropic",
            "model_id": "claude-opus-5",
        }
        body.update(overrides)
        return self.client.post(
            f"/v1/workspaces/{self.id}/model-profiles", json=body, headers=self.actor.headers
        )

    def make_profile(self, name: str, **overrides: Any) -> Any:
        response = self.post_profile(name, **overrides)
        assert response.status_code == 201, response.text
        return response.json()

    def names(self) -> list[str]:
        return sorted(profile["name"] for profile in self.listed())

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


async def _audit(
    owner_database: Database, workspace_id: str, action: str = "model_profile.update"
) -> list[Any]:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT resource_id, metadata FROM audit_log WHERE workspace_id = :w "
                    "AND action = :a ORDER BY created_at, id"
                ),
                {"w": uuid.UUID(workspace_id), "a": action},
            )
        ).all()


# ------------------------------------------------------------- the editable ---


def test_every_editable_field_can_be_changed(workspace: Workspace, second_provider: str) -> None:
    """One request touching all six, because they are one row.

    The provider genuinely moves, which takes a second one to move to:
    `second_provider` registers one, and the credential is issued for that
    same provider because the two now have to agree. Moving both halves in
    one request is the migration the pairing rule must not prevent.
    """
    credential = workspace.make_credential("Aurora", provider_slug=second_provider)

    response = workspace.patch(
        {
            "name": "Renamed",
            "provider_slug": second_provider,
            "model_id": "aurora-1",
            "parameters": {"max_output_tokens": 2048},
            "credential_id": credential["id"],
            "is_default": True,
        }
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["provider_slug"] == second_provider
    assert body["model_id"] == "aurora-1"
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
    workspace: Workspace, owner_database: Database, second_provider: str
) -> None:
    """`model_profile.update`: field names, the provider and the model.

    The provider and model are what an incident asks about and `create`
    records them too. `parameters` values are operator configuration and the
    credential's id is on the row already - neither belongs in a digest.

    A real provider migration, for the same reason the editable-fields test
    is one: the digest is most worth having about the edit that repoints a
    workspace at another vendor.
    """
    credential = workspace.make_credential("Aurora", provider_slug=second_provider)
    workspace.patch(
        {
            "provider_slug": second_provider,
            "model_id": "aurora-1",
            "parameters": {"base_url": "https://gateway.internal"},
            "credential_id": credential["id"],
        }
    )

    rows = await _audit(owner_database, workspace.id)

    assert len(rows) == 1
    assert str(rows[0].resource_id) == workspace.profile_id
    assert rows[0].metadata == {
        "fields": ["credential_id", "model_id", "parameters", "provider_slug"],
        "provider": second_provider,
        "model": "aurora-1",
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


# ------------------------------------------------------------- the provider ---


def test_a_provider_this_build_cannot_serve_is_refused_on_create(workspace: Workspace) -> None:
    """422 on the field, and nothing stored.

    The slug decides which vendor a workspace's key is sent to, and until
    now the only thing that read it was `registry.build`, mid-dispatch - by
    which point a typo is a dead execution and a half-finished run rather
    than a message beside a form field.
    """
    refused = workspace.post_profile("Typo", provider_slug=UNSERVABLE)

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "provider_slug"
    assert workspace.names() == ["Default"]


def test_a_provider_this_build_cannot_serve_is_refused_on_update(workspace: Workspace) -> None:
    refused = workspace.patch({"provider_slug": UNSERVABLE})

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "provider_slug"
    assert workspace.listed()[0]["provider_slug"] == "anthropic"


def test_the_refusal_says_what_this_build_does_serve(workspace: Workspace) -> None:
    """A 422 that lists the alternatives, because the caller cannot guess them.

    The same list `GET /v1/meta` publishes, which is public and the same for
    every caller - a client that reads it never reaches this error at all.
    """
    detail = workspace.post_profile("Typo", provider_slug=UNSERVABLE).json()["detail"]
    served = workspace.client.get("/v1/meta").json()["providers"]

    assert served, "an empty registry would make this assertion vacuous"
    for slug in served:
        assert slug in detail


def test_a_provider_the_registry_gains_is_accepted(
    workspace: Workspace, second_provider: str
) -> None:
    """The rule is the registry, not a list of slugs written somewhere.

    `second_provider` adds an entry the way shipping a provider does, and
    neither the service nor this assertion names it. A build with another
    provider accepts it the day it ships, with no migration and no edit
    here.
    """
    created = workspace.post_profile("Aurora", provider_slug=second_provider)
    assert created.status_code == 201, created.text
    assert created.json()["provider_slug"] == second_provider

    moved = workspace.patch({"provider_slug": second_provider})
    assert moved.status_code == 200, moved.text
    assert moved.json()["provider_slug"] == second_provider


def test_the_same_slug_is_refused_once_the_build_stops_serving_it(
    workspace: Workspace, second_provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity for the test above: it is the registration that admits it.

    `undo()` is a build that retired the provider. The row stored while it
    was registered stays exactly as it was - this rule governs what may be
    written, and sweeping existing rows would be a migration nobody asked
    for.
    """
    stored = workspace.make_profile("Aurora", provider_slug=second_provider)

    monkeypatch.undo()

    refused = workspace.post_profile("Aurora II", provider_slug=second_provider)
    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "provider_slug"
    assert workspace.names() == ["Aurora", "Default"]
    assert workspace.listed()[1]["provider_slug"] == stored["provider_slug"]


def test_a_profile_whose_provider_is_no_longer_served_can_still_be_repaired(
    workspace: Workspace, second_provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming and detaching are the two edits that fix such a row.

    Re-checking the provider on a PATCH that does not mention it would make
    exactly those two impossible and leave the row unfixable through the
    API. Sending either half of the pair is what asks for the pair to be
    valid; sending neither asks nothing.
    """
    credential = workspace.make_credential("Aurora key", provider_slug=second_provider)
    stranded = workspace.make_profile(
        "Aurora", provider_slug=second_provider, credential_id=credential["id"]
    )

    monkeypatch.undo()

    renamed = workspace.patch({"name": "Aurora, retired"}, profile_id=str(stranded["id"]))
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["provider_slug"] == second_provider

    detached = workspace.patch({"credential_id": None}, profile_id=str(stranded["id"]))
    assert detached.status_code == 200, detached.text
    assert detached.json()["credential_id"] is None


async def test_a_refused_create_stores_nothing_and_moves_no_default(
    workspace: Workspace, owner_database: Database
) -> None:
    """Checked before `clear_default`, the one write `create` makes early.

    A refusal that had already stood the old default down would leave a
    workspace with no default and no new profile - worse than either the
    request succeeding or failing cleanly.
    """
    workspace.patch({"is_default": True})
    before = await _defaults(owner_database, workspace.id)
    created_rows = len(await _audit(owner_database, workspace.id, "model_profile.create"))

    refused = workspace.post_profile("Usurper", provider_slug=UNSERVABLE, is_default=True)

    assert refused.status_code == 422, refused.text
    assert workspace.names() == ["Default"]
    assert await _defaults(owner_database, workspace.id) == before
    assert (
        len(await _audit(owner_database, workspace.id, "model_profile.create")) == created_rows
    ), "a refused create was audited"


async def test_a_refused_update_leaves_the_profile_exactly_as_it_was(
    workspace: Workspace, owner_database: Database
) -> None:
    """Including the half of the request that was perfectly valid."""
    second = workspace.make_profile("Second")
    workspace.patch({"is_default": True}, profile_id=str(second["id"]))
    before = workspace.listed()
    # The line above is itself an audited edit, so the claim is that this
    # request added nothing - not that the log is empty.
    audited = await _audit(owner_database, workspace.id)

    refused = workspace.patch({"name": "Renamed", "provider_slug": UNSERVABLE, "is_default": True})

    assert refused.status_code == 422, refused.text
    assert workspace.listed() == before
    assert await _defaults(owner_database, workspace.id) == [str(second["id"])]
    assert await _audit(owner_database, workspace.id) == audited


# -------------------------------------------------- the provider's credential ---


def test_a_credential_for_another_provider_is_refused_on_create(workspace: Workspace) -> None:
    """The pairing nothing downstream checks.

    `DispatchService._plan` reads `provider_slug` and `credential_id` off
    one row and uses both as they stand, so a profile naming one vendor
    while holding another's key sends that key to the wrong endpoint. That
    is a secret disclosed to a third party by configuration, and the only
    place it can be caught is here, before the row exists.
    """
    other = workspace.make_credential("Other", provider_slug=OTHER_PROVIDER)

    refused = workspace.post_profile("Crossed", credential_id=other["id"])

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "credential_id"
    assert workspace.names() == ["Default"]


def test_a_credential_for_another_provider_is_refused_on_update(workspace: Workspace) -> None:
    other = workspace.make_credential("Other", provider_slug=OTHER_PROVIDER)

    refused = workspace.patch({"credential_id": other["id"]})

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "credential_id"
    assert workspace.listed()[0]["credential_id"] is None


def test_moving_the_provider_away_from_the_credential_it_keeps_is_refused(
    workspace: Workspace, second_provider: str
) -> None:
    """Changing one half changes the pair.

    The request names no credential at all; what it would leave behind is a
    profile pointing one way and a key issued the other, which is why the
    effective row is what gets checked rather than the fields sent.
    """
    credential = workspace.make_credential("Anthropic")
    assert workspace.patch({"credential_id": credential["id"]}).status_code == 200

    refused = workspace.patch({"provider_slug": second_provider})

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "credential_id"
    assert workspace.listed()[0]["provider_slug"] == "anthropic"
    assert workspace.listed()[0]["credential_id"] == credential["id"]


def test_a_provider_change_is_refused_while_the_credential_is_a_tombstone(
    workspace: Workspace, second_provider: str
) -> None:
    """A 422 on a field this request did not send, and the honest answer.

    Destroying a credential's material keeps the row - `audit_log` points
    at it - and `CredentialRepository.get` excludes it, so a profile can
    legitimately hold a pointer that no longer resolves. What this request
    would leave behind is a pair whose second half cannot be read at all,
    which is not something the rule can bless on the strength of the first.
    Clearing the credential in the same request is the way through.
    """
    credential = workspace.make_credential("Doomed")
    assert workspace.patch({"credential_id": credential["id"]}).status_code == 200
    destroyed = workspace.client.delete(
        f"/v1/workspaces/{workspace.id}/credentials/{credential['id']}",
        headers=workspace.actor.headers,
    )
    assert destroyed.status_code == 204, destroyed.text

    refused = workspace.patch({"provider_slug": second_provider})

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "credential_id"

    through = workspace.patch({"provider_slug": second_provider, "credential_id": None})
    assert through.status_code == 200, through.text
    assert through.json()["provider_slug"] == second_provider


def test_a_credential_matching_the_provider_is_accepted_on_create(workspace: Workspace) -> None:
    credential = workspace.make_credential("Anthropic")

    created = workspace.post_profile("Wired", credential_id=credential["id"])

    assert created.status_code == 201, created.text
    assert created.json()["credential_id"] == credential["id"]


def test_create_refuses_a_credential_this_workspace_does_not_have(
    workspace: Workspace, client: TestClient, actor: Actor
) -> None:
    """The hole this milestone closes on the create path.

    `update` has resolved the id since Stage B2c. `create` handed it
    straight to the insert, where the only thing between another tenant's
    credential and a stored pointer to it was a foreign key that does not
    know what a tenant is - and an id that exists nowhere was worse: a
    `23503` the integrity handler deliberately leaves as a 500.

    Both ids here are for a credential whose provider *matches*, so what
    refuses them is the workspace resolution and not the pairing.
    """
    elsewhere = Workspace(client, actor, name="Somewhere else")
    theirs = elsewhere.make_credential("Theirs")

    for label, credential_id in (("Invented", str(uuid.uuid4())), ("Reached", theirs["id"])):
        refused = workspace.post_profile(label, credential_id=credential_id)
        assert refused.status_code == 422, f"{label}: {refused.text}"
        assert refused.json()["field"] == "credential_id"

    assert workspace.names() == ["Default"]
    assert elsewhere.names() == ["Default"]


def test_create_refuses_a_credential_whose_material_was_destroyed(workspace: Workspace) -> None:
    """A tombstone is not a key, on this path as on the other one."""
    credential = workspace.make_credential("Doomed")
    destroyed = workspace.client.delete(
        f"/v1/workspaces/{workspace.id}/credentials/{credential['id']}",
        headers=workspace.actor.headers,
    )
    assert destroyed.status_code == 204, destroyed.text

    refused = workspace.post_profile("Pointing at a tombstone", credential_id=credential["id"])

    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "credential_id"
    assert workspace.names() == ["Default"]


def test_no_credential_material_reaches_a_mismatch_refusal(workspace: Workspace) -> None:
    """The error names two provider slugs and nothing else about the key.

    It is reached only after the row resolved in this workspace, so it
    cannot confirm a credential an outsider guessed at - and there is no
    field on `Credential` through which material could reach it even if the
    message wanted one.
    """
    other = workspace.make_credential("Other", provider_slug=OTHER_PROVIDER)

    refused = workspace.patch({"credential_id": other["id"]})

    assert refused.status_code == 422
    assert SECRET not in refused.text
    assert set(refused.json()) <= {"type", "title", "status", "detail", "instance", "field"}
    assert OTHER_PROVIDER in refused.json()["detail"], "the caller is told which two disagree"

"""The Phase 1 exit criteria, end to end over HTTP.

`docs/05-ROADMAP.md`: *"Done when a new user can sign up, create a workspace
and project, and hold an API key whose plaintext the server cannot recover."*

The first test walks exactly that path. The last one checks the second half of
the claim the hard way, by searching every text column in the database for the
secret it was just handed.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration


def test_the_whole_phase_1_journey(client: TestClient, actor: Actor) -> None:
    """Sign in, onboard, create a project, mint a key, use the key."""
    # 1. The first authenticated call materialises the local user. There is no
    #    sign-up endpoint; Supabase made the account and AgentWorld mirrors it.
    me = client.get("/v1/me", headers=actor.headers)
    assert me.status_code == 200
    assert me.json()["user"]["email"] == actor.email
    assert me.json()["needs_onboarding"] is True, "a user with no workspace needs onboarding"

    # 2. Create a workspace. The caller becomes its owner.
    created = client.post(
        "/v1/workspaces", json={"name": "Support Automation"}, headers=actor.headers
    )
    assert created.status_code == 201
    workspace = created.json()
    assert workspace["slug"] == "support-automation", "the slug is derived from the name"

    me = client.get("/v1/me", headers=actor.headers).json()
    assert me["needs_onboarding"] is False
    assert me["workspaces"][0]["role"] == "owner"

    # 3. Create a project in it.
    project_response = client.post(
        f"/v1/workspaces/{workspace['id']}/projects",
        json={"name": "EU Workers"},
        headers=actor.headers,
    )
    assert project_response.status_code == 201
    project = project_response.json()
    assert project["slug"] == "eu-workers"
    assert project["workspace_id"] == workspace["id"]

    # Addressable by id alone, with no workspace in the path.
    fetched = client.get(f"/v1/projects/{project['id']}", headers=actor.headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == project["id"]

    # 4. Mint an ingest key for that project.
    key_response = client.post(
        f"/v1/workspaces/{workspace['id']}/api-keys",
        json={"name": "EU workers", "scope": "ingest", "project_id": project["id"]},
        headers=actor.headers,
    )
    assert key_response.status_code == 201
    minted = key_response.json()
    assert minted["key"].startswith(minted["prefix"] + ".")
    assert minted["prefix"].startswith("aw_test_"), "a test environment mints test-labelled keys"

    # 5. The key works on the ingest plane, and reports what it may do.
    identity = client.get("/v1/self/key", headers={"X-AgentWorld-Key": minted["key"]})
    assert identity.status_code == 200
    assert identity.json() == {
        "api_key_id": minted["id"],
        "workspace_id": workspace["id"],
        "project_id": project["id"],
        "scope": "ingest",
        "rate_limit_rps": 50,
        "agent_key_allowlist": None,
    }

    # 6. The plaintext is never shown again.
    listed = client.get(f"/v1/workspaces/{workspace['id']}/api-keys", headers=actor.headers).json()
    assert len(listed) == 1
    assert "key" not in listed[0], "a listing must never carry the secret"
    assert listed[0]["prefix"] == minted["prefix"]


async def test_the_server_cannot_recover_a_key_secret(
    client: TestClient, actor: Actor, owner_database: Database
) -> None:
    """The strong half of the exit criterion, checked rather than asserted.

    Rather than trusting that nothing stores the plaintext, search every text
    and jsonb column in the database for it. The prefix is expected to be
    there - it is public. The secret must appear nowhere.
    """
    workspace = client.post(
        "/v1/workspaces", json={"name": "Secrets Workspace"}, headers=actor.headers
    ).json()
    project = client.post(
        f"/v1/workspaces/{workspace['id']}/projects",
        json={"name": "Primary"},
        headers=actor.headers,
    ).json()
    minted = client.post(
        f"/v1/workspaces/{workspace['id']}/api-keys",
        json={"name": "k", "scope": "ingest", "project_id": project["id"]},
        headers=actor.headers,
    ).json()

    prefix, _, secret = minted["key"].partition(".")
    assert secret, "the key should have a secret half"

    async with owner_database.session() as session:
        columns = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND data_type IN ('text', 'character varying', 'jsonb', 'citext')"
                )
            )
        ).all()

        hits: list[str] = []
        for table, column in columns:
            # Identifiers come from information_schema, not from user input.
            found = (
                await session.execute(
                    text(f'SELECT count(*) FROM "{table}" WHERE "{column}"::text LIKE :needle'),
                    {"needle": f"%{secret}%"},
                )
            ).scalar_one()
            if found:
                hits.append(f"{table}.{column}")

    assert hits == [], f"the key secret was found stored in {hits}"

    # The public half, by contrast, is stored - that is what lookup uses.
    async with owner_database.session() as session:
        stored_prefixes = (
            (await session.execute(text("SELECT prefix FROM api_keys"))).scalars().all()
        )
    assert list(stored_prefixes) == [prefix]


# ------------------------------------------------------------------- identity ---


def test_me_requires_a_token(client: TestClient) -> None:
    response = client.get("/v1/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_an_unknown_token_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/me", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "nonsense"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
    ],
)
def test_a_malformed_authorization_header_is_rejected(
    client: TestClient, header: dict[str, str]
) -> None:
    assert client.get("/v1/me", headers=header).status_code == 401


def test_patching_your_own_profile(client: TestClient, actor: Actor) -> None:
    response = client.patch("/v1/me", json={"display_name": "Grace Hopper"}, headers=actor.headers)
    assert response.status_code == 200
    assert response.json()["user"]["display_name"] == "Grace Hopper"


def test_a_profile_edit_survives_the_next_login(client: TestClient, actor: Actor) -> None:
    """The upsert must not overwrite what the user set.

    `GET /v1/me` re-upserts the mirror row on every request. If it wrote the
    token's metadata unconditionally, a user's own display name would be
    reverted by their next page load.
    """
    client.patch("/v1/me", json={"display_name": "Grace Hopper"}, headers=actor.headers)
    again = client.get("/v1/me", headers=actor.headers)
    assert again.json()["user"]["display_name"] == "Grace Hopper"


def test_an_unknown_field_is_rejected_rather_than_ignored(client: TestClient, actor: Actor) -> None:
    """A typo'd field that silently does nothing is worse than a 422."""
    response = client.patch("/v1/me", json={"dispaly_name": "typo"}, headers=actor.headers)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


# ----------------------------------------------------------------- workspaces ---


def test_creating_a_workspace_with_an_explicit_slug(client: TestClient, actor: Actor) -> None:
    response = client.post(
        "/v1/workspaces", json={"name": "Acme", "slug": "acme-eu"}, headers=actor.headers
    )
    assert response.status_code == 201
    assert response.json()["slug"] == "acme-eu"


def test_a_duplicate_slug_is_a_conflict(client: TestClient, actor: Actor) -> None:
    client.post("/v1/workspaces", json={"name": "Acme"}, headers=actor.headers)
    response = client.post("/v1/workspaces", json={"name": "Acme"}, headers=actor.headers)
    assert response.status_code == 409


def test_slugs_are_case_insensitively_unique(client: TestClient, actor: Actor) -> None:
    """`citext`, so `Acme` and `acme` cannot both exist."""
    client.post("/v1/workspaces", json={"name": "x", "slug": "acme"}, headers=actor.headers)
    response = client.post(
        "/v1/workspaces", json={"name": "y", "slug": "ACME"}, headers=actor.headers
    )
    # Normalised to lowercase before it even reaches the database.
    assert response.status_code == 409


def test_a_reserved_slug_is_refused(client: TestClient, actor: Actor) -> None:
    response = client.post(
        "/v1/workspaces", json={"name": "Settings", "slug": "settings"}, headers=actor.headers
    )
    assert response.status_code == 422


def test_a_name_that_yields_no_slug_is_refused(client: TestClient, actor: Actor) -> None:
    response = client.post("/v1/workspaces", json={"name": "🎉"}, headers=actor.headers)
    assert response.status_code == 422


def test_renaming_a_workspace_to_its_own_slug_is_not_a_conflict(
    client: TestClient, actor: Actor
) -> None:
    """A no-op rename must not 409 just because the slug is "taken" by itself."""
    workspace = client.post("/v1/workspaces", json={"name": "Acme"}, headers=actor.headers).json()
    response = client.patch(
        f"/v1/workspaces/{workspace['id']}",
        json={"name": "Acme Corp", "slug": "acme"},
        headers=actor.headers,
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Acme Corp"


def test_an_archived_workspace_disappears(client: TestClient, actor: Actor) -> None:
    workspace = client.post(
        "/v1/workspaces", json={"name": "Temporary"}, headers=actor.headers
    ).json()
    assert (
        client.delete(f"/v1/workspaces/{workspace['id']}", headers=actor.headers).status_code == 204
    )

    assert client.get(f"/v1/workspaces/{workspace['id']}", headers=actor.headers).status_code == 404
    assert client.get("/v1/workspaces", headers=actor.headers).json() == []
    assert client.get("/v1/me", headers=actor.headers).json()["needs_onboarding"] is True


def test_an_archived_workspace_keeps_its_slug(client: TestClient, actor: Actor) -> None:
    """During the grace period the workspace can be restored, which a slug
    reused in the meantime would make impossible."""
    workspace = client.post(
        "/v1/workspaces", json={"name": "Temporary"}, headers=actor.headers
    ).json()
    client.delete(f"/v1/workspaces/{workspace['id']}", headers=actor.headers)

    response = client.post("/v1/workspaces", json={"name": "Temporary"}, headers=actor.headers)
    assert response.status_code == 409


# ------------------------------------------------------------------- projects ---


def test_project_slugs_are_unique_per_workspace_not_globally(
    client: TestClient, actor: Actor
) -> None:
    """Two customers should both be able to have a project called `support`."""
    first = client.post("/v1/workspaces", json={"name": "One"}, headers=actor.headers).json()
    second = client.post("/v1/workspaces", json={"name": "Two"}, headers=actor.headers).json()

    for workspace in (first, second):
        response = client.post(
            f"/v1/workspaces/{workspace['id']}/projects",
            json={"name": "Support"},
            headers=actor.headers,
        )
        assert response.status_code == 201

    clash = client.post(
        f"/v1/workspaces/{first['id']}/projects",
        json={"name": "Support"},
        headers=actor.headers,
    )
    assert clash.status_code == 409


def test_project_settings_are_replaced_not_merged(client: TestClient, actor: Actor) -> None:
    """So that a setting can be removed. A one-level merge would make removal
    inexpressible and is a rule every client would have to learn."""
    workspace = client.post(
        "/v1/workspaces", json={"name": "Settings Demo"}, headers=actor.headers
    ).json()
    project = client.post(
        f"/v1/workspaces/{workspace['id']}/projects",
        json={"name": "Primary", "settings": {"a": 1, "b": 2}},
        headers=actor.headers,
    ).json()

    updated = client.patch(
        f"/v1/projects/{project['id']}", json={"settings": {"a": 9}}, headers=actor.headers
    )
    assert updated.json()["settings"] == {"a": 9}


def test_an_archived_project_disappears(client: TestClient, actor: Actor) -> None:
    workspace = client.post(
        "/v1/workspaces", json={"name": "Settings Demo"}, headers=actor.headers
    ).json()
    project = client.post(
        f"/v1/workspaces/{workspace['id']}/projects",
        json={"name": "Primary"},
        headers=actor.headers,
    ).json()

    assert client.delete(f"/v1/projects/{project['id']}", headers=actor.headers).status_code == 204
    assert client.get(f"/v1/projects/{project['id']}", headers=actor.headers).status_code == 404
    assert (
        client.get(f"/v1/workspaces/{workspace['id']}/projects", headers=actor.headers).json() == []
    )


def test_a_nonexistent_project_is_a_404(client: TestClient, actor: Actor) -> None:
    response = client.get(f"/v1/projects/{uuid.uuid4()}", headers=actor.headers)
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == 404
    assert "instance" in body, "a problem document carries the request id"


# ------------------------------------------------------------------ audit log ---


async def test_mutations_are_audited_with_the_request_id(
    client: TestClient, actor: Actor, owner_database: Database
) -> None:
    """Every mutating control-plane call writes a row, joinable to the response.

    The request id in the row is the same value the response returned in
    `X-Request-Id`, which is what lets a user's bug report be matched to the
    record of what their request actually did.
    """
    created = client.post("/v1/workspaces", json={"name": "Audited"}, headers=actor.headers)
    workspace_id = created.json()["id"]
    create_request_id = created.headers["X-Request-Id"]

    project_response = client.post(
        f"/v1/workspaces/{workspace_id}/projects",
        json={"name": "Primary"},
        headers=actor.headers,
    )
    project_request_id = project_response.headers["X-Request-Id"]

    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT action, actor_type, actor_id, request_id, resource_type "
                    "FROM audit_log WHERE workspace_id = :workspace_id ORDER BY id"
                ),
                {"workspace_id": uuid.UUID(workspace_id)},
            )
        ).all()

    assert [row[0] for row in rows] == ["workspace.create", "project.create"]
    assert {row[1] for row in rows} == {"user"}
    assert {row[2] for row in rows} == {actor.user_id}
    assert [row[3] for row in rows] == [create_request_id, project_request_id]
    assert [row[4] for row in rows] == ["workspace", "project"]


async def test_a_failed_mutation_leaves_no_audit_row(
    client: TestClient, actor: Actor, owner_database: Database
) -> None:
    """The audit row is written in the same transaction as the change.

    A rolled-back mutation must leave no claim that it happened. Here the
    project creation fails on a duplicate slug after the audit write would
    have been attempted.
    """
    workspace_id = client.post(
        "/v1/workspaces", json={"name": "Rollback"}, headers=actor.headers
    ).json()["id"]
    client.post(
        f"/v1/workspaces/{workspace_id}/projects", json={"name": "Primary"}, headers=actor.headers
    )
    clash = client.post(
        f"/v1/workspaces/{workspace_id}/projects", json={"name": "Primary"}, headers=actor.headers
    )
    assert clash.status_code == 409

    async with owner_database.session() as session:
        actions = (
            (
                await session.execute(
                    text(
                        "SELECT action FROM audit_log WHERE workspace_id = :workspace_id "
                        "ORDER BY id"
                    ),
                    {"workspace_id": uuid.UUID(workspace_id)},
                )
            )
            .scalars()
            .all()
        )

    assert list(actions) == ["workspace.create", "project.create"], (
        "the failed second create must not have written a row"
    )


# -------------------------------------------------------------------- health ---


def test_readiness_reports_postgres(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ok"}}

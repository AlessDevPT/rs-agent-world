"""Agents and model profiles over HTTP.

The test that matters most is the split permission: one PATCH, two
authorization checks, and a `member` who may rename an agent and may not give
it a model. The rest is the ordinary CRUD surface and the tenancy rules that
apply to every endpoint.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration


class World:
    """An owner, a workspace, a project, and a second member to demote."""

    def __init__(self, client: TestClient, actor: Actor) -> None:
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Agents"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()

    @property
    def workspace_id(self) -> str:
        return str(self.workspace["id"])

    @property
    def project_id(self) -> str:
        return str(self.project["id"])

    def agents_url(self) -> str:
        return f"/v1/projects/{self.project_id}/agents"


@pytest.fixture
def world(client: TestClient, actor: Actor) -> World:
    return World(client, actor)


async def _join(
    owner_database: Database, world: World, user_id: uuid.UUID, email: str, role: str
) -> None:
    """Add a second person at a given role, without an invitation flow."""
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": user_id, "e": email},
        )
        await session.execute(
            text(
                "INSERT INTO workspace_members (workspace_id, user_id, role) "
                "VALUES (:w, :u, :r) ON CONFLICT (workspace_id, user_id) "
                "DO UPDATE SET role = :r"
            ),
            {"w": uuid.UUID(world.workspace_id), "u": user_id, "r": role},
        )


def create_agent(client: TestClient, world: World, actor: Actor, **body: Any) -> Any:
    payload: dict[str, Any] = {"display_name": "Researcher", "interaction_mode": "managed"}
    payload.update(body)
    return client.post(world.agents_url(), json=payload, headers=actor.headers)


# ---------------------------------------------------------------------- CRUD ---


def test_an_agent_is_created_with_a_derived_key(
    client: TestClient, world: World, actor: Actor
) -> None:
    response = create_agent(client, world, actor, display_name="Market Researcher")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["agent_key"] == "market-researcher"
    assert body["interaction_mode"] == "managed"
    # Derived state comes back at its starting value and was set by nothing
    # the caller sent.
    assert body["status"] == "provisioning"
    assert body["capabilities"] == []
    assert body["last_heartbeat_at"] is None


def test_a_duplicate_agent_key_in_one_project_is_refused(
    client: TestClient, world: World, actor: Actor
) -> None:
    assert create_agent(client, world, actor, agent_key="researcher").status_code == 201
    second = create_agent(client, world, actor, agent_key="researcher")
    assert second.status_code == 409


def test_listing_and_fetching(client: TestClient, world: World, actor: Actor) -> None:
    created = create_agent(client, world, actor).json()

    listed = client.get(world.agents_url(), headers=actor.headers)
    assert listed.status_code == 200
    assert [a["id"] for a in listed.json()] == [created["id"]]

    one = client.get(f"{world.agents_url()}/{created['id']}", headers=actor.headers)
    assert one.status_code == 200
    assert one.json()["display_name"] == "Researcher"


def test_an_archived_agent_leaves_the_default_listing_and_still_exists(
    client: TestClient, world: World, actor: Actor
) -> None:
    """Archived, not deleted: it keeps its history and its place on the island."""
    created = create_agent(client, world, actor).json()
    assert (
        client.delete(f"{world.agents_url()}/{created['id']}", headers=actor.headers).status_code
        == 204
    )

    assert client.get(world.agents_url(), headers=actor.headers).json() == []
    with_archived = client.get(
        f"{world.agents_url()}?include_archived=true", headers=actor.headers
    ).json()
    assert [a["id"] for a in with_archived] == [created["id"]]
    assert with_archived[0]["archived_at"] is not None


def test_a_patch_leaves_absent_fields_alone(client: TestClient, world: World, actor: Actor) -> None:
    """Absent means "leave it", which is a different request from null."""
    created = create_agent(client, world, actor, role="Finds things").json()

    patched = client.patch(
        f"{world.agents_url()}/{created['id']}",
        json={"display_name": "Senior Researcher"},
        headers=actor.headers,
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Senior Researcher"
    assert patched.json()["role"] == "Finds things"


def test_null_clears_a_field(client: TestClient, world: World, actor: Actor) -> None:
    created = create_agent(client, world, actor, role="Finds things").json()
    patched = client.patch(
        f"{world.agents_url()}/{created['id']}",
        json={"role": None},
        headers=actor.headers,
    )
    assert patched.json()["role"] is None


# --------------------------------------------- the split permission, up close ---


async def test_a_member_may_rename_an_agent_and_may_not_give_it_a_model(
    client: TestClient,
    world: World,
    actor: Actor,
    make_actor: Any,
    owner_database: Database,
) -> None:
    """`agent.update` is member. `agent.configure` is admin.

    This is the rule the whole module exists to enforce, so it is checked at
    the level a user meets it: the same endpoint, the same agent, two
    requests that differ only in which field they touch.
    """
    agent = create_agent(client, world, actor).json()
    profile = client.post(
        f"/v1/workspaces/{world.workspace_id}/model-profiles",
        json={"name": "Default", "provider_slug": "anthropic", "model_id": "claude-opus-5"},
        headers=actor.headers,
    ).json()

    member = make_actor("member@example.com")
    await _join(owner_database, world, member.user_id, member.email, "member")

    renamed = client.patch(
        f"{world.agents_url()}/{agent['id']}",
        json={"display_name": "Renamed by a member"},
        headers=member.headers,
    )
    assert renamed.status_code == 200

    configured = client.patch(
        f"{world.agents_url()}/{agent['id']}",
        json={"model_profile_id": profile["id"]},
        headers=member.headers,
    )
    assert configured.status_code == 403
    assert "agent.configure" in configured.json()["detail"]


async def test_a_mixed_request_changes_neither_half(
    client: TestClient,
    world: World,
    actor: Actor,
    make_actor: Any,
    owner_database: Database,
) -> None:
    """Both checks run before anything is written.

    Partially applying it would be worse than refusing: the caller would have
    to diff the response to find out which half landed.
    """
    agent = create_agent(client, world, actor).json()
    profile = client.post(
        f"/v1/workspaces/{world.workspace_id}/model-profiles",
        json={"name": "Default", "provider_slug": "anthropic", "model_id": "claude-opus-5"},
        headers=actor.headers,
    ).json()

    member = make_actor("mixed@example.com")
    await _join(owner_database, world, member.user_id, member.email, "member")

    refused = client.patch(
        f"{world.agents_url()}/{agent['id']}",
        json={"display_name": "Should not stick", "model_profile_id": profile["id"]},
        headers=member.headers,
    )
    assert refused.status_code == 403

    after = client.get(f"{world.agents_url()}/{agent['id']}", headers=actor.headers).json()
    assert after["display_name"] == "Researcher", "the member half was applied anyway"
    assert after["model_profile_id"] is None


async def test_an_admin_may_configure(
    client: TestClient, world: World, actor: Actor, make_actor: Any, owner_database: Database
) -> None:
    agent = create_agent(client, world, actor).json()
    profile = client.post(
        f"/v1/workspaces/{world.workspace_id}/model-profiles",
        json={"name": "Default", "provider_slug": "anthropic", "model_id": "claude-opus-5"},
        headers=actor.headers,
    ).json()

    admin = make_actor("admin@example.com")
    await _join(owner_database, world, admin.user_id, admin.email, "admin")

    configured = client.patch(
        f"{world.agents_url()}/{agent['id']}",
        json={"model_profile_id": profile["id"]},
        headers=admin.headers,
    )
    assert configured.status_code == 200
    assert configured.json()["model_profile_id"] == profile["id"]


async def test_creating_an_agent_with_a_model_also_needs_configure(
    client: TestClient, world: World, actor: Actor, make_actor: Any, owner_database: Database
) -> None:
    """Otherwise `agent.configure` would be a rule about editing rather than
    about capability, and a member could get the same result by creating."""
    profile = client.post(
        f"/v1/workspaces/{world.workspace_id}/model-profiles",
        json={"name": "Default", "provider_slug": "anthropic", "model_id": "claude-opus-5"},
        headers=actor.headers,
    ).json()
    member = make_actor("creator@example.com")
    await _join(owner_database, world, member.user_id, member.email, "member")

    refused = create_agent(
        client, world, member, agent_key="configured", model_profile_id=profile["id"]
    )
    assert refused.status_code == 403


# ------------------------------------------------------- derived state is safe ---


def test_no_request_can_assert_what_an_agent_is_doing(
    client: TestClient, world: World, actor: Actor
) -> None:
    """There is no field for it, and no action that would authorize one.

    A human saying "this agent is working" would be asserting something only
    the agent can know, which is why status is folded from events and nothing
    else.
    """
    agent = create_agent(client, world, actor).json()
    patched = client.patch(
        f"{world.agents_url()}/{agent['id']}",
        json={"status": "working", "last_heartbeat_at": "2026-11-01T00:00:00Z"},
        headers=actor.headers,
    )
    # Unknown fields are ignored rather than rejected, so the check that
    # matters is what the agent looks like afterwards.
    assert patched.status_code == 200
    assert patched.json()["status"] == "provisioning"
    assert patched.json()["last_heartbeat_at"] is None


# ------------------------------------------------------------ model profiles ---


def test_only_a_managed_agent_may_have_a_model(
    client: TestClient, world: World, actor: Actor
) -> None:
    """The database says so too; this turns it into a sentence."""
    profile = client.post(
        f"/v1/workspaces/{world.workspace_id}/model-profiles",
        json={"name": "Default", "provider_slug": "anthropic", "model_id": "claude-opus-5"},
        headers=actor.headers,
    ).json()
    refused = create_agent(
        client,
        world,
        actor,
        interaction_mode="observed",
        model_profile_id=profile["id"],
    )
    assert refused.status_code == 422
    assert "managed" in refused.json()["detail"]


def test_a_profile_in_use_cannot_be_deleted(client: TestClient, world: World, actor: Actor) -> None:
    """Cascading would leave a workspace of managed agents with no model and
    no explanation."""
    profile = client.post(
        f"/v1/workspaces/{world.workspace_id}/model-profiles",
        json={"name": "Default", "provider_slug": "anthropic", "model_id": "claude-opus-5"},
        headers=actor.headers,
    ).json()
    create_agent(client, world, actor, model_profile_id=profile["id"])

    refused = client.delete(
        f"/v1/workspaces/{world.workspace_id}/model-profiles/{profile['id']}",
        headers=actor.headers,
    )
    assert refused.status_code == 409


def test_a_model_profile_never_returns_credential_material(
    client: TestClient, world: World, actor: Actor
) -> None:
    created = client.post(
        f"/v1/workspaces/{world.workspace_id}/model-profiles",
        json={"name": "Default", "provider_slug": "anthropic", "model_id": "claude-opus-5"},
        headers=actor.headers,
    ).json()
    assert set(created) == {
        "id",
        "name",
        "provider_slug",
        "model_id",
        "parameters",
        "credential_id",
        "is_default",
    }


def test_only_one_profile_is_the_default(client: TestClient, world: World, actor: Actor) -> None:
    url = f"/v1/workspaces/{world.workspace_id}/model-profiles"
    for name in ("First", "Second"):
        client.post(
            url,
            json={
                "name": name,
                "provider_slug": "anthropic",
                "model_id": "claude-opus-5",
                "is_default": True,
            },
            headers=actor.headers,
        )
    profiles = client.get(url, headers=actor.headers).json()
    assert [p["name"] for p in profiles if p["is_default"]] == ["Second"]


# ------------------------------------------------------------------- tenancy ---


def test_another_tenants_project_is_not_found(
    client: TestClient, world: World, make_actor: Any
) -> None:
    """404 rather than 403: telling a stranger the project exists confirms
    they guessed its id."""
    stranger = make_actor("stranger@example.com")
    assert client.get(world.agents_url(), headers=stranger.headers).status_code == 404
    assert create_agent(client, world, stranger).status_code == 404


def test_an_api_key_cannot_reach_the_control_plane(
    client: TestClient, world: World, actor: Actor
) -> None:
    minted = client.post(
        f"/v1/workspaces/{world.workspace_id}/api-keys",
        json={"name": "Worker", "scope": "ingest", "project_id": world.project_id},
        headers=actor.headers,
    ).json()
    response = client.get(world.agents_url(), headers={"X-AgentWorld-Key": str(minted["key"])})
    assert response.status_code == 401

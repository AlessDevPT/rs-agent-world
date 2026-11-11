"""Connections, drafts, and validation over HTTP.

The last test is the Phase 2 acceptance criterion read literally: an
ecosystem of two agents, a connection and a draft workflow, built through the
API, and `validate` telling the truth about whether it could run.
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


class Project:
    def __init__(self, client: TestClient, actor: Actor) -> None:
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Builder"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()

    @property
    def id(self) -> str:
        return str(self.project["id"])

    @property
    def workspace_id(self) -> str:
        return str(self.workspace["id"])


@pytest.fixture
def project(client: TestClient, actor: Actor) -> Project:
    return Project(client, actor)


def make_agent(
    client: TestClient, project: Project, actor: Actor, key: str, mode: str = "managed"
) -> Any:
    return client.post(
        f"/v1/projects/{project.id}/agents",
        json={"display_name": key.title(), "agent_key": key, "interaction_mode": mode},
        headers=actor.headers,
    ).json()


def grant(
    client: TestClient,
    project: Project,
    actor: Actor,
    agent_id: str,
    tool: str,
    effect: str = "read",
) -> Any:
    return client.post(
        f"/v1/projects/{project.id}/connections",
        json={
            "kind": "grant_tool",
            "source_kind": "agent",
            "source_id": agent_id,
            "target_kind": "tool",
            "target_id": str(uuid.uuid4()),
            "config": {"tool_name": tool, "side_effect": effect},
        },
        headers=actor.headers,
    )


def make_workflow(
    client: TestClient, project: Project, actor: Actor, graph: dict[str, Any] | None = None
) -> Any:
    return client.post(
        f"/v1/projects/{project.id}/workflows",
        json={"name": "Competitor brief", "graph": graph},
        headers=actor.headers,
    ).json()


def validate(client: TestClient, project: Project, actor: Actor, workflow_id: str) -> Any:
    response = client.post(
        f"/v1/projects/{project.id}/workflows/{workflow_id}/validate",
        headers=actor.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _join(
    owner_database: Database, project: Project, user_id: uuid.UUID, email: str, role: str
) -> None:
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
            {"w": uuid.UUID(project.workspace_id), "u": user_id, "r": role},
        )


# ------------------------------------------------------------- connections ---


def test_a_read_only_grant_is_a_member_action(
    client: TestClient, project: Project, actor: Actor
) -> None:
    agent = make_agent(client, project, actor, "researcher")
    response = grant(client, project, actor, agent["id"], "web_search")
    assert response.status_code == 201
    assert response.json()["config"]["tool_name"] == "web_search"


async def test_granting_a_consequential_tool_needs_an_admin(
    client: TestClient,
    project: Project,
    actor: Actor,
    make_actor: Any,
    owner_database: Database,
) -> None:
    """Granting a capability is at least as privileged as using it.

    A `member` may call a tool an agent already holds; wiring an agent to one
    that sends messages is an admin decision.
    """
    agent = make_agent(client, project, actor, "writer")
    member = make_actor("grant-member@example.com")
    await _join(owner_database, project, member.user_id, member.email, "member")

    harmless = grant(client, project, member, agent["id"], "web_search", "read")
    assert harmless.status_code == 201

    dangerous = grant(client, project, member, agent["id"], "send_slack_message", "consequential")
    assert dangerous.status_code == 403
    assert "connection.create" in dangerous.json()["detail"]


async def test_an_unrecognised_side_effect_is_treated_as_consequential(
    client: TestClient,
    project: Project,
    actor: Actor,
    make_actor: Any,
    owner_database: Database,
) -> None:
    """Guessing low would mean a member granting an agent the ability to send
    email because of a typo in a field nobody validates."""
    agent = make_agent(client, project, actor, "typo")
    member = make_actor("typo-member@example.com")
    await _join(owner_database, project, member.user_id, member.email, "member")

    response = grant(client, project, member, agent["id"], "do_something", "catastrophic")
    assert response.status_code == 403


def patch_connection(
    client: TestClient, project: Project, actor: Actor, connection_id: str, body: Any
) -> Any:
    return client.patch(
        f"/v1/projects/{project.id}/connections/{connection_id}",
        json=body,
        headers=actor.headers,
    )


def test_a_grant_can_be_switched_off_and_stops_counting(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """`enabled=false` takes effect on the next read, not on a restart.

    Proved through `validate`, which is what the orchestrator's own grant
    lookup answers: the tool was granted, the workflow validated cleanly,
    and switching the grant off makes the same draft report
    `tool_not_granted`.
    """
    agent = make_agent(client, project, actor, "writer")
    connection = grant(client, project, actor, agent["id"], "send_slack_message", "write").json()
    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [
                {
                    "step_key": "post",
                    "kind": "agent_task",
                    "agent_key": "writer",
                    "tool_name": "send_slack_message",
                }
            ],
            "edges": [],
        },
    )
    workflow_id = created["workflow"]["id"]
    assert not [
        f
        for f in validate(client, project, actor, workflow_id)["findings"]
        if f["code"] == "tool_not_granted"
    ]

    response = patch_connection(client, project, actor, connection["id"], {"enabled": False})

    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert any(
        f["code"] == "tool_not_granted"
        for f in validate(client, project, actor, workflow_id)["findings"]
    )


def test_the_ends_of_a_connection_are_refused_by_name(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """422: a connection with a different end is a different connection.

    `kind`, `source_*` and `target_*` are together `connections_edge_key`,
    so repointing a grant is a delete and a create - each with its own
    authorization and its own audit row.
    """
    agent = make_agent(client, project, actor, "researcher")
    connection = grant(client, project, actor, agent["id"], "web_search").json()

    for body in (
        {"kind": "delegate"},
        {"source_id": str(uuid.uuid4())},
        {"source_kind": "workflow"},
        {"target_id": str(uuid.uuid4())},
        {"target_kind": "agent"},
    ):
        response = patch_connection(client, project, actor, connection["id"], body)
        assert response.status_code == 422, f"{body}: {response.text}"
        assert next(iter(body)) in response.text


def test_the_config_is_replaced_whole(client: TestClient, project: Project, actor: Actor) -> None:
    """One decision, not two halves merged into a permission nobody chose."""
    agent = make_agent(client, project, actor, "researcher")
    connection = grant(client, project, actor, agent["id"], "web_search", "read").json()

    updated = patch_connection(
        client,
        project,
        actor,
        connection["id"],
        {"config": {"tool_name": "web_search"}, "requires_approval": True},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["config"] == {"tool_name": "web_search"}, "no leftover side_effect"
    assert updated.json()["requires_approval"] is True
    assert updated.json()["enabled"] is True, "absent means leave it alone"


async def test_a_member_may_edit_a_harmless_grant(
    client: TestClient,
    project: Project,
    actor: Actor,
    make_actor: Any,
    owner_database: Database,
) -> None:
    agent = make_agent(client, project, actor, "researcher")
    connection = grant(client, project, actor, agent["id"], "web_search", "read").json()
    member = make_actor("patch-member@example.com")
    await _join(owner_database, project, member.user_id, member.email, "member")

    response = patch_connection(client, project, member, connection["id"], {"enabled": False})

    assert response.status_code == 200, response.text


async def test_a_member_may_not_make_a_grant_consequential(
    client: TestClient,
    project: Project,
    actor: Actor,
    make_actor: Any,
    owner_database: Database,
) -> None:
    """The incoming half of the elevation: granting is an admin act.

    The same rule `connection.create` enforces, now on the verb that could
    otherwise reach the same state in two steps.
    """
    agent = make_agent(client, project, actor, "writer")
    connection = grant(client, project, actor, agent["id"], "web_search", "read").json()
    member = make_actor("widen-member@example.com")
    await _join(owner_database, project, member.user_id, member.email, "member")

    refused = patch_connection(
        client,
        project,
        member,
        connection["id"],
        {"config": {"tool_name": "send_slack_message", "side_effect": "consequential"}},
    )

    assert refused.status_code == 403, refused.text
    assert "connection.update" in refused.json()["detail"]
    listed = client.get(f"/v1/projects/{project.id}/connections", headers=actor.headers).json()
    assert listed[0]["config"] == {"tool_name": "web_search", "side_effect": "read"}


async def test_a_member_may_not_edit_a_grant_that_is_already_consequential(
    client: TestClient,
    project: Project,
    actor: Actor,
    make_actor: Any,
    owner_database: Database,
) -> None:
    """The stored half, which is the direction a config rewrite would hide.

    A member replacing a consequential grant's config with a harmless one is
    still editing a capability they may not grant - the config is the whole
    of what the grant permits, so "what it will be" cannot be the only thing
    the check looks at.
    """
    agent = make_agent(client, project, actor, "writer")
    connection = grant(
        client, project, actor, agent["id"], "send_slack_message", "consequential"
    ).json()
    member = make_actor("narrow-member@example.com")
    await _join(owner_database, project, member.user_id, member.email, "member")

    for body in (
        {"config": {"tool_name": "web_search", "side_effect": "read"}},
        {"enabled": False},
        {"requires_approval": False},
    ):
        refused = patch_connection(client, project, member, connection["id"], body)
        assert refused.status_code == 403, f"{body}: {refused.text}"

    listed = client.get(f"/v1/projects/{project.id}/connections", headers=actor.headers).json()
    assert listed[0]["config"]["side_effect"] == "consequential"
    assert listed[0]["enabled"] is True


async def test_an_admin_may_edit_a_consequential_grant(
    client: TestClient,
    project: Project,
    actor: Actor,
    make_actor: Any,
    owner_database: Database,
) -> None:
    """The other side of the same rule: refused for a member, allowed here."""
    agent = make_agent(client, project, actor, "writer")
    connection = grant(
        client, project, actor, agent["id"], "send_slack_message", "consequential"
    ).json()
    admin = make_actor("patch-admin@example.com")
    await _join(owner_database, project, admin.user_id, admin.email, "admin")

    response = patch_connection(client, project, admin, connection["id"], {"enabled": False})

    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False


def test_an_unknown_connection_is_a_404(client: TestClient, project: Project, actor: Actor) -> None:
    assert (
        patch_connection(client, project, actor, str(uuid.uuid4()), {"enabled": False}).status_code
        == 404
    )


def test_another_projects_connection_cannot_be_edited(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """The repository's `project_id` predicate, from the outside."""
    agent = make_agent(client, project, actor, "researcher")
    connection = grant(client, project, actor, agent["id"], "web_search").json()
    elsewhere = client.post(
        f"/v1/workspaces/{project.workspace_id}/projects",
        json={"name": "Other island"},
        headers=actor.headers,
    ).json()

    response = client.patch(
        f"/v1/projects/{elsewhere['id']}/connections/{connection['id']}",
        json={"enabled": False},
        headers=actor.headers,
    )

    assert response.status_code == 404, response.text
    listed = client.get(f"/v1/projects/{project.id}/connections", headers=actor.headers).json()
    assert listed[0]["enabled"] is True


def test_a_stranger_cannot_edit_a_connection(
    client: TestClient, project: Project, actor: Actor, make_actor: Any
) -> None:
    agent = make_agent(client, project, actor, "researcher")
    connection = grant(client, project, actor, agent["id"], "web_search").json()
    stranger = make_actor("connection-outsider@example.com")

    response = patch_connection(client, project, stranger, connection["id"], {"enabled": False})

    assert response.status_code == 404, response.text


async def test_the_edit_records_one_row_naming_the_fields(
    client: TestClient, project: Project, actor: Actor, owner_database: Database
) -> None:
    """`connection.update`, with `consequential` saying why it needed what it
    needed."""
    agent = make_agent(client, project, actor, "writer")
    connection = grant(
        client, project, actor, agent["id"], "send_slack_message", "consequential"
    ).json()
    patch_connection(client, project, actor, connection["id"], {"enabled": False})

    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT resource_id, metadata FROM audit_log "
                    "WHERE workspace_id = :w AND action = 'connection.update'"
                ),
                {"w": uuid.UUID(project.workspace_id)},
            )
        ).all()

    assert len(rows) == 1
    assert str(rows[0].resource_id) == connection["id"]
    assert rows[0].metadata == {"fields": ["enabled"], "consequential": True}


def test_a_connection_can_be_revoked(client: TestClient, project: Project, actor: Actor) -> None:
    agent = make_agent(client, project, actor, "researcher")
    created = grant(client, project, actor, agent["id"], "web_search").json()
    assert (
        client.delete(
            f"/v1/projects/{project.id}/connections/{created['id']}", headers=actor.headers
        ).status_code
        == 204
    )
    assert client.get(f"/v1/projects/{project.id}/connections", headers=actor.headers).json() == []


# ------------------------------------------------------------------ drafts ---


def test_a_workflow_arrives_with_a_draft(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """So the editor never meets a workflow with no version to open."""
    created = make_workflow(client, project, actor)
    assert created["workflow"]["slug"] == "competitor-brief"
    assert created["draft"]["version"] == 1
    assert created["draft"]["graph"] == {"steps": [], "edges": []}
    assert created["workflow"]["current_version_id"] is None
    assert created["workflow"]["enabled"] is False


def test_a_draft_is_saved_whole_and_read_back(
    client: TestClient, project: Project, actor: Actor
) -> None:
    created = make_workflow(client, project, actor)
    workflow_id = created["workflow"]["id"]
    graph = {
        "steps": [{"step_key": "research", "kind": "agent_task", "agent_key": "researcher"}],
        "edges": [],
    }
    saved = client.put(
        f"/v1/projects/{project.id}/workflows/{workflow_id}/draft",
        json={"graph": graph},
        headers=actor.headers,
    )
    assert saved.status_code == 200
    assert saved.json()["graph"] == graph

    fetched = client.get(
        f"/v1/projects/{project.id}/workflows/{workflow_id}/draft", headers=actor.headers
    )
    assert fetched.json()["graph"] == graph


# ---------------------------------------------------------------- validate ---


def test_validate_reports_an_observed_agent(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """The line drawn to an agent that only reports.

    On the island it looks exactly like a line to one that can be
    instructed, which is the whole reason this finding exists.
    """
    make_agent(client, project, actor, "crawler", mode="observed")
    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [{"step_key": "research", "kind": "agent_task", "agent_key": "crawler"}],
            "edges": [],
        },
    )
    report = validate(client, project, actor, created["workflow"]["id"])

    assert report["publishable"] is False
    finding = next(f for f in report["findings"] if f["code"] == "observed_agent_assigned")
    assert finding["pointer"] == "/steps/research"
    assert "cannot be given work" in finding["detail"]
    assert finding["remedy"]


def test_validate_reports_a_tool_the_agent_was_never_granted(
    client: TestClient, project: Project, actor: Actor
) -> None:
    make_agent(client, project, actor, "writer")
    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [
                {
                    "step_key": "post",
                    "kind": "agent_task",
                    "agent_key": "writer",
                    "tool_name": "send_slack_message",
                }
            ],
            "edges": [],
        },
    )
    report = validate(client, project, actor, created["workflow"]["id"])
    assert any(f["code"] == "tool_not_granted" for f in report["findings"])


def test_a_granted_tool_validates_cleanly(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """So the finding above is about the grant and not about the step."""
    agent = make_agent(client, project, actor, "writer")
    grant(client, project, actor, agent["id"], "send_slack_message", "write")
    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [
                {
                    "step_key": "post",
                    "kind": "agent_task",
                    "agent_key": "writer",
                    "tool_name": "send_slack_message",
                }
            ],
            "edges": [],
        },
    )
    report = validate(client, project, actor, created["workflow"]["id"])
    assert report["findings"] == []
    assert report["publishable"] is True


def test_a_disabled_grant_does_not_count(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """A switched-off connection is a capability someone deliberately
    removed, and treating it as live would miss exactly that case."""
    agent = make_agent(client, project, actor, "writer")
    client.post(
        f"/v1/projects/{project.id}/connections",
        json={
            "kind": "grant_tool",
            "source_kind": "agent",
            "source_id": agent["id"],
            "target_kind": "tool",
            "target_id": str(uuid.uuid4()),
            "config": {"tool_name": "send_slack_message", "side_effect": "write"},
            "enabled": False,
        },
        headers=actor.headers,
    )
    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [
                {
                    "step_key": "post",
                    "kind": "agent_task",
                    "agent_key": "writer",
                    "tool_name": "send_slack_message",
                }
            ],
            "edges": [],
        },
    )
    report = validate(client, project, actor, created["workflow"]["id"])
    assert any(f["code"] == "tool_not_granted" for f in report["findings"])


def test_a_consequential_tool_with_nothing_gating_it_is_a_warning(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """Whether to gate it is the approval policy's call, so it does not block."""
    agent = make_agent(client, project, actor, "writer")
    grant(client, project, actor, agent["id"], "send_slack_message", "consequential")
    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [
                {
                    "step_key": "post",
                    "kind": "agent_task",
                    "agent_key": "writer",
                    "tool_name": "send_slack_message",
                }
            ],
            "edges": [],
        },
    )
    report = validate(client, project, actor, created["workflow"]["id"])
    finding = next(f for f in report["findings"] if f["code"] == "consequential_without_approval")
    assert finding["severity"] == "warning"
    assert report["publishable"] is True


def test_validate_reports_four_eyes_in_a_workspace_of_one(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """A deadlock built by configuration, caught before it strands a run."""
    make_agent(client, project, actor, "writer")
    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [
                {
                    "step_key": "post",
                    "kind": "agent_task",
                    "agent_key": "writer",
                    "approval_requires_different_user": True,
                }
            ],
            "edges": [],
        },
    )
    report = validate(client, project, actor, created["workflow"]["id"])
    finding = next(f for f in report["findings"] if f["code"] == "approval_unsatisfiable")
    assert "only one member who can approve" in finding["detail"]


def test_validate_survives_a_graph_full_of_nonsense(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """The shape a half-finished UI or a conversational builder will send.

    Findings, never an exception - a 500 here would be the builder's answer
    to "is this ready?".
    """
    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [
                {"step_key": "a", "kind": "not-a-kind", "condition": "!!!"},
                "a bare string where a step should be",
                {"kind": "agent_task"},
            ],
            "edges": [{"to": "nowhere"}, {"from": "a"}, 42],
        },
    )
    report = validate(client, project, actor, created["workflow"]["id"])
    assert report["publishable"] is False
    assert report["findings"]


def test_a_graph_that_is_not_even_a_graph_still_validates(
    client: TestClient, project: Project, actor: Actor
) -> None:
    created = make_workflow(client, project, actor, {"steps": "not a list"})
    report = validate(client, project, actor, created["workflow"]["id"])
    assert any(f["code"] == "no_steps" for f in report["findings"])


# ------------------------------------------- the Phase 2 acceptance criterion ---


def test_an_ecosystem_is_built_through_the_api_and_reads_back(
    client: TestClient, project: Project, actor: Actor
) -> None:
    """Two agents, a connection and a draft workflow, created and fetched.

    Then validated, which is the part that makes it more than four rows: the
    island shows a chain of three steps, and the backend says whether the
    chain could actually run.
    """
    researcher = make_agent(client, project, actor, "researcher")
    make_agent(client, project, actor, "writer")
    grant(client, project, actor, researcher["id"], "web_search", "read")

    created = make_workflow(
        client,
        project,
        actor,
        {
            "steps": [
                {
                    "step_key": "research",
                    "kind": "agent_task",
                    "agent_key": "researcher",
                    "tool_name": "web_search",
                },
                {"step_key": "write", "kind": "agent_task", "agent_key": "writer"},
                {
                    "step_key": "review",
                    "kind": "approval",
                    "condition": "$.steps.research.output.count > 3",
                },
            ],
            "edges": [
                {"from": "research", "to": "write"},
                {"from": "write", "to": "review"},
            ],
        },
    )

    agents = client.get(f"/v1/projects/{project.id}/agents", headers=actor.headers).json()
    connections = client.get(f"/v1/projects/{project.id}/connections", headers=actor.headers).json()
    workflows = client.get(f"/v1/projects/{project.id}/workflows", headers=actor.headers).json()

    assert sorted(a["agent_key"] for a in agents) == ["researcher", "writer"]
    assert len(connections) == 1
    assert [w["slug"] for w in workflows] == ["competitor-brief"]

    report = validate(client, project, actor, created["workflow"]["id"])
    assert report["findings"] == [], report["findings"]
    assert report["publishable"] is True


# ------------------------------------------------------------------ tenancy ---


def test_a_stranger_sees_no_project(client: TestClient, project: Project, make_actor: Any) -> None:
    stranger = make_actor("outsider@example.com")
    assert (
        client.get(f"/v1/projects/{project.id}/workflows", headers=stranger.headers).status_code
        == 404
    )
    assert (
        client.get(f"/v1/projects/{project.id}/connections", headers=stranger.headers).status_code
        == 404
    )

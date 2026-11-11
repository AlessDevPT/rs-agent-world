"""Bringing an archived agent or workflow back, and nothing else with it.

Stage C1. Archiving was always a single column write - no cascade, no
delete, and both uniqueness checks count archived rows, so the row holds its
own key or slug while it is away. Restoring is therefore the same single
column write in reverse, and the tests that matter are the ones about what
it must *not* touch: an agent's event-folded status and heartbeat, and a
workflow's versions, draft and pointer.

The other half is discovery. A restore endpoint for workflows is unusable
on its own, because until now nothing could list an archived one - so
`?include_archived=true` is tested here rather than beside the listing.

Two sentences in the codebase have told users to "restore the agent" since
the agent-test compiler was written. This is the file that makes them true.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain.enums import MemberRole
from agentworld.infra.db import Database
from helpers import Actor, seed_membership, seed_user

pytestmark = pytest.mark.integration

SAMPLE = "Summarise the fintech funding news."


class Island:
    """A workspace, a project, one managed agent and one workflow."""

    def __init__(self, client: TestClient, actor: Actor, name: str = "Restores") -> None:
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
        self.agent = client.post(
            f"{self.agents}",
            json={
                "display_name": "Researcher",
                "agent_key": "researcher",
                "interaction_mode": "managed",
            },
            headers=actor.headers,
        ).json()
        self.workflow = client.post(
            f"{self.workflows}",
            json={
                "name": "Competitor brief",
                "graph": {
                    "steps": [
                        {"step_key": "research", "kind": "agent_task", "agent_key": "researcher"}
                    ],
                    "edges": [],
                },
            },
            headers=actor.headers,
        ).json()["workflow"]

    @property
    def workspace_id(self) -> str:
        return str(self.workspace["id"])

    @property
    def project_id(self) -> str:
        return str(self.project["id"])

    @property
    def agents(self) -> str:
        return f"/v1/projects/{self.project_id}/agents"

    @property
    def workflows(self) -> str:
        return f"/v1/projects/{self.project_id}/workflows"

    @property
    def agent_id(self) -> str:
        return str(self.agent["id"])

    @property
    def workflow_id(self) -> str:
        return str(self.workflow["id"])

    def headers(self, actor: Actor | None = None) -> Any:
        return (actor or self.actor).headers

    # ------------------------------------------------------------- verbs ---

    def archive_agent(self, agent_id: str | None = None) -> Any:
        return self.client.delete(
            f"{self.agents}/{agent_id or self.agent_id}", headers=self.headers()
        )

    def restore_agent(self, agent_id: str | None = None, actor: Actor | None = None) -> Any:
        return self.client.post(
            f"{self.agents}/{agent_id or self.agent_id}/restore", headers=self.headers(actor)
        )

    def archive_workflow(self, workflow_id: str | None = None) -> Any:
        return self.client.delete(
            f"{self.workflows}/{workflow_id or self.workflow_id}", headers=self.headers()
        )

    def restore_workflow(self, workflow_id: str | None = None, actor: Actor | None = None) -> Any:
        return self.client.post(
            f"{self.workflows}/{workflow_id or self.workflow_id}/restore",
            headers=self.headers(actor),
        )

    # ------------------------------------------------------------- reads ---

    def read_agent(self, agent_id: str | None = None) -> Any:
        return self.client.get(
            f"{self.agents}/{agent_id or self.agent_id}", headers=self.headers()
        ).json()

    def listed_agents(self, include_archived: bool = False) -> list[Any]:
        response = self.client.get(
            f"{self.agents}?include_archived={str(include_archived).lower()}",
            headers=self.headers(),
        )
        assert response.status_code == 200, response.text
        return list(response.json())

    def read_workflow(self, suffix: str = "", workflow_id: str | None = None) -> Any:
        return self.client.get(
            f"{self.workflows}/{workflow_id or self.workflow_id}{suffix}", headers=self.headers()
        ).json()

    def listed_workflows(self, include_archived: bool = False) -> list[Any]:
        response = self.client.get(
            f"{self.workflows}?include_archived={str(include_archived).lower()}",
            headers=self.headers(),
        )
        assert response.status_code == 200, response.text
        return list(response.json())


@pytest.fixture
def island(client: TestClient, actor: Actor) -> Island:
    return Island(client, actor)


async def _actions(owner_database: Database, workspace_id: str, action: str) -> list[Any]:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT resource_id, resource_type, metadata FROM audit_log "
                    "WHERE workspace_id = :w AND action = :a ORDER BY created_at, id"
                ),
                {"w": uuid.UUID(workspace_id), "a": action},
            )
        ).all()


async def _seed_derived_state(owner_database: Database, agent_id: str) -> None:
    """Give the agent the state a live one would have folded from events.

    Written as the owner rather than through an endpoint, because no endpoint
    can write it: that is the invariant this file is about. A heartbeat an
    hour old and a status of `working` is exactly the awkward case - a
    restore must bring it back unchanged rather than tidy it up.
    """
    async with owner_database.session() as session:
        await session.execute(
            text(
                "UPDATE agents SET status = 'working', status_reason = 'mid-task', "
                "capabilities = ARRAY['task.claim','command.pause'], "
                "last_heartbeat_at = now() - interval '1 hour' WHERE id = :a"
            ),
            {"a": uuid.UUID(agent_id)},
        )


async def _count(owner_database: Database, table: str, project_id: str) -> int:
    async with owner_database.session() as session:
        return int(
            (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE project_id = :p"),
                    {"p": uuid.UUID(project_id)},
                )
            ).scalar_one()
        )


# --------------------------------------------------------------- the agent ---


def test_an_archived_agent_comes_back(island: Island) -> None:
    assert island.archive_agent().status_code == 204
    assert [row["id"] for row in island.listed_agents()] == []

    response = island.restore_agent()

    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] is None
    assert [row["id"] for row in island.listed_agents()] == [island.agent_id]
    assert island.read_agent()["archived_at"] is None


async def test_restoring_an_agent_writes_nothing_derived(
    island: Island, owner_database: Database
) -> None:
    """Decision 3, as an equality.

    Status, reason, capabilities, heartbeat and current task are folded from
    events, and a restore is not an event. The agent comes back saying
    `working` with an hour-old heartbeat - which is honest, and which a
    reader judges from `last_heartbeat_at` exactly as they must for an agent
    that stopped reporting without being archived.
    """
    await _seed_derived_state(owner_database, island.agent_id)
    before = island.read_agent()
    assert before["status"] == "working"

    island.archive_agent()
    restored = island.restore_agent().json()

    derived = ("status", "status_reason", "capabilities", "last_heartbeat_at", "current_task_id")
    assert {field: restored[field] for field in derived} == {
        field: before[field] for field in derived
    }


def test_restoring_an_agent_that_was_never_archived_is_a_404(island: Island) -> None:
    """ "Restore this" about a live agent is a caller who believes something
    untrue, and silence would leave them believing it."""
    assert island.restore_agent().status_code == 404


def test_an_unknown_agent_is_a_404(island: Island) -> None:
    assert island.restore_agent(agent_id=str(uuid.uuid4())).status_code == 404


def test_another_projects_agent_cannot_be_restored(
    island: Island, client: TestClient, actor: Actor
) -> None:
    island.archive_agent()
    elsewhere = client.post(
        f"/v1/workspaces/{island.workspace_id}/projects",
        json={"name": "Other island"},
        headers=actor.headers,
    ).json()

    response = client.post(
        f"/v1/projects/{elsewhere['id']}/agents/{island.agent_id}/restore",
        headers=actor.headers,
    )

    assert response.status_code == 404, response.text
    assert island.read_agent()["archived_at"] is not None


def test_a_stranger_cannot_restore_an_agent(island: Island, make_actor: Any) -> None:
    island.archive_agent()
    stranger = make_actor("restore-outsider@example.com")

    assert island.restore_agent(actor=stranger).status_code == 404
    assert island.read_agent()["archived_at"] is not None


async def test_restoring_an_agent_is_an_admin_action(
    island: Island, owner_database: Database, make_actor: Any
) -> None:
    """`admin`, like archiving: a restored managed agent can be dispatched."""
    island.archive_agent()
    member = make_actor("restore-member@example.com")
    await seed_user(owner_database, member.user_id, member.email)
    await seed_membership(
        owner_database, uuid.UUID(island.workspace_id), member.user_id, MemberRole.MEMBER
    )

    refused = island.restore_agent(actor=member)
    assert refused.status_code == 403, refused.text
    assert "agent.restore" in refused.json()["detail"]
    assert island.read_agent()["archived_at"] is not None

    admin = make_actor("restore-admin@example.com")
    await seed_user(owner_database, admin.user_id, admin.email)
    await seed_membership(
        owner_database, uuid.UUID(island.workspace_id), admin.user_id, MemberRole.ADMIN
    )
    assert island.restore_agent(actor=admin).status_code == 200


def test_testing_an_agent_works_again_after_a_restore(island: Island) -> None:
    """The remedy the compiler has been printing since it was written.

    `agent.test` refuses an archived agent with `agent_archived`, whose
    remedy says "Restore the agent, then test it" - and until now named
    something no endpoint could do. The harness is the same one, on the
    version the first test published: restoring does not republish.
    """
    first = island.client.post(
        f"{island.agents}/{island.agent_id}/test", json={"input": SAMPLE}, headers=island.headers()
    )
    assert first.status_code == 202, first.text

    island.archive_agent()
    refused = island.client.post(
        f"{island.agents}/{island.agent_id}/test", json={"input": SAMPLE}, headers=island.headers()
    )
    assert refused.status_code == 422, refused.text
    assert any(e["code"] == "agent_archived" for e in refused.json()["errors"])

    island.restore_agent()
    again = island.client.post(
        f"{island.agents}/{island.agent_id}/test", json={"input": SAMPLE}, headers=island.headers()
    )

    assert again.status_code == 202, again.text
    assert again.json()["workflow_id"] == first.json()["workflow_id"]
    assert again.json()["workflow_version_id"] == first.json()["workflow_version_id"]


# ------------------------------------------------------------ the workflow ---


def test_an_archived_workflow_comes_back(island: Island) -> None:
    assert island.archive_workflow().status_code == 204
    assert island.listed_workflows() == []

    response = island.restore_workflow()

    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] is None
    assert [row["id"] for row in island.listed_workflows()] == [island.workflow_id]


def test_a_restored_workflow_keeps_its_versions_its_draft_and_its_pointer(
    island: Island,
) -> None:
    """Archiving never touched them, so restoring has nothing to put back.

    Asserted as equality over all three reads, because "runnable again at
    the version it was runnable at" is the whole claim: the pointer, the
    frozen history and the open draft the editor would reopen.
    """
    published = island.client.post(
        f"{island.workflows}/{island.workflow_id}/publish", headers=island.headers()
    )
    assert published.status_code == 201, published.text
    before = (
        island.read_workflow(),
        island.read_workflow("/versions"),
        island.read_workflow("/draft"),
    )

    island.archive_workflow()
    restored = island.restore_workflow()

    assert restored.status_code == 200, restored.text
    after = (
        island.read_workflow(),
        island.read_workflow("/versions"),
        island.read_workflow("/draft"),
    )
    assert after == before
    assert island.read_workflow()["current_version_id"] == published.json()["version_id"]


async def test_restoring_a_workflow_starts_nothing(
    island: Island, owner_database: Database
) -> None:
    """It makes a workflow runnable. It does not run it."""
    island.client.post(f"{island.workflows}/{island.workflow_id}/publish", headers=island.headers())
    island.archive_workflow()
    before = await _count(owner_database, "workflow_executions", island.project_id)

    island.restore_workflow()

    assert await _count(owner_database, "workflow_executions", island.project_id) == before
    assert len(island.read_workflow("/versions")) == 1, "nothing was published"


def test_restoring_a_workflow_that_was_never_archived_is_a_404(island: Island) -> None:
    assert island.restore_workflow().status_code == 404


def test_an_unknown_workflow_is_a_404(island: Island) -> None:
    assert island.restore_workflow(workflow_id=str(uuid.uuid4())).status_code == 404


def test_another_projects_workflow_cannot_be_restored(
    island: Island, client: TestClient, actor: Actor
) -> None:
    island.archive_workflow()
    elsewhere = client.post(
        f"/v1/workspaces/{island.workspace_id}/projects",
        json={"name": "Other island"},
        headers=actor.headers,
    ).json()

    response = client.post(
        f"/v1/projects/{elsewhere['id']}/workflows/{island.workflow_id}/restore",
        headers=actor.headers,
    )

    assert response.status_code == 404, response.text
    assert island.read_workflow()["archived_at"] is not None


def test_a_stranger_cannot_restore_a_workflow(island: Island, make_actor: Any) -> None:
    island.archive_workflow()
    stranger = make_actor("workflow-outsider@example.com")

    assert island.restore_workflow(actor=stranger).status_code == 404


async def test_restoring_a_workflow_is_an_admin_action(
    island: Island, owner_database: Database, make_actor: Any
) -> None:
    island.archive_workflow()
    member = make_actor("workflow-restore-member@example.com")
    await seed_user(owner_database, member.user_id, member.email)
    await seed_membership(
        owner_database, uuid.UUID(island.workspace_id), member.user_id, MemberRole.MEMBER
    )

    refused = island.restore_workflow(actor=member)

    assert refused.status_code == 403, refused.text
    assert "workflow.restore" in refused.json()["detail"]
    assert island.read_workflow()["archived_at"] is not None


def test_a_harness_cannot_be_restored(island: Island) -> None:
    """There is no archived harness to restore: `archive` refuses one.

    The refusal is the 409 the other harness verbs give, so a client that
    tries gets the same sentence rather than a 404 implying the workflow is
    gone.
    """
    started = island.client.post(
        f"{island.agents}/{island.agent_id}/test", json={"input": SAMPLE}, headers=island.headers()
    ).json()
    harness_id = str(started["workflow_id"])
    assert island.archive_workflow(workflow_id=harness_id).status_code == 409

    refused = island.restore_workflow(workflow_id=harness_id)

    assert refused.status_code == 409, refused.text
    assert "test harness" in refused.json()["detail"]


# --------------------------------------------------------------- discovery ---


def test_archived_workflows_are_listed_only_when_asked_for(island: Island) -> None:
    """Without this parameter a workflow could only be restored by id.

    And harnesses stay out of both modes: being absent from the listing has
    nothing to do with being archived, and a flag about one must not reveal
    the other.
    """
    island.client.post(
        f"{island.agents}/{island.agent_id}/test", json={"input": SAMPLE}, headers=island.headers()
    )
    live = island.client.post(
        f"{island.workflows}", json={"name": "Still here"}, headers=island.headers()
    ).json()["workflow"]
    island.archive_workflow()

    assert [row["id"] for row in island.listed_workflows()] == [str(live["id"])]

    both = island.listed_workflows(include_archived=True)

    assert {row["id"] for row in both} == {str(live["id"]), island.workflow_id}
    assert all(row["is_test_harness"] is False for row in both), "harnesses are never listed"
    archived = next(row for row in both if row["id"] == island.workflow_id)
    assert archived["archived_at"] is not None


def test_archived_agents_are_still_listed_only_when_asked_for(island: Island) -> None:
    """Unchanged by this stage, asserted so that it stays that way."""
    island.archive_agent()

    assert island.listed_agents() == []
    assert [row["id"] for row in island.listed_agents(include_archived=True)] == [island.agent_id]


# ------------------------------------------------------------------- audit ---


async def test_each_restore_records_one_row_with_no_metadata(
    island: Island, owner_database: Database
) -> None:
    """There are no fields to name: a restore clears one column.

    `agent.restore` and `workflow.restore` are separate actions, as archiving
    is, so a digest can tell which lifecycle moved.
    """
    island.archive_agent()
    island.archive_workflow()
    island.restore_agent()
    island.restore_workflow()

    agents = await _actions(owner_database, island.workspace_id, "agent.restore")
    workflows = await _actions(owner_database, island.workspace_id, "workflow.restore")

    assert len(agents) == 1
    assert (str(agents[0].resource_id), agents[0].resource_type) == (island.agent_id, "agent")
    assert agents[0].metadata in (None, {})
    assert len(workflows) == 1
    assert (str(workflows[0].resource_id), workflows[0].resource_type) == (
        island.workflow_id,
        "workflow",
    )


async def test_a_refused_restore_records_nothing(island: Island, owner_database: Database) -> None:
    assert island.restore_agent().status_code == 404, "never archived"
    assert island.restore_workflow().status_code == 404

    assert await _actions(owner_database, island.workspace_id, "agent.restore") == []
    assert await _actions(owner_database, island.workspace_id, "workflow.restore") == []

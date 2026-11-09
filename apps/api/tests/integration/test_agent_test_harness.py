"""The test harness row: found or created once, and not edited by hand.

S3 of `agent.test`. The compiler decides what the graph is; this file is
about the row that holds it, and about the three things that row must be
able to say no to:

- **one per agent, under concurrency** - two people pressing Test at the same
  moment must end up with one workflow, not two whose published versions
  then diverge;
- **invisible in the ordinary listing** - it is a real workflow, but nobody
  drew it;
- **not editable or archivable through the ordinary workflow API** - editing
  it would change what the next test runs, and archiving it would leave a row
  no later test can insert past, because the unique index spans archived rows.

The concurrency test is the reason this is an integration file rather than a
unit one: `ON CONFLICT DO NOTHING` and the re-read that follows it are a
claim about two transactions in PostgreSQL, and nothing short of two
transactions in PostgreSQL can hold it.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain.enums import AgentInteractionMode, MemberRole
from agentworld.domain.errors import ConflictError
from agentworld.domain.orchestration import AgentTestSubject, compile_agent_test
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.definitions import WorkflowService
from helpers import Actor

pytestmark = pytest.mark.integration


class Harness:
    """A workspace, a project and one managed agent, built through the API."""

    def __init__(self, client: TestClient, actor: Actor) -> None:
        self.client = client
        self.actor = actor
        workspace = client.post("/v1/workspaces", json={"name": "Harness"}, headers=actor.headers)
        assert workspace.status_code == 201, workspace.text
        self.workspace = workspace.json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()
        agent = client.post(
            f"/v1/projects/{self.project['id']}/agents",
            json={
                "display_name": "Brief Writer",
                "agent_key": "writer",
                "interaction_mode": "managed",
                "instructions": "Be brief.",
            },
            headers=actor.headers,
        )
        assert agent.status_code == 201, agent.text
        self.agent = agent.json()

    @property
    def workspace_id(self) -> uuid.UUID:
        return uuid.UUID(self.workspace["id"])

    @property
    def project_id(self) -> uuid.UUID:
        return uuid.UUID(self.project["id"])

    @property
    def agent_id(self) -> uuid.UUID:
        return uuid.UUID(self.agent["id"])

    @property
    def scope(self) -> TenantScope:
        return TenantScope(self.workspace_id, self.project_id)

    @property
    def principal(self) -> UserPrincipal:
        return UserPrincipal(
            user_id=self.actor.user_id,
            email=self.actor.email,
            memberships={self.workspace_id: MemberRole.OWNER},
        )

    def compiled(self) -> Any:
        """What S2 would hand the writer for this agent."""
        workflow = compile_agent_test(
            AgentTestSubject(
                agent_id=self.agent_id,
                agent_key=self.agent["agent_key"],
                interaction_mode=AgentInteractionMode.MANAGED,
                display_name=self.agent["display_name"],
            )
        ).workflow
        assert workflow is not None
        return workflow


@pytest.fixture
def harness(client: TestClient, actor: Actor) -> Harness:
    return Harness(client, actor)


async def _find_or_create(factory: UnitOfWorkFactory, harness: Harness) -> tuple[Any, bool]:
    compiled = harness.compiled()
    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        return await work.workflows(harness.scope).find_or_create_test(
            agent_id=harness.agent_id,
            slug=compiled.slug,
            name=compiled.name,
            description=compiled.description,
            graph=compiled.graph,
            created_by_user_id=harness.actor.user_id,
        )


async def _count(owner_database: Database, project_id: uuid.UUID) -> int:
    async with owner_database.session() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM workflows "
                        "WHERE project_id = :p AND test_for_agent_id IS NOT NULL"
                    ),
                    {"p": project_id},
                )
            ).scalar_one()
        )


# ------------------------------------------------------------ find or create ---


async def test_the_first_test_creates_the_harness_and_its_first_version(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """One workflow, marked as the agent's, holding the compiled graph.

    The version matters as much as the workflow: `create` pairs the two
    because a workflow with no version is a row every reader has to
    special-case, and a harness is read by the ordinary publish.
    """
    workflow, created = await _find_or_create(factory, harness)

    assert created
    assert workflow.test_for_agent_id == harness.agent_id
    assert workflow.is_test_harness
    assert workflow.slug == harness.compiled().slug
    assert await _count(owner_database, harness.project_id) == 1

    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        workflows = work.workflows(harness.scope)
        # Unpublished, asked of `workflow_versions`. This first asserted
        # `workflow.current_version_id is None` - which holds whatever the
        # code does, because nothing in this codebase ever writes that
        # column. S4 found that by republishing on every test.
        assert await workflows.published_version(workflow.id) is None
        draft = await workflows.draft_of(workflow.id)
    assert draft is not None
    assert draft.version == 1
    assert draft.graph == harness.compiled().graph


async def test_the_second_test_reuses_it_and_creates_nothing(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """`created` is False, which is what tells S4 not to audit a creation."""
    first, created_first = await _find_or_create(factory, harness)
    second, created_second = await _find_or_create(factory, harness)

    assert created_first and not created_second
    assert second.id == first.id
    assert await _count(owner_database, harness.project_id) == 1

    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        versions = await work.workflows(harness.scope).draft_of(first.id)
    assert versions is not None
    assert versions.version == 1, "no second draft was opened"


async def test_concurrent_first_tests_create_exactly_one_workflow(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """Two transactions, overlapping on purpose.

    Both open before either inserts - the barrier is what makes that true
    rather than hoped - so both see no harness and both try to create one.
    The partial unique index decides it: one insert lands, the other writes
    no row and its re-read returns the winner's.

    Without the index this test is the one that fails: both inserts succeed,
    the agent has two harnesses, and which one a later test publishes depends
    on row order. It is also the test that found the conflict target: with
    the partial index named as the arbiter, the loser's insert raised on
    `workflows_project_slug_key` instead, because two first tests of one
    agent collide on both indexes at once.
    """
    both_open = asyncio.Barrier(2)
    compiled = harness.compiled()

    async def attempt() -> tuple[Any, bool]:
        async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
            workflows = work.workflows(harness.scope)
            assert await workflows.test_for(harness.agent_id) is None
            await both_open.wait()
            return await workflows.find_or_create_test(
                agent_id=harness.agent_id,
                slug=compiled.slug,
                name=compiled.name,
                description=compiled.description,
                graph=compiled.graph,
                created_by_user_id=harness.actor.user_id,
            )

    results = await asyncio.gather(attempt(), attempt())

    assert sorted(created for _, created in results) == [False, True], (
        "exactly one of the two calls must report that it created the workflow"
    )
    assert all(workflow is not None for workflow, _ in results), "the loser read nothing back"
    assert len({workflow.id for workflow, _ in results if workflow}) == 1
    assert await _count(owner_database, harness.project_id) == 1


async def test_an_agent_without_a_harness_has_no_harness(
    factory: UnitOfWorkFactory, harness: Harness
) -> None:
    """`test_for` is a question, not an assertion that one exists."""
    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        assert await work.workflows(harness.scope).test_for(harness.agent_id) is None
        assert await work.workflows(harness.scope).test_for(uuid.uuid4()) is None


# ------------------------------------------------------------------ listing ---


async def test_the_harness_is_absent_from_the_listing_but_present_by_id(
    factory: UnitOfWorkFactory, harness: Harness, client: TestClient, actor: Actor
) -> None:
    """Hidden from the list, reachable by id - publishing it needs the second.

    Asserted over HTTP as well as over the repository, because the listing a
    person sees is the one that matters and `GET /workflows` is what builds
    it.
    """
    authored = client.post(
        f"/v1/projects/{harness.project_id}/workflows",
        json={"name": "Weekly brief", "graph": {"steps": [], "edges": []}},
        headers=actor.headers,
    )
    assert authored.status_code == 201, authored.text
    created, _ = await _find_or_create(factory, harness)

    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        workflows = work.workflows(harness.scope)
        listed = await workflows.list_all()
        assert [w.slug for w in listed] == ["weekly-brief"]
        found = await workflows.get(created.id)
    assert found is not None
    assert found.id == created.id

    over_http = client.get(f"/v1/projects/{harness.project_id}/workflows", headers=actor.headers)
    assert over_http.status_code == 200
    assert [w["slug"] for w in over_http.json()] == ["weekly-brief"]


# ----------------------------------------------------------------- refusals ---


async def test_the_ordinary_draft_editor_refuses_a_harness(
    factory: UnitOfWorkFactory, harness: Harness
) -> None:
    """A 409 naming the agent, not a 404 pretending the workflow is absent.

    The caller may read this workflow; it is simply not theirs to edit. The
    message has to say which agent it belongs to, or the only way to find out
    is to go through every agent.
    """
    created, _ = await _find_or_create(factory, harness)
    service = WorkflowService(factory)

    with pytest.raises(ConflictError) as refused:
        await service.save_draft(
            harness.principal,
            CallContext(request_id="r1"),
            harness.project_id,
            created.id,
            graph={"steps": [], "edges": []},
        )
    assert "test harness" in str(refused.value)
    assert refused.value.context["agent_id"] == str(harness.agent_id)

    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        draft = await work.workflows(harness.scope).draft_of(created.id)
    assert draft is not None
    assert draft.graph == harness.compiled().graph, "the graph was not touched"


async def test_the_repository_refuses_a_harness_even_without_the_service(
    factory: UnitOfWorkFactory, harness: Harness
) -> None:
    """The floor under the service check.

    `workflow.update_draft` will one day be held by the conversational
    builder, which has no reason to know that some workflows are compiled. So
    the condition is in the statement: called directly, `save_draft` updates
    nothing and returns None.
    """
    created, _ = await _find_or_create(factory, harness)

    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        workflows = work.workflows(harness.scope)
        assert await workflows.save_draft(created.id, graph={"steps": [], "edges": []}) is None
        draft = await workflows.draft_of(created.id)
    assert draft is not None
    assert draft.graph == harness.compiled().graph


async def test_a_harness_cannot_be_archived(factory: UnitOfWorkFactory, harness: Harness) -> None:
    """Because an archived harness is one no later test can get past.

    The unique index on `test_for_agent_id` does not exclude archived rows,
    so archiving this workflow would make every later test of that agent
    collide with a row nothing uses. Refused at both layers, for the reason
    `save_draft` is.
    """
    created, _ = await _find_or_create(factory, harness)
    service = WorkflowService(factory)

    with pytest.raises(ConflictError):
        await service.archive(
            harness.principal, CallContext(request_id="r2"), harness.project_id, created.id
        )

    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        workflows = work.workflows(harness.scope)
        assert await workflows.archive(created.id) is False
        assert await workflows.test_for(harness.agent_id) is not None


async def test_an_authored_workflow_is_still_editable_and_archivable(
    factory: UnitOfWorkFactory, harness: Harness, client: TestClient, actor: Actor
) -> None:
    """The refusals are about harnesses, not about workflows.

    Both guards are one condition away from refusing everything, and a test
    that only asserted the refusals would pass if they did.
    """
    authored = client.post(
        f"/v1/projects/{harness.project_id}/workflows",
        json={"name": "Weekly brief", "graph": {"steps": [], "edges": []}},
        headers=actor.headers,
    ).json()["workflow"]
    service = WorkflowService(factory)

    saved = await service.save_draft(
        harness.principal,
        CallContext(request_id="r3"),
        harness.project_id,
        uuid.UUID(authored["id"]),
        graph={"steps": [{"step_key": "one", "kind": "transform"}], "edges": []},
    )
    assert saved.graph["steps"][0]["step_key"] == "one"

    await service.archive(
        harness.principal,
        CallContext(request_id="r4"),
        harness.project_id,
        uuid.UUID(authored["id"]),
    )
    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        assert await work.workflows(harness.scope).list_all() == []


async def test_a_slug_an_author_already_took_is_reported_rather_than_swallowed(
    factory: UnitOfWorkFactory, harness: Harness
) -> None:
    """The other conflict, told apart from the race.

    `DO NOTHING` is unqualified, so an insert that collides with a workflow
    somebody authored writes nothing either - and the re-read is what says
    which of the two happened. No harness for this agent means the slug was
    the problem, and `(None, False)` is how the repository says so without
    raising a message the service layer owns.
    """
    compiled = harness.compiled()
    async with factory.begin(visible_workspaces={harness.workspace_id}) as work:
        workflows = work.workflows(harness.scope)
        # Somebody has authored a workflow with exactly this slug. Contrived
        # - the slug carries a digest of the agent id - but it is the one
        # case the index cannot resolve, so it is the one worth stating.
        await workflows.create(
            slug=compiled.slug,
            name="Mine, actually",
            created_by_user_id=harness.actor.user_id,
        )
        found, created = await workflows.find_or_create_test(
            agent_id=harness.agent_id,
            slug=compiled.slug,
            name=compiled.name,
            graph=compiled.graph,
        )

    assert found is None
    assert created is False

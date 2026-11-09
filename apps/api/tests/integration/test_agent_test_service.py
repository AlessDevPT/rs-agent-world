"""`agent.test` as an application service: four actions, four audit rows.

S4. The compiler decides the graph and the repository owns the row; this file
is about the lifecycle that joins them to the *existing* publish and run, and
about what that lifecycle records.

There is no HTTP route yet, so the service is called the way S5's route will
call it: with a principal a request would have built. The authorization and
isolation tests here are the ones that would otherwise be route tests, and
they matter more without a route than with one - nothing else stands between
a caller and this method.

No worker runs in this file. `start` ends with `advance_once`, so a test run
reaches a `pending` dispatch and stops there: that is enough to show the
orchestrator planned the work, and the provider call is S6's.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_agent_test_harness import Harness

from agentworld.domain.enums import MemberRole
from agentworld.domain.errors import (
    AgentNotTestableError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.agent_test import AgentTestService
from agentworld.services.context import CallContext
from agentworld.services.executions import ExecutionService
from helpers import Actor, seed_membership, seed_user

pytestmark = pytest.mark.integration

SAMPLE = "Summarise the fintech funding news."


@pytest.fixture
def harness(client: TestClient, actor: Actor) -> Harness:
    """A project with one managed agent - and deliberately no model profile.

    Nothing in this file needs one: compiling, publishing and planning a
    dispatch all happen without a credential, which is C19 and is asserted
    here as well as stated.
    """
    return Harness(client, actor)


def service(factory: UnitOfWorkFactory) -> AgentTestService:
    return AgentTestService(factory, executions=ExecutionService(factory))


def principal(harness: Harness, role: MemberRole = MemberRole.OWNER) -> UserPrincipal:
    return UserPrincipal(
        user_id=harness.actor.user_id,
        email=harness.actor.email,
        memberships={harness.workspace_id: role},
    )


async def run_test(
    factory: UnitOfWorkFactory,
    harness: Harness,
    *,
    role: MemberRole = MemberRole.OWNER,
    who: UserPrincipal | None = None,
    agent_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    request_id: str = "req-1",
    tested_by: AgentTestService | None = None,
) -> uuid.UUID:
    return await (tested_by or service(factory)).run(
        who or principal(harness, role),
        CallContext(request_id=request_id),
        project_id or harness.project_id,
        agent_id or harness.agent_id,
        sample=SAMPLE,
    )


# ------------------------------------------------------------- reading rows ---


async def _rows(owner_database: Database, sql: str, **binds: Any) -> list[Any]:
    async with owner_database.session() as session:
        return (await session.execute(text(sql), binds)).all()


async def _harnesses(owner_database: Database, project_id: uuid.UUID) -> list[Any]:
    """The project's test harnesses, with the version a run would execute.

    `published` comes from `workflow_versions` rather than from
    `workflows.current_version_id`: **nothing writes that column**, so it is
    NULL for every published workflow in the system and an assertion on it
    would hold whatever the code did.
    """
    return await _rows(
        owner_database,
        "SELECT w.id, w.slug, w.test_for_agent_id, ("
        "  SELECT v.id FROM workflow_versions v"
        "   WHERE v.workflow_id = w.id AND v.published_at IS NOT NULL"
        "   ORDER BY v.version DESC LIMIT 1"
        ") AS published FROM workflows w "
        "WHERE w.project_id = :p AND w.test_for_agent_id IS NOT NULL",
        p=project_id,
    )


async def _audit(owner_database: Database, workspace_id: uuid.UUID) -> list[Any]:
    return await _rows(
        owner_database,
        "SELECT action, resource_type, resource_id, request_id, metadata FROM audit_log "
        "WHERE workspace_id = :w ORDER BY created_at, id",
        w=workspace_id,
    )


async def _executions(owner_database: Database, project_id: uuid.UUID) -> list[Any]:
    return await _rows(
        owner_database,
        "SELECT id, workflow_id, workflow_version_id, status, input, initiator_label "
        "FROM workflow_executions WHERE project_id = :p ORDER BY created_at",
        p=project_id,
    )


async def _dispatches(owner_database: Database, project_id: uuid.UUID) -> list[Any]:
    return await _rows(
        owner_database,
        "SELECT kind, status, side_effect_class, max_attempts, instruction "
        "FROM dispatches WHERE project_id = :p",
        p=project_id,
    )


# ---------------------------------------------------------------- first test ---


async def test_the_first_test_creates_publishes_and_runs_one_harness(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """The whole lifecycle, asserted on rows rather than on return values.

    One harness, published; one execution bound to its published version and
    carrying the sample input; one pending dispatch the orchestrator planned
    from that version. Nothing in this service wrote the dispatch - `start`
    ends with `advance_once`, and that is the only thing that can.
    """
    execution_id = await run_test(factory, harness)

    harnesses = await _harnesses(owner_database, harness.project_id)
    assert len(harnesses) == 1
    assert harnesses[0].published is not None, "the first test publishes it"
    assert uuid.UUID(str(harnesses[0].test_for_agent_id)) == harness.agent_id

    executions = await _executions(owner_database, harness.project_id)
    assert len(executions) == 1
    assert executions[0].id == execution_id
    assert executions[0].workflow_id == harnesses[0].id
    assert executions[0].workflow_version_id == harnesses[0].published
    assert executions[0].input == {"input": SAMPLE}
    assert executions[0].initiator_label == harness.actor.email

    dispatches = await _dispatches(owner_database, harness.project_id)
    assert len(dispatches) == 1
    assert dispatches[0].kind == "managed_agent_turn"
    assert dispatches[0].status == "pending"
    # `read`, which is what `advance` gives a managed turn - the same class a
    # hand-authored agent step gets, and never `consequential`.
    assert dispatches[0].side_effect_class == "read"
    assert dispatches[0].instruction["agent_key"] == "writer"
    assert dispatches[0].instruction["input"] == {"input": SAMPLE}


async def test_a_test_dispatch_is_the_same_shape_as_an_ordinary_one(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database, actor: Actor
) -> None:
    """Nothing about the row says "test", because nothing about it is.

    A hand-authored single-step workflow over the same agent is published and
    run through the ordinary routes, and its dispatch is compared field for
    field with the test's. If a later change gave test runs their own kind,
    their own effect class or their own retry budget, this is what would
    fail.
    """
    await run_test(factory, harness)

    authored = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows",
        json={
            "name": "By hand",
            "graph": {
                "steps": [
                    {
                        "step_key": "writer",
                        "kind": "agent_task",
                        "agent_key": "writer",
                        "input_mapping": {"input": "$.input.input"},
                    }
                ],
                "edges": [],
            },
        },
        headers=actor.headers,
    ).json()["workflow"]
    published = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows/{authored['id']}/publish",
        headers=actor.headers,
    )
    assert published.status_code == 201, published.text
    ran = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows/{authored['id']}/run",
        json={"input": {"input": SAMPLE}},
        headers=actor.headers,
    )
    assert ran.status_code == 202, ran.text

    dispatches = await _dispatches(owner_database, harness.project_id)
    assert len(dispatches) == 2
    compared = {
        (row.kind, row.status, row.side_effect_class, row.max_attempts) for row in dispatches
    }
    assert len(compared) == 1, compared
    assert {row.instruction["input"]["input"] for row in dispatches} == {SAMPLE}


# ---------------------------------------------------------------- later tests ---


async def test_a_second_test_reuses_the_harness_and_adds_one_execution(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """No second workflow, no second version, no second publish."""
    first = await run_test(factory, harness, request_id="req-1")
    second = await run_test(factory, harness, request_id="req-2")

    assert first != second
    harnesses = await _harnesses(owner_database, harness.project_id)
    assert len(harnesses) == 1

    versions = await _rows(
        owner_database,
        "SELECT version, published_at FROM workflow_versions WHERE workflow_id = :w "
        "ORDER BY version",
        w=harnesses[0].id,
    )
    # Version 1 published by the first test, and the fresh draft `publish`
    # always opens behind it. A second publish would have made a third.
    assert [row.version for row in versions] == [1, 2]
    assert [row.published_at is not None for row in versions] == [True, False]

    executions = await _executions(owner_database, harness.project_id)
    assert {row.id for row in executions} == {first, second}
    assert {row.workflow_version_id for row in executions} == {harnesses[0].published}


async def test_changed_instructions_are_picked_up_without_republishing(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database, actor: Actor
) -> None:
    """Why one reusable harness is correct and not merely cheap.

    The published version names the agent; `StepRunner._plan` reads the agent
    itself when the dispatch is performed. So editing instructions changes
    what the next test does, with no republish and no new version - which is
    the property that makes a cached harness honest.
    """
    await run_test(factory, harness, request_id="req-1")
    edited = harness.client.patch(
        f"/v1/projects/{harness.project_id}/agents/{harness.agent_id}",
        json={"instructions": "Answer in exactly one word."},
        headers=actor.headers,
    )
    assert edited.status_code == 200, edited.text

    await run_test(factory, harness, request_id="req-2")

    harnesses = await _harnesses(owner_database, harness.project_id)
    assert len(harnesses) == 1
    versions = await _rows(
        owner_database,
        "SELECT count(*) AS n FROM workflow_versions WHERE workflow_id = :w",
        w=harnesses[0].id,
    )
    assert versions[0].n == 2, "the edit did not provoke a new version"
    agent = await _rows(
        owner_database, "SELECT instructions FROM agents WHERE id = :a", a=harness.agent_id
    )
    assert agent[0].instructions == "Answer in exactly one word."


# --------------------------------------------------------------------- audit ---


async def test_the_first_test_records_four_actions_under_one_request(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """Each real action under its own name, correlated by `request_id`.

    `workflow.publish` and `workflow.run` are in `SENSITIVE_ACTIONS`, so
    folding them into a single `agent.test` row would hide two sensitive
    actions from the audit digest. Recording only them would lose what the
    person asked for.
    """
    await run_test(factory, harness, request_id="the-first-test")

    rows = await _audit(owner_database, harness.workspace_id)
    mine = [row for row in rows if row.request_id == "the-first-test"]
    assert [row.action for row in mine] == [
        "workflow.create",
        "agent.test",
        "workflow.publish",
        "workflow.run",
    ], [row.action for row in rows]

    created, tested = mine[0], mine[1]
    assert created.resource_type == "workflow"
    assert created.metadata["test_for_agent_id"] == str(harness.agent_id)
    assert tested.resource_type == "agent"
    assert uuid.UUID(str(tested.resource_id)) == harness.agent_id
    assert tested.metadata["step_key"] == "writer"
    assert tested.metadata["created_harness"] is True


async def test_a_later_test_records_the_run_and_the_test_but_not_a_creation(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """Nothing was created or published, so nothing claims to have been."""
    await run_test(factory, harness, request_id="first")
    await run_test(factory, harness, request_id="second")

    rows = await _audit(owner_database, harness.workspace_id)
    again = [row.action for row in rows if row.request_id == "second"]
    assert again == ["agent.test", "workflow.run"]
    tested = next(row for row in rows if row.request_id == "second" and row.action == "agent.test")
    assert tested.metadata["created_harness"] is False


# ------------------------------------------------------------- authorization ---


async def test_a_viewer_cannot_test_an_agent(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """`agent.test` is `member+`, and it spends money - that is why."""
    with pytest.raises(PermissionDeniedError):
        await run_test(factory, harness, role=MemberRole.VIEWER)

    assert await _harnesses(owner_database, harness.project_id) == []
    assert await _executions(owner_database, harness.project_id) == []


async def test_authorization_is_checked_before_the_agent_is_read(
    factory: UnitOfWorkFactory, harness: Harness
) -> None:
    """The ordering, asserted the only way it can be.

    A viewer naming an agent id that does not exist must be told they may
    not test agents, not that this one is missing. If the read came first the
    error would be `NotFoundError`, and the service would be answering a
    question the caller was never allowed to ask.
    """
    with pytest.raises(PermissionDeniedError):
        await run_test(factory, harness, role=MemberRole.VIEWER, agent_id=uuid.uuid4())


async def test_a_stranger_gets_nothing_found(
    factory: UnitOfWorkFactory, harness: Harness, auth: Any, owner_database: Database
) -> None:
    """Not 403: a project they cannot see must not be distinguishable.

    The principal is built by hand with a membership of a workspace that is
    not this one, which is what a stranger's resolved principal looks like.
    """
    outsider = Actor(auth, "stranger@example.com")
    await seed_user(owner_database, outsider.user_id, outsider.email)
    elsewhere = UserPrincipal(
        user_id=outsider.user_id,
        email=outsider.email,
        memberships={uuid.uuid4(): MemberRole.OWNER},
    )
    with pytest.raises(NotFoundError):
        await run_test(factory, harness, who=elsewhere)


async def test_an_agent_in_another_project_is_not_found(
    factory: UnitOfWorkFactory,
    harness: Harness,
    client: TestClient,
    actor: Actor,
    owner_database: Database,
) -> None:
    """A member of both projects still cannot test across them.

    The agent id is real and the caller may read it in its own project; named
    through the wrong project it is simply not there, because every read in
    this service is scoped to the project in the path.
    """
    other = client.post(
        f"/v1/workspaces/{harness.workspace['id']}/projects",
        json={"name": "Elsewhere"},
        headers=actor.headers,
    ).json()

    with pytest.raises(NotFoundError):
        await run_test(factory, harness, project_id=uuid.UUID(other["id"]))

    assert await _harnesses(owner_database, uuid.UUID(other["id"])) == []


async def test_a_member_may_test(
    factory: UnitOfWorkFactory,
    harness: Harness,
    auth: Any,
    owner_database: Database,
) -> None:
    """The floor of the role matrix, not only the ceiling.

    Every other authorization test here asserts a refusal, and all of them
    would pass if this service refused everybody.
    """
    member = Actor(auth, "member@example.com")
    await seed_user(owner_database, member.user_id, member.email)
    await seed_membership(owner_database, harness.workspace_id, member.user_id, MemberRole.MEMBER)
    who = UserPrincipal(
        user_id=member.user_id,
        email=member.email,
        memberships={harness.workspace_id: MemberRole.MEMBER},
    )

    execution_id = await run_test(factory, harness, who=who)

    executions = await _executions(owner_database, harness.project_id)
    assert [row.id for row in executions] == [execution_id]
    assert executions[0].initiator_label == member.email


# ----------------------------------------------------------------- refusals ---


async def test_an_observed_agent_is_refused_and_nothing_is_created(
    factory: UnitOfWorkFactory,
    harness: Harness,
    client: TestClient,
    actor: Actor,
    owner_database: Database,
) -> None:
    """A 422 carrying the compiler's finding, and no harness behind it."""
    observed = client.post(
        f"/v1/projects/{harness.project_id}/agents",
        json={
            "display_name": "Watcher",
            "agent_key": "watcher",
            "interaction_mode": "observed",
        },
        headers=actor.headers,
    ).json()

    with pytest.raises(AgentNotTestableError) as refused:
        await run_test(factory, harness, agent_id=uuid.UUID(observed["id"]))

    assert [f["code"] for f in refused.value.findings] == ["observed_agent"]
    assert await _harnesses(owner_database, harness.project_id) == []
    assert await _executions(owner_database, harness.project_id) == []


async def test_an_archived_agent_is_refused_with_a_reason_not_a_404(
    factory: UnitOfWorkFactory,
    harness: Harness,
    client: TestClient,
    actor: Actor,
    owner_database: Database,
) -> None:
    """Archived, and said so.

    `AgentService.get` reads an archived agent by id, so this service does
    too: the agent is visible to this caller, and "it is archived, restore
    it" is a better answer than pretending it is gone.
    """
    archived = client.delete(
        f"/v1/projects/{harness.project_id}/agents/{harness.agent_id}",
        headers=actor.headers,
    )
    assert archived.status_code in (200, 204), archived.text

    with pytest.raises(AgentNotTestableError) as refused:
        await run_test(factory, harness)

    assert [f["code"] for f in refused.value.findings] == ["agent_archived"]
    assert await _harnesses(owner_database, harness.project_id) == []


async def test_an_agent_that_does_not_exist_is_not_found(
    factory: UnitOfWorkFactory, harness: Harness
) -> None:
    with pytest.raises(NotFoundError):
        await run_test(factory, harness, agent_id=uuid.uuid4())


async def test_a_slug_an_author_already_took_is_a_conflict_not_a_second_harness(
    factory: UnitOfWorkFactory, harness: Harness, actor: Actor, owner_database: Database
) -> None:
    """The one conflict the index cannot resolve, reported as itself.

    Contrived - the slug carries a digest of the agent id - but it is the
    only way `find_or_create_test` can return no workflow, and a caller who
    hits it has to be told which slug to go and free.
    """
    taken = harness.compiled().slug
    authored = harness.client.post(
        f"/v1/projects/{harness.project_id}/workflows",
        json={"name": "Mine, actually", "slug": taken, "graph": {"steps": [], "edges": []}},
        headers=actor.headers,
    )
    assert authored.status_code == 201, authored.text

    with pytest.raises(ConflictError) as refused:
        await run_test(factory, harness)

    assert refused.value.context["slug"] == taken
    assert await _harnesses(owner_database, harness.project_id) == []


# -------------------------------------------------------- resuming a failure ---


class PublishFails(ExecutionService):
    """An `ExecutionService` whose first publish raises, and no more.

    Induced rather than imagined: the lifecycle commits the harness before
    publishing it, so a publish that fails has to leave something behind,
    and the only honest way to know what is to make one fail.
    """

    def __init__(self, factory: UnitOfWorkFactory) -> None:
        super().__init__(factory)
        self.attempts = 0

    async def publish(self, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("the publish failed")
        return await super().publish(*args, **kwargs)


async def test_a_failed_publication_leaves_one_unpublished_harness(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """The recoverable state: the row is there, unpublished, and alone."""
    executions = PublishFails(factory)
    tester = AgentTestService(factory, executions=executions)

    with pytest.raises(RuntimeError):
        await run_test(factory, harness, tested_by=tester)

    harnesses = await _harnesses(owner_database, harness.project_id)
    assert len(harnesses) == 1
    assert harnesses[0].published is None
    assert await _executions(owner_database, harness.project_id) == []


async def test_the_next_test_publishes_the_harness_the_failure_left(
    factory: UnitOfWorkFactory, harness: Harness, owner_database: Database
) -> None:
    """And creates no second one, which is the whole point of the index.

    The retry goes through the same service instance, so its second publish
    is a real one. What matters is that it publishes *that* harness: the
    workflow id after the successful test is the one the failed attempt
    created.
    """
    executions = PublishFails(factory)
    tester = AgentTestService(factory, executions=executions)

    with pytest.raises(RuntimeError):
        await run_test(factory, harness, tested_by=tester, request_id="failed")
    failed = (await _harnesses(owner_database, harness.project_id))[0]

    execution_id = await run_test(factory, harness, tested_by=tester, request_id="recovered")

    harnesses = await _harnesses(owner_database, harness.project_id)
    assert len(harnesses) == 1
    assert harnesses[0].id == failed.id
    assert harnesses[0].published is not None
    assert [row.id for row in await _executions(owner_database, harness.project_id)] == [
        execution_id
    ]

    rows = await _audit(owner_database, harness.workspace_id)
    # The creation was audited by the attempt that made the row, and the
    # recovery does not claim to have created it again.
    assert [row.action for row in rows if row.request_id == "failed"] == [
        "workflow.create",
        "agent.test",
    ]
    assert [row.action for row in rows if row.request_id == "recovered"] == [
        "agent.test",
        "workflow.publish",
        "workflow.run",
    ]

"""A compiled delegation, through the ordinary lifecycle and the real worker.

`03-API-AND-FRONTEND.md` §7.1: a team "is not a new entity and not a special
execution path... it runs on the same orchestrator as everything else". The
unit tests prove the compiler emits the right graph. **This file proves the
graph is not special** - that after compiling, every remaining step is the
one a hand-drawn workflow takes:

    compile -> unpublished draft -> POST /validate -> POST /publish
            -> POST /run -> DispatchWorker -> advance -> second dispatch

Nothing here inserts a dispatch, calls `advance()` or touches a reducer. The
writer's dispatch can only exist if the researcher's completion really
produced it, through the outbox, in the second of `report`'s two
transactions. That is what makes "same orchestrator" an assertion rather than
a claim.

There is deliberately **no HTTP route for compiling** in this phase, so the
service is called the way `services/builder.py` will call it: with a
principal a request would have built, through the same two `authorize` calls.
The authorization and isolation tests here are the ones that would otherwise
have been route tests, and they matter more without a route than with one -
nothing else stands between a caller and this method.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_dispatch_worker import FakeProvider, Scenario, _worker

from agentworld.domain.enums import MemberRole
from agentworld.domain.errors import (
    ConflictError,
    DelegationNotCompilableError,
    NotFoundError,
    PermissionDeniedError,
)
from agentworld.domain.orchestration import DelegationCode
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.definitions import WorkflowService
from helpers import Actor

pytestmark = pytest.mark.integration

#: Distinct on purpose. The researcher's answer has to be recognisable in the
#: writer's request, or the handoff assertion proves nothing.
FINDINGS = "Acme raised a round and Globex cut prices."
BRIEF = "Two rivals moved this week."


def _answer(text_: str) -> dict[str, Any]:
    return {
        "id": "msg_01",
        "model": "test-model",
        "content": [{"type": "text", "text": text_}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


@pytest.fixture
async def provider() -> Any:
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


# ------------------------------------------------------------- the fixtures ---


def _writer(scenario: Scenario, *, agent_key: str = "writer", name: str = "Writer") -> str:
    """A second managed agent sharing the scenario's model profile."""
    detail = scenario.client.get(
        f"/v1/projects/{scenario.project['id']}/agents/{scenario.agent['id']}",
        headers=scenario.actor.headers,
    )
    assert detail.status_code == 200, detail.text
    profile_id = detail.json()["model_profile_id"]
    assert profile_id, "the scenario's researcher has no model profile to share"

    created = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/agents",
        json={
            "display_name": name,
            "agent_key": agent_key,
            "interaction_mode": "managed",
            "instructions": "Write one sentence.",
        },
        headers=scenario.actor.headers,
    )
    assert created.status_code == 201, created.text
    configured = scenario.client.patch(
        f"/v1/projects/{scenario.project['id']}/agents/{created.json()['id']}",
        json={"model_profile_id": profile_id},
        headers=scenario.actor.headers,
    )
    assert configured.status_code == 200, configured.text
    return str(created.json()["id"])


def _delegate(scenario: Scenario, source_id: str, target_id: str) -> uuid.UUID:
    """A `delegate` connection, created through the public API.

    Through the API rather than by inserting a row: the claim is that a
    delegation somebody drew on the island compiles, which means the request
    schema has to accept it too.
    """
    created = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/connections",
        json={
            "kind": "delegate",
            "source_kind": "agent",
            "source_id": source_id,
            "target_kind": "agent",
            "target_id": target_id,
        },
        headers=scenario.actor.headers,
    )
    assert created.status_code == 201, created.text
    return uuid.UUID(str(created.json()["id"]))


def _principal(actor: Actor, workspace_id: uuid.UUID, role: MemberRole) -> UserPrincipal:
    """The actor as the services see them, for the call with no route."""
    return UserPrincipal(user_id=actor.user_id, email=actor.email, memberships={workspace_id: role})


async def _compile(
    factory: UnitOfWorkFactory,
    scenario: Scenario,
    connection_id: uuid.UUID,
    *,
    role: MemberRole = MemberRole.OWNER,
    project_id: uuid.UUID | None = None,
    principal: UserPrincipal | None = None,
) -> Any:
    service = WorkflowService(factory)
    return await service.compile_delegation(
        principal or _principal(scenario.actor, scenario.workspace_id, role),
        CallContext(request_id="test-compile"),
        project_id or scenario.project_id,
        connection_id,
    )


# ------------------------------------------------------------ reading rows ---


async def _steps(owner_database: Database, execution_id: uuid.UUID) -> dict[str, Any]:
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT step_key, status, output FROM execution_steps "
                    "WHERE execution_id = :id ORDER BY step_key"
                ),
                {"id": str(execution_id)},
            )
        ).all()
    return {str(row.step_key): row for row in rows}


async def _dispatches(owner_database: Database, execution_id: uuid.UUID) -> list[Any]:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT d.kind, d.status, d.instruction, s.step_key "
                    "FROM dispatches d JOIN execution_steps s ON s.id = d.execution_step_id "
                    "WHERE d.execution_id = :id ORDER BY d.created_at, s.step_key"
                ),
                {"id": str(execution_id)},
            )
        ).all()


async def _version_steps(owner_database: Database, workflow_id: Any) -> list[Any]:
    """Frozen step keys, per published version, as the publish wrote them."""
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT v.version, s.step_key, s.kind, s.input_mapping "
                    "FROM workflow_steps s "
                    "JOIN workflow_versions v ON v.id = s.workflow_version_id "
                    "WHERE v.workflow_id = :id ORDER BY v.version, s.step_key"
                ),
                {"id": str(workflow_id)},
            )
        ).all()


async def _audit(owner_database: Database, workspace_id: uuid.UUID) -> list[Any]:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT action, resource_type, resource_id, metadata FROM audit_log "
                    "WHERE workspace_id = :id ORDER BY created_at, id"
                ),
                {"id": str(workspace_id)},
            )
        ).all()


def _sent_text(request: dict[str, Any]) -> str:
    messages = request.get("messages") or []
    assert messages, f"no messages in the captured request: {request}"
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    return " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))


# ------------------------------------------------------ compiling produces a draft ---


async def test_compiling_produces_an_unpublished_draft_and_nothing_else(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """A draft, not a published version, and not a running anything.

    §7.1 has the builder leave workflows unpublished, and this path is held
    to the same rule: publishing is a separate, authorized act somebody takes
    after looking at what was compiled. So the workflow comes back disabled,
    with no current version, and its first draft holds the graph.
    """
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)

    workflow, draft = await _compile(factory, scenario, connection_id)

    assert workflow.slug == "researcher-then-writer"
    assert workflow.name == "Researcher then Writer"
    assert workflow.enabled is False
    assert workflow.current_version_id is None
    assert draft.version == 1
    assert draft.is_published is False
    assert draft.graph == {
        "steps": [
            {
                "step_key": "researcher",
                "kind": "agent_task",
                "agent_key": "researcher",
                "input_mapping": {"input": "$.input.input"},
            },
            {
                "step_key": "writer",
                "kind": "agent_task",
                "agent_key": "writer",
                "input_mapping": {"input": "$.steps.researcher.output.text"},
            },
        ],
        "edges": [{"from": "researcher", "to": "writer"}],
    }

    # Nothing was published and nothing was started.
    assert await _version_steps(owner_database, workflow.id) == []
    async with owner_database.session() as session:
        executions = (
            await session.execute(
                text("SELECT count(*) FROM workflow_executions WHERE project_id = :id"),
                {"id": str(scenario.project_id)},
            )
        ).scalar_one()
    assert executions == 0


async def test_compiling_is_audited_with_the_connection_and_the_step_keys(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Who built this team, from which delegation, and with which step keys.

    The step keys are in the row because they are what a published version
    freezes: after the fact, "which agents were in this team" is answered by
    the audit row even if the workflow was archived.
    """
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)
    workflow, _ = await _compile(factory, scenario, connection_id)

    rows = list(await _audit(owner_database, scenario.workspace_id))
    compiled = [row for row in rows if row.action == "workflow.compile_delegation"]
    assert len(compiled) == 1, [row.action for row in rows]
    assert compiled[0].resource_type == "workflow"
    assert uuid.UUID(str(compiled[0].resource_id)) == workflow.id
    assert compiled[0].metadata["connection_id"] == str(connection_id)
    assert compiled[0].metadata["steps"] == ["researcher", "writer"]


# ---------------------------------------- the existing publication lifecycle ---


async def test_the_compiled_draft_validates_publishes_and_keeps_its_step_keys(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Published through the ordinary route, and the keys survive it.

    Step keys are the one thing a published version makes permanent: every
    `$.steps.<key>` reference in it is resolved against them, and a running
    execution is pinned to that version. So the assertion is not only that
    publishing works, but that the second published version froze the *same*
    keys - a compiler that derived keys from anything renameable would drift
    here.
    """
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)
    workflow, _ = await _compile(factory, scenario, connection_id)
    base = f"/v1/projects/{scenario.project['id']}/workflows/{workflow.id}"

    report = scenario.client.post(f"{base}/validate", headers=scenario.actor.headers)
    assert report.status_code == 200, report.text
    assert report.json()["findings"] == [], report.json()
    assert report.json()["publishable"] is True

    first = scenario.client.post(f"{base}/publish", headers=scenario.actor.headers)
    assert first.status_code == 201, first.text

    # The next draft carries the same graph forward, so publishing it again
    # freezes the same keys under a new version.
    draft = scenario.client.get(f"{base}/draft", headers=scenario.actor.headers)
    assert draft.status_code == 200, draft.text
    saved = scenario.client.put(
        f"{base}/draft",
        json={"graph": draft.json()["graph"]},
        headers=scenario.actor.headers,
    )
    assert saved.status_code == 200, saved.text
    second = scenario.client.post(f"{base}/publish", headers=scenario.actor.headers)
    assert second.status_code == 201, second.text

    frozen = await _version_steps(owner_database, workflow.id)
    by_version: dict[int, list[Any]] = {}
    for row in frozen:
        by_version.setdefault(int(row.version), []).append(row)
    assert sorted(by_version) == [1, 2], f"expected two published versions: {sorted(by_version)}"
    for version, rows in by_version.items():
        assert [row.step_key for row in rows] == ["researcher", "writer"], (
            f"version {version} froze different step keys: {[r.step_key for r in rows]}"
        )
        assert {row.kind for row in rows} == {"agent_task"}
    assert by_version[1][1].input_mapping == {"input": "$.steps.researcher.output.text"}
    assert by_version[2][1].input_mapping == by_version[1][1].input_mapping


# ----------------------------------------------- the same orchestrator, really ---


async def test_a_compiled_team_executes_through_the_existing_worker(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    provider: FakeProvider,
) -> None:
    """The sentence M1 asks for, on a workflow nobody drew.

    Every step after compiling is the production path: publish, `POST /run`,
    `DispatchWorker.run_once`. The writer's dispatch is never inserted here -
    it exists only because the researcher's completion advanced the execution
    through the outbox - and the writer's prompt is read back out of the
    provider request, so a correct-looking definition cannot pass for a
    correct handoff.
    """
    provider.responses = [_answer(FINDINGS), _answer(BRIEF)]
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)
    workflow, _ = await _compile(factory, scenario, connection_id)
    base = f"/v1/projects/{scenario.project['id']}/workflows/{workflow.id}"

    published = scenario.client.post(f"{base}/publish", headers=scenario.actor.headers)
    assert published.status_code == 201, published.text

    # The entry contract: a compiled team takes `{"input": {"input": ...}}`.
    started = scenario.client.post(
        f"{base}/run",
        json={"input": {"input": "look into fintech"}},
        headers=scenario.actor.headers,
    )
    assert started.status_code == 202, started.text
    execution_id = uuid.UUID(started.json()["id"])

    worker = _worker(factory, provider)

    queued = await _dispatches(owner_database, execution_id)
    assert [row.step_key for row in queued] == ["researcher"], (
        f"expected one dispatch for the upstream step: {[r.step_key for r in queued]}"
    )

    first = await worker.run_once()
    assert first is not None, "the upstream dispatch was never claimed"
    assert first.step_key == "researcher"

    after = await _dispatches(owner_database, execution_id)
    assert [row.step_key for row in after] == ["researcher", "writer"], (
        "no dispatch was emitted for the downstream step, so the compiled workflow "
        f"did not advance: {[r.step_key for r in after]}"
    )
    # The handoff, as the orchestrator persisted it: the researcher's actual
    # words, under the contract's field name, not the expression.
    assert after[1].instruction["input"] == {"input": FINDINGS}, (
        f"the downstream instruction does not carry the upstream output: "
        f"{after[1].instruction.get('input')!r}"
    )

    second = await worker.run_once()
    assert second is not None, "the downstream dispatch was never claimed"
    assert second.step_key == "writer"
    assert len(provider.captured) == 2, "both turns did not reach the provider"
    assert FINDINGS in _sent_text(provider.captured[1]), (
        f"the writer's prompt does not contain the researcher's output: "
        f"{_sent_text(provider.captured[1])!r}"
    )

    steps = await _steps(owner_database, execution_id)
    assert {key: row.status for key, row in steps.items()} == {
        "researcher": "succeeded",
        "writer": "succeeded",
    }
    assert steps["writer"].output["text"] == BRIEF
    async with owner_database.session() as session:
        status = (
            await session.execute(
                text("SELECT status FROM workflow_executions WHERE id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()
    assert status == "completed"

    done = await _dispatches(owner_database, execution_id)
    assert len(done) == 2 and {row.kind for row in done} == {"managed_agent_turn"}
    assert len(provider.captured) == 2, "the provider was called more than twice"


async def test_a_compiled_team_is_indistinguishable_from_a_hand_drawn_one(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """No row anywhere says "team".

    The guarantee behind "not a new entity": after compiling and publishing,
    the only trace is a workflow, a version, two steps and an edge - the same
    rows the hand-written two-step test produces. If a later change gave
    teams a marker of their own, this is where it would show up.
    """
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)
    workflow, _ = await _compile(factory, scenario, connection_id)
    published = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/workflows/{workflow.id}/publish",
        headers=scenario.actor.headers,
    )
    assert published.status_code == 201, published.text

    async with owner_database.session() as session:
        edges = (
            await session.execute(
                text(
                    "SELECT e.condition FROM workflow_edges e "
                    "JOIN workflow_versions v ON v.id = e.workflow_version_id "
                    "WHERE v.workflow_id = :id"
                ),
                {"id": str(workflow.id)},
            )
        ).all()
        tables = {
            str(row[0])
            for row in (
                await session.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).all()
        }
    assert len(edges) == 1 and edges[0].condition is None
    assert not [name for name in tables if "team" in name], (
        f"a team table exists, so a team is now an entity: {sorted(tables)}"
    )


# ---------------------------------------------------- authorization and tenancy ---


async def test_a_viewer_may_not_compile_a_delegation(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """Reading connections is `viewer`; creating a workflow is `member`.

    A viewer passes the first check and must still be refused, which is why
    the service makes both calls rather than assuming the read implies the
    write.
    """
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)

    with pytest.raises(PermissionDeniedError):
        await _compile(factory, scenario, connection_id, role=MemberRole.VIEWER)


async def test_a_member_may_compile_a_delegation(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """The floor, asserted so the viewer test cannot pass by over-restriction."""
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)

    workflow, _ = await _compile(factory, scenario, connection_id, role=MemberRole.MEMBER)
    assert workflow.slug == "researcher-then-writer"


async def test_a_stranger_cannot_compile_anything_in_this_project(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """404, not 403: the project is invisible, so its existence is not leaked."""
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)
    stranger = UserPrincipal(user_id=uuid.uuid4(), email="nobody@example.com", memberships={})

    with pytest.raises(NotFoundError):
        await _compile(factory, scenario, connection_id, principal=stranger)


async def test_a_connection_from_another_project_is_not_found(
    client: TestClient, actor: Actor, provider: FakeProvider, factory: UnitOfWorkFactory
) -> None:
    """Project isolation, with the caller a member of both workspaces.

    The interesting case: permission is not the thing stopping this. The
    repository is scoped to the project named in the call, so another
    project's connection id simply has no row - and compiling cannot be used
    to reach across projects by passing an id from one and a project from
    another.
    """
    here = Scenario(client, actor, provider, workspace_name="Here")
    there = Scenario(client, actor, provider, workspace_name="There")
    their_writer = _writer(there)
    their_connection = _delegate(there, str(there.agent["id"]), their_writer)

    both = UserPrincipal(
        user_id=actor.user_id,
        email=actor.email,
        memberships={here.workspace_id: MemberRole.OWNER, there.workspace_id: MemberRole.OWNER},
    )
    with pytest.raises(NotFoundError):
        await _compile(factory, here, their_connection, principal=both)

    # And it does compile in the project that owns it, so the refusal above
    # is about the project and not about the connection being unusable.
    workflow, _ = await _compile(factory, there, their_connection, principal=both)
    assert workflow.slug == "researcher-then-writer"


# ------------------------------------------------------------- refusals reach here ---


async def test_an_unsupported_shape_is_refused_with_findings_and_creates_nothing(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """The compiler's refusal, arriving through the service as findings.

    A fan-out built from two real connections, so this covers the whole
    path - the service gathers every delegation in the project, the pure
    function judges the shape, and the refusal carries reasons a caller can
    act on. And no workflow is created on the way to refusing.
    """
    writer_id = _writer(scenario)
    editor_id = _writer(scenario, agent_key="editor", name="Editor")
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)
    _delegate(scenario, str(scenario.agent["id"]), editor_id)

    with pytest.raises(DelegationNotCompilableError) as raised:
        await _compile(factory, scenario, connection_id)

    codes = {finding["code"] for finding in raised.value.findings}
    assert codes == {DelegationCode.FAN_OUT.value}, raised.value.findings
    assert all(finding["detail"] for finding in raised.value.findings)

    async with owner_database.session() as session:
        workflows = (
            await session.execute(
                text("SELECT count(*) FROM workflows WHERE project_id = :id"),
                {"id": str(scenario.project_id)},
            )
        ).scalar_one()
    # One: the scenario's own single-step workflow, and nothing compiled.
    assert workflows == 1


async def test_compiling_the_same_delegation_twice_conflicts(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """The second compile is a conflict, not a second workflow.

    The slug is derived from the two agent keys, so it is the same slug - and
    a suffixed `researcher-then-writer-2` would be two teams for one
    delegation, which is the ambiguity the fan-out check exists to prevent.
    """
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)
    await _compile(factory, scenario, connection_id)

    with pytest.raises(ConflictError):
        await _compile(factory, scenario, connection_id)


async def test_a_delegation_to_an_archived_agent_is_refused(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """Archiving an agent is a decision its delegations should notice.

    The same rule `facts_and_effects` applies: an archived agent is not among
    the live ones, so a step naming it would be a step nothing can run.
    """
    writer_id = _writer(scenario)
    connection_id = _delegate(scenario, str(scenario.agent["id"]), writer_id)
    archived = scenario.client.delete(
        f"/v1/projects/{scenario.project['id']}/agents/{writer_id}",
        headers=scenario.actor.headers,
    )
    assert archived.status_code == 204, archived.text

    with pytest.raises(DelegationNotCompilableError) as raised:
        await _compile(factory, scenario, connection_id)
    assert {finding["code"] for finding in raised.value.findings} == {
        DelegationCode.UNKNOWN_PARTICIPANT.value
    }


async def test_two_agents_in_a_project_cannot_share_a_key(scenario: Scenario) -> None:
    """Uniqueness, which is the other half of what makes the key a step key.

    Two steps with the same key is a `DUPLICATE_STEP_KEY` error from
    `validate`; two agents with the same key would produce one every time
    either was compiled. The conflict is refused at the source instead, by
    `agents_project_key_key` and the check in front of it - and because the
    column is `citext`, `Writer` and `writer` are the same key.
    """
    _writer(scenario)
    duplicate = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/agents",
        json={
            "display_name": "Another writer",
            "agent_key": "Writer",
            "interaction_mode": "managed",
        },
        headers=scenario.actor.headers,
    )
    assert duplicate.status_code == 409, duplicate.text


async def test_a_tool_grant_is_not_compilable(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """A connection of the wrong kind, refused rather than misread.

    Worth asserting through the service: the kind check is in the pure
    function, so this proves the service does not pre-filter the selected
    connection into something the compiler never sees.
    """
    created = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/connections",
        json={
            "kind": "grant_tool",
            "source_kind": "agent",
            "source_id": str(scenario.agent["id"]),
            "target_kind": "tool",
            "target_id": str(uuid.uuid4()),
            "config": {"tool_name": "search", "side_effect": "readonly"},
        },
        headers=scenario.actor.headers,
    )
    assert created.status_code == 201, created.text

    with pytest.raises(DelegationNotCompilableError) as raised:
        await _compile(factory, scenario, uuid.UUID(str(created.json()["id"])))
    assert {finding["code"] for finding in raised.value.findings} == {
        DelegationCode.NOT_A_DELEGATION.value
    }

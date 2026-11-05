"""Two agent steps, one edge, through the real worker and the real outbox.

`advance()` has always been able to sequence steps: `test_advance.py` covers
edge readiness, joins, conditions and `$.steps.<key>.output.<field>`
resolution. What nothing covered is the same thing through the **production
path** - settle, fold, advance, emit, claim, perform - because every worker
test until now ran a single `research` step with `edges: []`.

That gap matters because the interesting part is not in `advance()`. It is in
`DispatchWorker.report`, which commits the settle and the step event in one
transaction and then calls `advance_once` in a *second* one. The second
dispatch is created by that second transaction. So "step output becomes the
next step's input" is a claim about two transactions and an outbox row, and a
pure-function test cannot make it.

M1 asks for exactly this: "the two-step execution completes, the second
agent's input is the first's output". These tests are that sentence, asserted
on rows.

**Nothing here simulates the worker.** `advance()` is never called directly
and no dispatch is ever inserted by hand; the only way the second dispatch
can exist is if the first step's completion really produced it. The two fake
provider responses are deliberately different, so "the writer received the
researcher's output" cannot be satisfied by both steps being sent the same
canned string.

The second test covers the recovery boundary. The worker dies in the hole
between those two transactions - the T6c failure window - and the question is
whether the second dispatch still eventually exists, exactly once.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_dispatch_worker import FakeProvider, Scenario, _worker

from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.executions import ExecutionService
from agentworld.services.reconcile import AdvanceReconciler
from helpers import Actor

pytestmark = pytest.mark.integration

#: Distinct on purpose. The researcher's answer has to be recognisable in the
#: writer's request, or the mapping assertion proves nothing.
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


# --------------------------------------------------------------- the fixture ---


def _two_step(scenario: Scenario) -> uuid.UUID:
    """A published `researcher -> writer` workflow in the scenario's project.

    Built through the same API the single-step scenario uses, reusing its
    workspace, credential and model profile, so the only new things are a
    second agent and an edge.
    """
    detail = scenario.client.get(
        f"/v1/projects/{scenario.project['id']}/agents/{scenario.agent['id']}",
        headers=scenario.actor.headers,
    )
    assert detail.status_code == 200, detail.text
    profile_id = detail.json()["model_profile_id"]
    assert profile_id, "the scenario's researcher has no model profile to share"

    writer = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/agents",
        json={
            "display_name": "Writer",
            "agent_key": "writer",
            "interaction_mode": "managed",
            "instructions": "Write one sentence.",
        },
        headers=scenario.actor.headers,
    )
    assert writer.status_code == 201, writer.text
    configured = scenario.client.patch(
        f"/v1/projects/{scenario.project['id']}/agents/{writer.json()['id']}",
        json={"model_profile_id": profile_id},
        headers=scenario.actor.headers,
    )
    assert configured.status_code == 200, configured.text

    created = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/workflows",
        json={
            "name": "Research then write",
            "graph": {
                "steps": [
                    {
                        "step_key": "research",
                        "kind": "agent_task",
                        "agent_key": "researcher",
                        "input_mapping": {"sector": "$.input.sector"},
                    },
                    {
                        "step_key": "write",
                        "kind": "agent_task",
                        "agent_key": "writer",
                        # The whole point: the writer's input is the
                        # researcher's persisted output, not the execution
                        # input.
                        "input_mapping": {"findings": "$.steps.research.output.text"},
                    },
                ],
                "edges": [{"from": "research", "to": "write"}],
            },
        },
        headers=scenario.actor.headers,
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["id"]

    report = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/workflows/{workflow_id}/validate",
        headers=scenario.actor.headers,
    )
    assert report.status_code == 200, report.text
    assert report.json()["publishable"] is True, (
        f"the two-step graph does not validate: {report.json()['findings']}"
    )

    published = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/workflows/{workflow_id}/publish",
        headers=scenario.actor.headers,
    )
    assert published.status_code == 201, published.text
    return uuid.UUID(str(workflow_id))


def _start(scenario: Scenario, workflow_id: uuid.UUID) -> uuid.UUID:
    started = scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/workflows/{workflow_id}/run",
        json={"input": {"sector": "fintech"}},
        headers=scenario.actor.headers,
    )
    assert started.status_code == 202, started.text
    return uuid.UUID(started.json()["id"])


# --------------------------------------------------------------- reading rows ---
# `_states` and `_step_output` in test_dispatch_worker use `scalar_one()`, so
# they raise on an execution with two steps. These read every row instead.


async def _steps(owner_database: Database, execution_id: uuid.UUID) -> dict[str, Any]:
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT step_key, status, input, output FROM execution_steps "
                    "WHERE execution_id = :id ORDER BY step_key"
                ),
                {"id": str(execution_id)},
            )
        ).all()
    return {str(row.step_key): row for row in rows}


async def _dispatches(owner_database: Database, execution_id: uuid.UUID) -> list[Any]:
    """Every dispatch for this execution, oldest first, with its step key."""
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT d.kind, d.status, d.instruction, d.idempotency_token, "
                    "s.step_key "
                    "FROM dispatches d "
                    "JOIN execution_steps s ON s.id = d.execution_step_id "
                    "WHERE d.execution_id = :id ORDER BY d.created_at, s.step_key"
                ),
                {"id": str(execution_id)},
            )
        ).all()


async def _execution_status(owner_database: Database, execution_id: uuid.UUID) -> str:
    async with owner_database.session() as session:
        return str(
            (
                await session.execute(
                    text("SELECT status FROM workflow_executions WHERE id = :id"),
                    {"id": str(execution_id)},
                )
            ).scalar_one()
        )


def _sent_text(request: dict[str, Any]) -> str:
    """The user message of a captured Messages request."""
    messages = request.get("messages") or []
    assert messages, f"no messages in the captured request: {request}"
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    return " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))


# ------------------------------------------------------------- the happy path ---


async def test_a_two_step_workflow_executes_end_to_end_through_the_worker(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    provider: FakeProvider,
) -> None:
    """The nine things M1 asks for, in the order the production path does them."""
    provider.responses = [_answer(FINDINGS), _answer(BRIEF)]
    workflow_id = _two_step(scenario)
    execution_id = _start(scenario, workflow_id)
    worker = _worker(factory, provider)

    # 1. The execution starts with the researcher dispatch, and only it. The
    #    writer has an incoming edge, so it is not ready.
    queued = await _dispatches(owner_database, execution_id)
    assert [row.step_key for row in queued] == ["research"], (
        f"expected one dispatch for 'research', found {[r.step_key for r in queued]}"
    )
    assert queued[0].status == "pending"

    # 2. The worker claims and performs it.
    first = await worker.run_once()
    assert first is not None, "the researcher dispatch was never claimed"
    assert first.step_key == "research"
    assert len(provider.captured) == 1, "the researcher's turn did not reach the provider"

    # 3. Its output is persisted, as the real model answer.
    steps = await _steps(owner_database, execution_id)
    assert steps["research"].status == "succeeded"
    assert steps["research"].output is not None
    assert steps["research"].output["text"] == FINDINGS

    # 4 and 5. The execution advanced, and the second dispatch exists -
    #    created by `advance_once` through the outbox, because nothing here
    #    created it.
    after_first = await _dispatches(owner_database, execution_id)
    assert [row.step_key for row in after_first] == ["research", "write"], (
        "no dispatch was emitted for 'write' after the researcher finished, so the "
        f"execution did not advance: {[r.step_key for r in after_first]}"
    )
    writer_dispatch = after_first[1]
    assert writer_dispatch.status == "pending"
    assert writer_dispatch.kind == "managed_agent_turn"

    # 7a. The strongest persisted evidence of the mapping: the instruction
    #     the orchestrator wrote carries the researcher's actual words, not
    #     the expression that produced them.
    assert writer_dispatch.instruction["input"] == {"findings": FINDINGS}, (
        "the writer's dispatch instruction does not carry the researcher's output: "
        f"{writer_dispatch.instruction.get('input')!r}"
    )

    # 6. The worker claims and performs the writer's turn.
    second = await worker.run_once()
    assert second is not None, "the writer dispatch was never claimed"
    assert second.step_key == "write"
    assert len(provider.captured) == 2, "the writer's turn did not reach the provider"

    # 7b. And the provider really received it. This is the assertion that
    #     distinguishes a correct mapping from a correct-looking definition.
    sent = _sent_text(provider.captured[1])
    assert FINDINGS in sent, (
        f"the writer's prompt does not contain the researcher's output. Sent: {sent!r}"
    )
    assert steps["research"].output["text"] in sent, "the mapped value is not the first output"

    # 8. Both transitions are persisted.
    final_steps = await _steps(owner_database, execution_id)
    assert {key: row.status for key, row in final_steps.items()} == {
        "research": "succeeded",
        "write": "succeeded",
    }
    assert final_steps["write"].output is not None
    assert final_steps["write"].output["text"] == BRIEF
    # `execution_steps.input` is deliberately NOT the evidence here. Nothing
    # writes that column yet: `seed_steps` creates every step row `pending`
    # with no input, and `ExecutionView` says step detail is T12's. So the
    # strongest persisted proof of the mapping is the dispatch instruction
    # asserted above, and the strongest proof it was *used* is the provider
    # request body. Asserting the column would have been asserting an
    # unimplemented feature.
    assert final_steps["write"].input is None, (
        "execution_steps.input is now populated. That is a change worth having - but "
        "this test's mapping evidence should move to it, and T12 should own the column"
    )

    # 9. The execution is over, successfully.
    assert await _execution_status(owner_database, execution_id) == "completed"

    # Exactly two of each, so nothing ran twice and nothing extra was emitted.
    assert len(final_steps) == 2, f"expected two step rows, found {len(final_steps)}"
    done = await _dispatches(owner_database, execution_id)
    assert len(done) == 2, f"expected two dispatches, found {len(done)}"
    assert [row.status for row in done] == ["succeeded", "succeeded"]
    assert {row.kind for row in done} == {"managed_agent_turn"}
    assert len({row.idempotency_token for row in done}) == 2, (
        "the two dispatches share an idempotency token"
    )
    assert len(provider.captured) == 2, "the provider was called more than twice"


# ------------------------------------------------- the T6c recovery boundary ---


async def test_a_stranded_advance_is_recovered_and_the_writer_runs_exactly_once(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The researcher finishes; the process dies before the writer is queued.

    Produced the way `test_reconcile.py` produces it - by making
    `advance_once` raise for one call, which is what a `SIGKILL` between the
    settle and the advance looks like from the database. The settle and the
    step event are committed; the decision never happened.

    Before T6c this execution stayed `running` with nothing in flight for
    ever. The question here is narrower than the general recovery test: does
    the *second step of a real sequence* get queued and run, exactly once.
    """
    provider.responses = [_answer(FINDINGS), _answer(BRIEF)]
    workflow_id = _two_step(scenario)
    execution_id = _start(scenario, workflow_id)
    worker = _worker(factory, provider)

    async def never_advances(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the process died between the two transactions")

    with monkeypatch.context() as patched:
        patched.setattr(ExecutionService, "advance_once", never_advances)
        with pytest.raises(RuntimeError, match="died between"):
            await worker.run_once()

    # The hole: the step is finished, and nothing is queued to continue it.
    stranded_steps = await _steps(owner_database, execution_id)
    assert stranded_steps["research"].status == "succeeded"
    assert stranded_steps["research"].output["text"] == FINDINGS
    stranded = await _dispatches(owner_database, execution_id)
    assert [row.step_key for row in stranded] == ["research"], (
        "the writer was queued despite the advance failing, so this test is not "
        f"exercising the recovery window at all: {[r.step_key for r in stranded]}"
    )
    assert await _execution_status(owner_database, execution_id) == "running"

    # The reconciler finds it from derived state. `grace_seconds=0` because
    # the staleness cut-off exists to avoid racing a live worker, and there
    # is no live worker here.
    reconciler = AdvanceReconciler(factory, executions=ExecutionService(factory), grace_seconds=0)
    result = await reconciler.sweep(limit=10)
    assert result.candidates >= 1, "the stranded execution was not offered as a candidate"
    assert result.advanced >= 1, f"the sweep advanced nothing: {result}"
    assert result.failures == 0, f"the sweep had failures: {result}"

    # Recovery went through the ordinary durable path, so the writer is now
    # an ordinary pending dispatch carrying the mapped input.
    recovered = await _dispatches(owner_database, execution_id)
    assert [row.step_key for row in recovered] == ["research", "write"], (
        f"the reconciler did not emit the writer's dispatch: {[r.step_key for r in recovered]}"
    )
    assert recovered[1].instruction["input"] == {"findings": FINDINGS}

    # And it runs, once.
    second = await worker.run_once()
    assert second is not None and second.step_key == "write"
    assert FINDINGS in _sent_text(provider.captured[-1])

    final_steps = await _steps(owner_database, execution_id)
    assert final_steps["write"].status == "succeeded"
    assert await _execution_status(owner_database, execution_id) == "completed"

    # No duplicates: one dispatch per step, one provider call per step.
    done = await _dispatches(owner_database, execution_id)
    assert len(done) == 2, f"recovery duplicated a dispatch: {[r.step_key for r in done]}"
    assert len(provider.captured) == 2, (
        f"the writer ran more than once: {len(provider.captured)} provider calls"
    )

    # A second sweep must find nothing left to do, or recovery is not
    # idempotent and a finished execution would be advanced for ever.
    again = await reconciler.sweep(limit=10)
    assert again.advanced == 0, f"a completed execution was advanced again: {again}"
    assert len(await _dispatches(owner_database, execution_id)) == 2

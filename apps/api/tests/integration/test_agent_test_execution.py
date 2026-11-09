"""A test run end to end: the real worker, a real socket, a real answer.

S6. Everything before this proved the pieces - the graph, the row, the
lifecycle, the route. This file proves the promise:

    POST /…/test -> harness published -> DispatchWorker -> provider
                 -> GET /executions/{id} carries what the agent said

The provider is the loopback server `test_dispatch_worker.py` uses, so the
guarded client, the credential decryption and the provider adapter are all in
the path rather than stubbed out of it. Nothing here inserts a dispatch or
calls `advance()`: the only way the provider can be reached is if the
orchestrator planned the work from a published version and the worker claimed
it.

The last four tests are the ones that matter most, and none of them is about
testing: a missing credential, a cancellation, a timeout, and whether the
request a test makes is the same request an ordinary run makes. "Test" is not
a mode here - it is a workflow nobody had to draw - and these are what hold
that to it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_dispatch_worker import API_KEY, FakeProvider, _worker

from agentworld.infra.db import Database
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.dispatch import LeaseReaper
from helpers import Actor

pytestmark = pytest.mark.integration

SAMPLE = "Two rivals moved this week. What changed?"
ANSWER = "Acme raised a round and Globex cut prices."
INSTRUCTIONS = "Be brief."


@pytest.fixture
async def provider() -> Any:
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


def _answer(text_: str, *, model: str = "test-model") -> dict[str, Any]:
    return {
        "id": "msg_01",
        "model": model,
        "content": [{"type": "text", "text": text_}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


class Island:
    """A project with one managed agent that can really call a provider.

    Built through the API, so the credential is really encrypted and really
    has to be decrypted before the call. Deliberately **no workflow**: the
    only workflow in this project is the one `agent.test` compiles, which is
    what makes "without a workflow" literal here.
    """

    def __init__(
        self,
        client: TestClient,
        actor: Actor,
        provider: FakeProvider,
        *,
        with_credential: bool = True,
    ) -> None:
        self.client = client
        self.actor = actor
        self.provider = provider
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Testing"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()

        credential_id: str | None = None
        if with_credential:
            credential = client.post(
                f"/v1/workspaces/{self.workspace['id']}/credentials",
                json={
                    "name": "Provider key",
                    "kind": "api_key",
                    "provider_slug": "anthropic",
                    "secret": API_KEY,
                },
                headers=actor.headers,
            )
            assert credential.status_code == 201, credential.text
            credential_id = credential.json()["id"]

        self.profile = self.add_profile("Test model", "test-model", credential_id)
        agent = client.post(
            f"/v1/projects/{self.project['id']}/agents",
            json={
                "display_name": "Brief Writer",
                "agent_key": "writer",
                "interaction_mode": "managed",
                "instructions": INSTRUCTIONS,
            },
            headers=actor.headers,
        )
        assert agent.status_code == 201, agent.text
        self.agent = agent.json()
        self.configure(self.profile["id"])

    def add_profile(self, name: str, model_id: str, credential_id: str | None) -> Any:
        created = self.client.post(
            f"/v1/workspaces/{self.workspace['id']}/model-profiles",
            json={
                "name": name,
                "provider_slug": "anthropic",
                "model_id": model_id,
                "credential_id": credential_id,
                "parameters": {
                    "base_url": self.provider.base_url,
                    "max_output_tokens": 64,
                },
            },
            headers=self.actor.headers,
        )
        assert created.status_code == 201, created.text
        return created.json()

    def configure(self, profile_id: str) -> None:
        patched = self.client.patch(
            f"/v1/projects/{self.project['id']}/agents/{self.agent['id']}",
            json={"model_profile_id": profile_id},
            headers=self.actor.headers,
        )
        assert patched.status_code == 200, patched.text

    def instruct(self, instructions: str) -> None:
        patched = self.client.patch(
            f"/v1/projects/{self.project['id']}/agents/{self.agent['id']}",
            json={"instructions": instructions},
            headers=self.actor.headers,
        )
        assert patched.status_code == 200, patched.text

    @property
    def project_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.project["id"]))

    @property
    def workspace_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.workspace["id"]))

    def test_it(self, sample: str = SAMPLE) -> Any:
        """`POST /…/test`, asserting only that it was accepted."""
        response = self.client.post(
            f"/v1/projects/{self.project_id}/agents/{self.agent['id']}/test",
            json={"input": sample},
            headers=self.actor.headers,
        )
        assert response.status_code == 202, response.text
        return response.json()

    def execution(self, execution_id: str) -> Any:
        read = self.client.get(
            f"/v1/projects/{self.project_id}/executions/{execution_id}",
            headers=self.actor.headers,
        )
        assert read.status_code == 200, read.text
        return read.json()


@pytest.fixture
def island(client: TestClient, actor: Actor, provider: FakeProvider) -> Island:
    return Island(client, actor, provider)


# -------------------------------------------------------------- reading rows ---


async def _rows(owner_database: Database, sql: str, **binds: Any) -> list[Any]:
    async with owner_database.session() as session:
        return (await session.execute(text(sql), binds)).all()


async def _dispatches(owner_database: Database, project_id: uuid.UUID) -> list[Any]:
    return await _rows(
        owner_database,
        "SELECT status, last_error_code, last_error, attempt, claim_count, side_effect_class, kind "
        "FROM dispatches WHERE project_id = :p ORDER BY created_at",
        p=project_id,
    )


async def _steps(owner_database: Database, execution_id: str) -> list[Any]:
    return await _rows(
        owner_database,
        "SELECT step_key, status, output, error FROM execution_steps "
        "WHERE execution_id = :e ORDER BY step_key, attempt",
        e=uuid.UUID(execution_id),
    )


def _sent_text(request: dict[str, Any]) -> str:
    messages = request.get("messages") or []
    assert messages, f"no messages in the captured request: {request}"
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    return " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))


# ------------------------------------------------------------- the whole path ---


async def test_a_test_run_reaches_the_provider_with_the_sample_input(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider
) -> None:
    """The sample the person typed is what the model is asked.

    Read out of the request the loopback server received, not out of the
    instruction column: `_prompt` renders the step's resolved input, and an
    input mapping that resolved to nothing would send "Begin the task you
    have been assigned." instead - which is what makes this assertion worth
    making over the wire.
    """
    provider.responses.append(_answer(ANSWER))
    island.test_it()

    assert await _worker(factory, provider).run_once() is not None

    assert len(provider.captured) == 1
    assert _sent_text(provider.captured[0]) == f"input: {SAMPLE}"
    # The agent's own instructions, as its system prompt. A test that did
    # not carry them would be testing a different agent.
    assert provider.captured[0]["system"] == INSTRUCTIONS
    assert provider.captured[0]["model"] == "test-model"
    # The credential really was decrypted and sent, and it is not in any
    # assertion here beyond the fact that the server saw a bearer of some
    # kind - the test must not print it.
    assert provider.requests[0].get("x-api-key") or provider.requests[0].get("authorization")


async def test_the_execution_output_carries_what_the_agent_said(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider, owner_database: Database
) -> None:
    """Decision 4 and decision 6, end to end and over HTTP.

    The answer arrives on the execution's own `output`, keyed by the step
    key - which is the agent key - and the ordinary execution read is what
    returns it. There is no second result endpoint, and this is the test that
    says so rather than the docstring that claims it.
    """
    provider.responses.append(_answer(ANSWER))
    started = island.test_it()

    assert await _worker(factory, provider).run_once() is not None

    finished = island.execution(started["id"])
    assert finished["status"] == "completed"
    assert finished["output"] == {
        "writer": {
            "text": ANSWER,
            # `end_turn` on the wire; the adapter normalises it to the
            # domain's own vocabulary before it is recorded.
            "stop_reason": "completed",
            "model": "test-model",
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
    }
    assert finished["error"] is None
    assert finished["finished_at"] is not None

    # Decision 8: the provider's token counts are preserved where they
    # already live, on the step's output. No ledger, and none invented.
    steps = await _steps(owner_database, started["id"])
    assert [row.status for row in steps] == ["succeeded"]
    assert steps[0].output["usage"] == {"input_tokens": 11, "output_tokens": 7}


async def test_a_later_test_sends_the_edited_instructions_without_republishing(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider, owner_database: Database
) -> None:
    """Why one reusable harness is correct and not merely cheap.

    The published version names the agent; the runner reads the agent when
    it performs the dispatch. So the second test sends the new instructions
    while running the same `workflow_version_id` as the first - asserted on
    the wire, because an assertion on the row would only show that the edit
    was saved.
    """
    provider.responses.extend([_answer(ANSWER), _answer("One word.")])
    first = island.test_it()
    assert await _worker(factory, provider).run_once() is not None

    island.instruct("Answer in exactly one word.")
    second = island.test_it()
    assert await _worker(factory, provider).run_once() is not None

    assert [r["system"] for r in provider.captured] == [
        INSTRUCTIONS,
        "Answer in exactly one word.",
    ]
    assert first["workflow_id"] == second["workflow_id"]
    assert first["workflow_version_id"] == second["workflow_version_id"]

    versions = await _rows(
        owner_database,
        "SELECT count(*) AS n FROM workflow_versions WHERE workflow_id = :w",
        w=uuid.UUID(first["workflow_id"]),
    )
    assert versions[0].n == 2, "version 1 published, one fresh draft, and no republish"


async def test_swapping_the_model_profile_changes_the_next_test(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider
) -> None:
    """The other half of "picked up live": the model, not just the prompt."""
    provider.responses.extend([_answer(ANSWER), _answer("Different.", model="other-model")])
    island.test_it()
    assert await _worker(factory, provider).run_once() is not None

    credential_id = island.profile["credential_id"]
    island.configure(island.add_profile("Other model", "other-model", credential_id)["id"])
    island.test_it()
    assert await _worker(factory, provider).run_once() is not None

    assert [r["model"] for r in provider.captured] == ["test-model", "other-model"]


# ------------------------------------------------- the four that are not about tests ---


async def test_a_missing_credential_fails_at_runtime_not_at_compilation(
    client: TestClient,
    actor: Actor,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """C19, through `agent.test`.

    A managed agent whose model profile carries no `credential_id` must
    still compile, publish and dispatch, and fail only where a runtime would
    be selected. The roadmap asks for exactly this path to be asserted so it
    cannot rot before Phase 7 needs it - and `agent.test` is now the
    cheapest way for a person to walk into it, so it has to fail in the
    honest place.
    """
    island = Island(client, actor, provider, with_credential=False)
    assert island.profile["credential_id"] is None

    started = island.test_it()
    assert started["status"] in ("pending", "running"), "the test was accepted"

    assert await _worker(factory, provider).run_once() is not None

    dispatches = await _dispatches(owner_database, island.project_id)
    assert [(row.status, row.last_error_code) for row in dispatches] == [("dead", "misconfigured")]
    assert provider.captured == [], "nothing was sent anywhere"

    failed = island.execution(started["id"])
    assert failed["status"] == "failed"
    # The reason reaches the person who pressed Test, and it names the
    # profile rather than the credential.
    steps = await _steps(owner_database, started["id"])
    assert steps[0].status == "failed"
    assert "credential" in steps[0].error["message"]


async def test_cancelling_a_test_execution_stops_it_like_any_other(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider, owner_database: Database
) -> None:
    """The existing cancel route, on a test run, with nothing added for it.

    This is the queued case: the dispatch had not been claimed, so
    cancelling drops it and there is nothing left for a worker to find. The
    liveness guard is not reached here - the test below is the one that
    reaches it.
    """
    provider.responses.append(_answer(ANSWER))
    started = island.test_it()

    cancelled = island.client.post(
        f"/v1/projects/{island.project_id}/executions/{started['id']}/cancel",
        headers=island.actor.headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["previous_status"] in ("pending", "running")
    assert cancelled.json()["cancelled_dispatches"] == 1
    assert cancelled.json()["cancelling"] is False

    # Nothing left to claim, and nothing sent.
    assert await _worker(factory, provider).run_once() is None
    assert provider.captured == []
    assert island.execution(started["id"])["status"] == "cancelled"


async def test_a_cancelled_test_is_not_resumed_when_its_lease_expires(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider, owner_database: Database
) -> None:
    """The liveness guard, on a test run, in the window it exists for.

    A dispatch leased when the user cancelled is left alone on purpose,
    because its call may be on the wire. If that worker then dies, the reaper
    returns the row to `pending` - correctly, as far as it can see, since no
    side effect had started. Performing it now would be a real model call,
    and real money, for a test somebody stopped.

    The same shape as `test_execution_cancel.py`'s version of this, aimed at
    a test run: the dispatch is identical, so what protects one protects the
    other, and this is the assertion that says so rather than assuming it.
    """
    provider.responses.append(_answer(ANSWER))
    started = island.test_it()

    async with factory.begin(visible_workspaces={island.workspace_id}) as work:
        claimed = await work.dispatch_claims(TenantScope(island.workspace_id)).claim(
            worker="doomed", lease_seconds=0
        )
    assert claimed is not None, "there was no dispatch to lease"

    cancelled = island.client.post(
        f"/v1/projects/{island.project_id}/executions/{started['id']}/cancel",
        headers=island.actor.headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["in_flight_dispatches"] == 1
    assert cancelled.json()["cancelling"] is True

    returned, escalated = await LeaseReaper(factory).sweep()
    assert (returned, escalated) == (1, 0), (
        "the reaper did not return the abandoned lease, so this test is not "
        "exercising the window it exists for"
    )

    performed = await _worker(factory, provider).run_once()
    assert provider.captured == [], (
        "a provider call was made for a cancelled test run after its lease expired. "
        f"The worker claimed {performed.id if performed else None}"
    )
    assert island.execution(started["id"])["status"] == "cancelled"


async def test_a_provider_timeout_goes_to_review_rather_than_to_failure(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider, owner_database: Database
) -> None:
    """The uncertain outcome, unchanged for a test run.

    A timeout means the request was sent and nothing came back, which says
    nothing about whether it was served. A test spends money, so "probably
    nothing happened" is not a conclusion this system is allowed to reach on
    its own - the dispatch goes to a person and the execution stays live.
    """
    provider.delay = 2.0
    island.test_it()

    assert await _worker(factory, provider, total_timeout=0.25).run_once() is not None

    dispatches = await _dispatches(owner_database, island.project_id)
    assert [(row.status, row.last_error_code) for row in dispatches] == [
        ("needs_review", "timeout")
    ]
    assert len(provider.captured) == 1, "the request really was sent"


async def test_the_request_a_test_sends_is_the_request_an_ordinary_run_sends(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider, actor: Actor
) -> None:
    """Body for body, with nothing about a test in it.

    A hand-authored single-step workflow over the same agent, published and
    run through the ordinary routes, and the two provider requests compared.
    If a later change gave test runs their own prompt shape, their own system
    prompt or their own parameters, this is what would fail - and it compares
    what left the process, which no amount of equal database rows would.
    """
    provider.responses.extend([_answer(ANSWER), _answer(ANSWER)])
    island.test_it()
    assert await _worker(factory, provider).run_once() is not None

    authored = island.client.post(
        f"/v1/projects/{island.project_id}/workflows",
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
    published = island.client.post(
        f"/v1/projects/{island.project_id}/workflows/{authored['id']}/publish",
        headers=actor.headers,
    )
    assert published.status_code == 201, published.text
    ran = island.client.post(
        f"/v1/projects/{island.project_id}/workflows/{authored['id']}/run",
        json={"input": {"input": SAMPLE}},
        headers=actor.headers,
    )
    assert ran.status_code == 202, ran.text
    assert await _worker(factory, provider).run_once() is not None

    assert len(provider.captured) == 2
    assert provider.captured[0] == provider.captured[1]


async def test_two_tests_at_once_run_one_harness_and_two_executions(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider, owner_database: Database
) -> None:
    """The concurrency S3 proved on rows, seen from the outside.

    Two HTTP requests, so the first-use path really is reached twice. They
    cannot overlap inside `TestClient`, which is why S3 holds the two-
    transaction case; what this adds is that the published harness is shared
    and both executions run against the same version.
    """
    provider.responses.extend([_answer(ANSWER), _answer(ANSWER)])
    first, second = island.test_it(), island.test_it()

    worker = _worker(factory, provider)
    assert await worker.run_once() is not None
    assert await worker.run_once() is not None

    assert first["workflow_id"] == second["workflow_id"]
    assert first["workflow_version_id"] == second["workflow_version_id"]
    harnesses = await _rows(
        owner_database,
        "SELECT count(*) AS n FROM workflows WHERE project_id = :p "
        "AND test_for_agent_id IS NOT NULL",
        p=island.project_id,
    )
    assert harnesses[0].n == 1
    assert island.execution(first["id"])["output"]["writer"]["text"] == ANSWER
    assert island.execution(second["id"])["output"]["writer"]["text"] == ANSWER


async def test_nothing_in_the_dispatch_marks_it_as_a_test(
    factory: UnitOfWorkFactory, island: Island, provider: FakeProvider, owner_database: Database
) -> None:
    """The negative claim, stated once: there is no test mode.

    If some later change wanted to treat test work differently - a longer
    lease, a cheaper model, a skipped guard - it would need a way to
    recognise it here, and there is none. The only thing that knows is
    `workflows.test_for_agent_id`, two joins away from anything the worker
    reads.
    """
    provider.responses.append(_answer(ANSWER))
    island.test_it()
    assert await _worker(factory, provider).run_once() is not None

    columns = await _rows(
        owner_database,
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t",
        t="dispatches",
    )
    names = {row.column_name for row in columns}
    assert not {name for name in names if "test" in name}, names

    rows = await _rows(
        owner_database,
        "SELECT instruction FROM dispatches WHERE project_id = :p",
        p=island.project_id,
    )
    assert set(rows[0].instruction) == {"agent_key", "input"}

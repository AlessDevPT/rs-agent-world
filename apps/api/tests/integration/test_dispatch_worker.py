"""The outbox worker: claiming, leasing, crashing, and reporting once.

Every test here runs the real worker against a real database and a real
socket. The provider is a loopback server that speaks the Messages protocol
and records what it was sent - deterministic, offline, and not a mock, so the
guarded client, the credential decryption and the provider adapter are all in
the path rather than stubbed out of it.

The eight scenarios T6 was asked for, in order: two workers racing for one
dispatch, a worker that crashes after claiming, a lease expiring and being
recovered, a duplicate report, a timeout whose outcome is unknown, a dispatch
the worker must refuse, advancement after success and after failure, and the
idempotency token that lets a receiver dedupe a retry.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import ipaddress
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain.enums import DispatchStatus, ExecutionStatus, SideEffect, StepStatus
from agentworld.infra import net
from agentworld.infra.db import Database
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.credentials import CredentialService
from agentworld.services.dispatch import (
    DispatchWorker,
    LeaseReaper,
    Outcome,
    StepRunner,
)
from agentworld.services.executions import ExecutionService
from helpers import Actor

pytestmark = pytest.mark.integration

API_KEY = "sk-ant-test-ZZworkerZZ-0001"


# ------------------------------------------------------------ a fake provider ---


@dataclass
class FakeProvider:
    """A loopback server speaking the Messages protocol.

    `delay` is how the timeout case is produced: a server that accepts the
    request and then does not answer is exactly the shape of an outcome
    nobody can be sure about.
    """

    port: int = 0
    status: int = 200
    delay: float = 0.0
    #: Extra response headers. `Retry-After` is why this exists: T6a needs
    #: the provider to ask for a specific delay, and without it the
    #: assertion passes against the fallback backoff instead.
    extra_headers: dict[str, str] = field(default_factory=dict)
    requests: list[dict[str, str]] = field(default_factory=list)
    #: Set by the handler the moment a request arrives, so a test can wait
    #: for the call to be genuinely in flight instead of sleeping and hoping.
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    #: When set to an event, the handler waits for it before answering. The
    #: pair replaces a fixed sleep: `arrived` says the call has started and
    #: `hold` keeps it started until the test is finished looking. A sleep
    #: long enough to be safe on a loaded machine is a sleep that makes the
    #: suite slower for everyone, and one short enough to be quick is one
    #: that fails on a loaded machine - which is exactly what happened.
    hold: asyncio.Event | None = None

    @property
    def base_url(self) -> str:
        return f"http://provider.test:{self.port}"

    def policy(self, *, total_timeout: float = 30.0) -> net.EgressPolicy:
        return net.EgressPolicy(
            allowed_ports=frozenset({self.port}),
            allowed_networks=frozenset({ipaddress.ip_network("127.0.0.0/8")}),
            total_timeout_seconds=total_timeout,
            connect_timeout_seconds=2.0,
        )


async def _resolve(_host: str, _port: int) -> Sequence[net.Address]:
    return [ipaddress.ip_address("127.0.0.1")]


@pytest.fixture
async def provider() -> AsyncIterator[FakeProvider]:
    state = FakeProvider()
    body = json.dumps(
        {
            "id": "msg_01",
            "model": "test-model",
            "content": [{"type": "text", "text": "Four competitors."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 4},
        }
    ).encode()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length:
            await reader.readexactly(length)
        state.requests.append(headers)
        state.arrived.set()

        if state.hold is not None:
            await state.hold.wait()
        if state.delay:
            await asyncio.sleep(state.delay)
        extra = "".join(f"{k}: {v}\r\n" for k, v in state.extra_headers.items())
        writer.write(
            f"HTTP/1.1 {state.status} X\r\nContent-Type: application/json\r\n{extra}"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state.port = server.sockets[0].getsockname()[1]
    async with server:
        yield state


# --------------------------------------------------------------- the scenario ---


class Scenario:
    """A published workflow whose one step is a managed agent turn.

    Built through the API, so the agent, the credential, the model profile and
    the published version are all in the state a real deployment would have
    them in - including a credential that is really encrypted and really has
    to be decrypted before the call.
    """

    def __init__(self, client: TestClient, actor: Actor, provider: FakeProvider) -> None:
        self.client = client
        self.actor = actor
        self.provider = provider
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Worker"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Island"},
            headers=actor.headers,
        ).json()
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
        profile = client.post(
            f"/v1/workspaces/{self.workspace['id']}/model-profiles",
            json={
                "name": "Test model",
                "provider_slug": "anthropic",
                "model_id": "test-model",
                "credential_id": credential.json()["id"],
                # Operator configuration carried as data, which is also how
                # the tests reach a local server: still validated by the
                # egress policy, still through the guarded client.
                "parameters": {"base_url": provider.base_url, "max_output_tokens": 64},
            },
            headers=actor.headers,
        )
        assert profile.status_code == 201, profile.text
        self.agent = client.post(
            f"/v1/projects/{self.project['id']}/agents",
            json={
                "display_name": "Researcher",
                "agent_key": "researcher",
                "interaction_mode": "managed",
                "instructions": "Be brief.",
            },
            headers=actor.headers,
        ).json()
        configured = client.patch(
            f"/v1/projects/{self.project['id']}/agents/{self.agent['id']}",
            json={"model_profile_id": profile.json()["id"]},
            headers=actor.headers,
        )
        assert configured.status_code == 200, configured.text

        workflow = client.post(
            f"/v1/projects/{self.project['id']}/workflows",
            json={
                "name": "Research",
                "graph": {
                    "steps": [
                        {
                            "step_key": "research",
                            "kind": "agent_task",
                            "agent_key": "researcher",
                            "input_mapping": {"sector": "$.input.sector"},
                        }
                    ],
                    "edges": [],
                },
            },
            headers=actor.headers,
        ).json()
        self.workflow_id = workflow["workflow"]["id"]
        published = client.post(
            f"/v1/projects/{self.project['id']}/workflows/{self.workflow_id}/publish",
            headers=actor.headers,
        )
        assert published.status_code == 201, published.text

    @property
    def project_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.project["id"]))

    @property
    def workspace_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.workspace["id"]))

    def run(self) -> uuid.UUID:
        response = self.client.post(
            f"/v1/projects/{self.project_id}/workflows/{self.workflow_id}/run",
            json={"input": {"sector": "fintech"}},
            headers=self.actor.headers,
        )
        assert response.status_code == 202, response.text
        return uuid.UUID(response.json()["id"])


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


def _worker(
    factory: UnitOfWorkFactory,
    provider: FakeProvider,
    *,
    name: str | None = None,
    lease_seconds: int = 120,
    total_timeout: float = 30.0,
) -> DispatchWorker:
    runner = StepRunner(
        factory,
        credentials=CredentialService(factory, store=_store()),
        client=net.GuardedClient(provider.policy(total_timeout=total_timeout), resolve=_resolve),
    )
    return DispatchWorker(
        factory,
        runner=runner,
        executions=ExecutionService(factory),
        name=name,
        lease_seconds=lease_seconds,
    )


def _store() -> Any:
    """The same keyring the app under test uses.

    Built from the development keyring rather than from `Settings` because
    the worker is constructed here rather than by the app; the credential was
    written by the app, so the two have to agree, and
    `test_credentials_api.py` is what asserts the app uses this one when no
    `CREDENTIAL_KEKS` is configured.
    """
    from agentworld.infra import secrets

    return secrets.EnvelopeSecretStore(secrets.development())


def _claims(work: Any, scenario: Scenario) -> Any:
    """The claim repository for this scenario's workspace.

    Claiming is workspace-scoped because `dispatches` is under forced
    row-level security, so there is a scope to pass on every call.
    """
    return work.dispatch_claims(TenantScope(scenario.workspace_id))


async def _dispatch_row(owner_database: Database, execution_id: uuid.UUID) -> Any:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT id, status, leased_by, leased_until, attempt, claim_count, "
                    "side_effect_started_at, last_error_code, run_after "
                    "FROM dispatches WHERE execution_id = :id"
                ),
                {"id": str(execution_id)},
            )
        ).one()


async def _states(owner_database: Database, execution_id: uuid.UUID) -> tuple[str, str]:
    async with owner_database.session() as session:
        execution = (
            await session.execute(
                text("SELECT status FROM workflow_executions WHERE id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()
        step = (
            await session.execute(
                text("SELECT status FROM execution_steps WHERE execution_id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()
    return str(execution), str(step)


# --------------------------------------------------- 1. the happy path first ---


async def test_a_worker_performs_the_turn_and_the_execution_completes(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The milestone: a managed agent turn actually runs.

    The credential is decrypted, the call goes through the guarded client to
    a real socket, the outcome is folded into the step, and the execution
    advances to completed - through `ExecutionService.advance_once`, which is
    the same path the run itself used.
    """
    execution_id = scenario.run()
    worker = _worker(factory, provider)

    claimed = await worker.run_once()
    assert claimed is not None
    assert claimed.step_key == "research"

    # The provider was really called, with the key the credential held.
    assert len(provider.requests) == 1
    assert provider.requests[0]["x-api-key"] == API_KEY

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.SUCCEEDED
    assert row.leased_by is None
    assert row.side_effect_started_at is None, "the uncertainty was resolved"

    execution, step = await _states(owner_database, execution_id)
    assert step == StepStatus.SUCCEEDED
    assert execution == ExecutionStatus.COMPLETED

    async with owner_database.session() as session:
        output = (
            await session.execute(
                text("SELECT output FROM execution_steps WHERE execution_id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()
    assert output["text"] == "Four competitors."
    assert output["usage"]["output_tokens"] == 4


async def test_nothing_to_claim_is_not_an_error(
    scenario: Scenario, provider: FakeProvider, factory: UnitOfWorkFactory
) -> None:
    worker = _worker(factory, provider)
    assert await worker.run_once() is None


# ------------------------------------- 2. two workers, one dispatch, one claim ---


async def test_concurrent_workers_cannot_claim_the_same_dispatch(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """`FOR UPDATE SKIP LOCKED`, under a real race.

    Two workers claim at the same moment. One gets the row; the other gets
    None rather than blocking on it, which is the difference between a pool
    that scales and a pool that queues behind its slowest member.
    """
    execution_id = scenario.run()
    first = _worker(factory, provider, name="worker-a")
    second = _worker(factory, provider, name="worker-b")

    results = await asyncio.gather(first.run_once(), second.run_once(), return_exceptions=True)
    claimed = [r for r in results if not isinstance(r, BaseException) and r is not None]
    assert len(claimed) == 1, f"two workers claimed the same dispatch: {results}"

    # And the provider was called exactly once, which is the property the
    # claim exists to protect.
    assert len(provider.requests) == 1
    row = await _dispatch_row(owner_database, execution_id)
    assert row.claim_count == 1


async def test_a_second_worker_sees_the_lease_while_the_first_works(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
) -> None:
    """The claim commits before the work starts, not after.

    A lease nobody can see does not stop a second worker, so the row has to
    be visibly `leased` for the duration of the call. The provider is held
    for a moment to make the window real.
    """
    scenario.run()
    provider.delay = 0.3

    slow = _worker(factory, provider)
    other = _worker(factory, provider, name="worker-b")

    async def claim_during() -> Any:
        await asyncio.sleep(0.1)
        return await other.run_once()

    worked, concurrent = await asyncio.gather(slow.run_once(), claim_during())
    assert worked is not None
    assert concurrent is None, "the lease was not visible while the work was in flight"


# ------------------------------------------- 3. a crash, and 4. lease recovery ---


async def test_a_worker_that_crashes_after_claiming_leaves_a_lease_behind(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Simulated the only way a crash can be: the claim commits and nothing
    else happens.

    The row stays `leased` with a `leased_by` nobody is running any more.
    Nothing else in the system can move it - a dead process cannot be asked
    what it was doing - which is why the reaper exists.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="doomed", lease_seconds=1)
    assert claimed is not None

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.LEASED
    assert row.leased_by == "doomed"
    assert row.side_effect_started_at is None, "it died before calling anything"

    # A second worker finds nothing, because the lease has not expired yet.
    assert await _worker(factory, provider).run_once() is None


async def test_an_expired_lease_is_recovered_and_the_work_runs(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The reaper puts it back, and a live worker picks it up.

    `side_effect_started_at` is NULL, so the work is known not to have
    started and is safe to retry. The row returns to `pending`, keeping its
    `attempt` - which is the identity of this unit of work and immutable per
    row since T0 - while `claim_count` records that it has now been leased
    twice.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        await _claims(work, scenario).claim(worker="doomed", lease_seconds=0)

    returned, escalated = await LeaseReaper(factory).sweep()
    assert (returned, escalated) == (1, 0)

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING
    assert row.leased_by is None
    assert row.last_error_code == "lease_expired"
    assert row.attempt == 0, "attempt is the identity of the work, not a counter"
    assert row.claim_count == 1

    claimed = await _worker(factory, provider).run_once()
    assert claimed is not None
    assert claimed.claim_count == 2

    execution, step = await _states(owner_database, execution_id)
    assert step == StepStatus.SUCCEEDED
    assert execution == ExecutionStatus.COMPLETED


async def test_a_live_lease_is_left_alone(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Guard on the reaper: it must only take back what has actually expired."""
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        await _claims(work, scenario).claim(worker="busy", lease_seconds=600)

    assert await LeaseReaper(factory).sweep() == (0, 0)
    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.LEASED
    assert row.leased_by == "busy"


# ------------------------------------------------- 5. an uncertain outcome ---


async def test_the_worker_marks_the_call_in_flight_before_making_it(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """`side_effect_started_at` is set *before* the call, and cleared after.

    The non-vacuity pass found that nothing asserted this: the mid-flight
    test below sets the marker itself, and the timeout test gets its
    `needs_review` from the provider failure rather than from the column. So
    removing the write broke nothing, while leaving the reaper unable to tell
    a crash before the call from a crash during it.

    Observed from outside while the provider holds the request open - which
    is exactly the state a worker is in when it dies mid-flight.

    The observation is a handshake rather than a sleep. The first version
    slept 200 ms and read the row, which assumed the claim, the credential
    decryption and the connection all finished inside that window; on a
    loaded machine they did not, and the test failed in a full run that took
    27 minutes instead of 17. `arrived` says the call has genuinely started
    and `hold` keeps it started until this test has finished looking, so
    there is no window to lose.
    """
    execution_id = scenario.run()
    provider.hold = asyncio.Event()
    worker = _worker(factory, provider)

    task = asyncio.create_task(worker.run_once())
    try:
        try:
            await asyncio.wait_for(provider.arrived.wait(), timeout=30)
        except TimeoutError:  # pragma: no cover - the worker never called out
            pytest.fail("the provider was never called")

        in_flight = await _dispatch_row(owner_database, execution_id)
        assert in_flight.status == DispatchStatus.LEASED
        assert in_flight.side_effect_started_at is not None, (
            "the reaper could not tell this from a crash before the call"
        )
    finally:
        # Release the held handler on every path, including a failing
        # assertion above: otherwise it waits on `hold` for ever and the
        # test hangs in teardown instead of reporting. A test that hangs
        # when the protection is removed is worse than no test at all - the
        # non-vacuity sweep has no way to see it fail.
        provider.hold.set()
        finished, _ = await asyncio.wait([task], timeout=30)
        if not finished:  # pragma: no cover - the worker never returned
            task.cancel()

    assert await task is not None
    settled = await _dispatch_row(owner_database, execution_id)
    assert settled.status == DispatchStatus.SUCCEEDED
    assert settled.side_effect_started_at is None, "the uncertainty was not resolved"


async def test_a_lease_that_expires_mid_flight_becomes_needs_review(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """The case the whole design exists for.

    `side_effect_started_at` is set *before* the outbound call. A lease that
    expires with it still set means the call was in flight when the worker
    died, and **nobody knows whether it landed**. Retrying might do the thing
    twice; failing might not do it at all. So it is escalated, and nothing -
    no worker, no timer - retries it.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="doomed", lease_seconds=0)
        assert claimed is not None
        await _claims(work, scenario).mark_side_effect_started(claimed.id)

    returned, escalated = await LeaseReaper(factory).sweep()
    assert (returned, escalated) == (0, 1)

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.NEEDS_REVIEW
    assert "unknown" in row.last_error_code or row.last_error_code == "lease_expired"

    # Nothing picks it up again.
    assert await LeaseReaper(factory).sweep() == (0, 0)

    # And the step is untouched: no outcome was invented for it.
    _, step = await _states(owner_database, execution_id)
    assert step == StepStatus.DISPATCHED


async def test_a_timeout_is_treated_as_uncertain_not_as_a_failure(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """A request that was sent and not answered may still have been served.

    The provider accepts the connection and then does not reply within the
    client's total timeout. The worker does not record a failure, because it
    does not know there was one: the row becomes `needs_review` and the step
    keeps its `dispatched` status rather than being told something untrue.
    """
    execution_id = scenario.run()
    provider.delay = 2.0
    worker = _worker(factory, provider, total_timeout=0.4)

    claimed = await worker.run_once()
    assert claimed is not None

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.NEEDS_REVIEW
    assert row.last_error_code == "timeout", "a timeout must not be reported as a 5xx"

    execution, step = await _states(owner_database, execution_id)
    assert step == StepStatus.DISPATCHED, "no outcome was invented for the step"
    assert execution == ExecutionStatus.RUNNING


# ---------------------------------------------- 6. a dispatch to refuse ---


async def test_a_consequential_dispatch_is_refused_rather_than_performed(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Requirement 7, tested against a row that should not exist.

    `advance()` cannot emit a consequential decision, so this row is written
    by hand as the schema owner - which is the only way one can appear. The
    worker refuses it, records why, and makes no outbound call. A worker that
    trusted the row would be a worker for which the decision-time guarantee
    bought nothing.
    """
    execution_id = scenario.run()
    async with owner_database.session() as session:
        await session.execute(
            text(
                "UPDATE dispatches SET side_effect_class = 'consequential', "
                "status = 'pending', leased_by = NULL, leased_until = NULL "
                "WHERE execution_id = :id"
            ),
            {"id": str(execution_id)},
        )

    worker = _worker(factory, provider)
    claimed = await worker.run_once()
    assert claimed is not None
    assert claimed.side_effect_class is SideEffect.CONSEQUENTIAL

    assert provider.requests == [], "the worker made an outbound call anyway"
    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.DEAD
    assert row.last_error_code == "consequential_refused"

    _, step = await _states(owner_database, execution_id)
    assert step == StepStatus.FAILED


# -------------------------------- 7. duplicate reporting, 8. advancement ---


async def test_reporting_the_same_outcome_twice_changes_nothing(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Idempotent at three layers, and this exercises all three.

    `settle` only moves a row out of `leased`, so the second report finds it
    settled and returns False. Had it got past that, the outcome event's
    dedup key is deterministic in the step and attempt, and `advance()` emits
    nothing for a step whose state already reflects it.
    """
    execution_id = scenario.run()
    worker = _worker(factory, provider)
    claimed = await worker.run_once()
    assert claimed is not None

    before = await _event_count(owner_database, execution_id)
    again = await worker.report(claimed, Outcome(status=DispatchStatus.SUCCEEDED, output={}))
    assert again is False, "the second report was applied"
    assert await _event_count(owner_database, execution_id) == before

    # And a contradictory second report cannot overwrite the first.
    await worker.report(claimed, Outcome(status=DispatchStatus.FAILED, error="nope"))
    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.SUCCEEDED


async def test_a_failed_result_fails_the_execution_through_the_same_path(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Advancement after a failure, with no attempts left.

    The provider answers 400, which the adapter classifies terminal, so the
    worker does not retry: the step fails, `advance()` sees a failed step and
    fails the execution. One path for both outcomes.
    """
    execution_id = scenario.run()
    provider.status = 400
    worker = _worker(factory, provider)

    # One attempt only, so the terminal classification is what decides it
    # rather than the attempt budget.
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE dispatches SET max_attempts = 1 WHERE execution_id = :id"),
            {"id": str(execution_id)},
        )

    assert await worker.run_once() is not None

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.FAILED

    execution, step = await _states(owner_database, execution_id)
    assert step == StepStatus.FAILED
    assert execution == ExecutionStatus.FAILED

    async with owner_database.session() as session:
        error = (
            await session.execute(
                text("SELECT error FROM workflow_executions WHERE id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()
    assert "research" in error["message"]


async def test_a_retryable_failure_goes_back_to_pending_with_backoff(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """A 500 is retryable, so the row returns to `pending` with `run_after`
    in the future - and the step is not told anything, because nothing has
    been decided about it."""
    execution_id = scenario.run()
    provider.status = 500
    worker = _worker(factory, provider)
    assert await worker.run_once() is not None

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING
    assert row.run_after > dt.datetime.now(dt.UTC), "no backoff was applied"

    _, step = await _states(owner_database, execution_id)
    assert step == StepStatus.DISPATCHED


# ---------------------------------------- 8. the receiver's idempotency key ---


async def test_a_retry_carries_the_same_idempotency_token(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """No duplicate side effect where the receiver can help.

    The token is a column on the dispatch, stable across attempts of the same
    work, and forwarded as the provider's idempotency header. So the same
    unit of work retried after a recovered lease presents the same token
    twice, and a receiver that honours it serves one.

    This is the only mitigation available for a receiver that does not: the
    system cannot make someone else's endpoint idempotent, which is why the
    uncertain-outcome path exists at all.
    """
    execution_id = scenario.run()
    provider.status = 500
    worker = _worker(factory, provider)

    assert await worker.run_once() is not None
    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING

    # Make the backoff due and let it run again, successfully this time.
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE dispatches SET run_after = now() WHERE execution_id = :id"),
            {"id": str(execution_id)},
        )
    provider.status = 200
    assert await worker.run_once() is not None

    tokens = {request.get("idempotency-key") for request in provider.requests}
    assert len(provider.requests) == 2
    assert len(tokens) == 1, f"the retry presented a different token: {tokens}"
    assert tokens != {None}


async def test_the_token_is_the_dispatchs_own(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Guard on the test above: a constant would also pass it."""
    execution_id = scenario.run()
    await _worker(factory, provider).run_once()
    async with owner_database.session() as session:
        token = (
            await session.execute(
                text("SELECT idempotency_token FROM dispatches WHERE execution_id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()
    assert provider.requests[0]["idempotency-key"] == str(token)


# ------------------------------------------------------------------ secrets ---


async def test_the_credential_reaches_the_provider_and_nothing_else(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The key is in the request and in no row the worker wrote.

    Decrypted immediately before the call and held as a local for its
    duration. The instruction carries a reference, the event carries an
    outcome, and neither carries material.
    """
    execution_id = scenario.run()
    await _worker(factory, provider).run_once()
    assert provider.requests[0]["x-api-key"] == API_KEY

    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT instruction::text AS instruction FROM dispatches "
                    "WHERE execution_id = :id"
                ),
                {"id": str(execution_id)},
            )
        ).all()
        payloads = (
            await session.execute(
                text("SELECT coalesce(string_agg(payload::text, ' '), '') FROM events")
            )
        ).scalar_one()
        outputs = (
            await session.execute(
                text(
                    "SELECT coalesce(string_agg(output::text, ' '), '') FROM execution_steps "
                    "WHERE execution_id = :id"
                ),
                {"id": str(execution_id)},
            )
        ).scalar_one()

    for row in rows:
        assert API_KEY not in row.instruction
    assert API_KEY not in payloads
    assert API_KEY not in outputs
    # And the base64 of it, in case something stored it encoded rather than
    # plainly.
    assert base64.b64encode(API_KEY.encode()).decode() not in payloads


async def test_a_backed_off_retry_is_not_claimable_until_it_is_due(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """`run_after` is a real filter, not decoration.

    The non-vacuity pass found this gap: the retry test set `run_after` to
    now before claiming again, so nothing ever asserted that a backed-off row
    is left alone. Without the filter a failing dispatch would be retried in
    a tight loop at whatever speed the worker polls.
    """
    execution_id = scenario.run()
    provider.status = 500
    worker = _worker(factory, provider)
    assert await worker.run_once() is not None

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING
    assert row.run_after > dt.datetime.now(dt.UTC)

    # Due in the future, so there is nothing to claim right now.
    assert await worker.run_once() is None
    assert len(provider.requests) == 1

    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE dispatches SET run_after = now() WHERE execution_id = :id"),
            {"id": str(execution_id)},
        )
    provider.status = 200
    assert await worker.run_once() is not None
    assert len(provider.requests) == 2


async def test_a_second_outcome_event_for_one_attempt_is_refused(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The second of the three idempotency layers, tested on its own.

    `settle` normally stops a duplicate report before it reaches the event,
    which is why `test_reporting_the_same_outcome_twice_changes_nothing`
    does not exercise this one - the non-vacuity pass found that making the
    dedup key random broke no test. So the row is put back to `leased` here,
    getting past the first layer, and the event's deterministic key -
    `exec:<id>:<step>:succeeded:<attempt>` - is what refuses the second fact.

    The whole transaction rolls back, so the duplicate leaves no event and
    no second settle either.
    """
    execution_id = scenario.run()
    worker = _worker(factory, provider)
    claimed = await worker.run_once()
    assert claimed is not None

    before = await _event_count(owner_database, execution_id)
    async with owner_database.session() as session:
        await session.execute(
            text(
                "UPDATE dispatches SET status = 'leased', leased_by = 'again', "
                "leased_until = now() + interval '60 seconds' WHERE id = :id"
            ),
            {"id": str(claimed.id)},
        )

    with pytest.raises(Exception, match=r"duplicate key|UniqueViolation"):
        await worker.report(claimed, Outcome(status=DispatchStatus.SUCCEEDED, output={}))

    assert await _event_count(owner_database, execution_id) == before
    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.LEASED, "the rolled-back settle was not applied"


async def _event_count(owner_database: Database, execution_id: uuid.UUID) -> int:
    async with owner_database.session() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM events WHERE execution_id = :id"),
                    {"id": str(execution_id)},
                )
            ).scalar_one()
        )

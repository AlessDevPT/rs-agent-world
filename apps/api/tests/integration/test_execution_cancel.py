"""Stopping an execution, and being honest about what cannot be stopped.

`02-ORCHESTRATION.md` §"Timeouts and cancellation" sets the semantics and
they are deliberately asymmetric:

> status `cancelled`; `pending` dispatches -> `cancelled`; `leased` ones are
> asked to stop and the step is marked cancelled when it reports or its
> lease expires

and then the part that is easy to implement away:

> Cancellation is where the UI must not overclaim: it says **"cancelling"**
> until the in-flight step actually reports, because a `POST` already on the
> wire to Slack cannot be recalled.

So these tests are as much about the second half as the first. Queued work
really is dropped; work a worker already holds is left alone, counted, and
reported as `cancelling`. Taking its lease away would be the tempting
version, and it would make `settle` find nothing to update - silently
dropping the record that a consequential call went out, which is the one
thing T6 and T6b exist to prevent.

Two things needed no code, which is why this change is small: `advance()`
already returns no decisions when the status is not live, so nothing can be
dispatched after a cancellation even by a reaper or a late report; and the
reducer already maps `workflow.execution_cancelled` to `cancelled` and
stamps `finished_at` for any non-live status. The service appends the event
and lets the fold write the status - one writer per field.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_dispatch_worker import FakeProvider, Scenario, _claims, _worker
from test_two_step_execution import (
    _answer,
    _dispatches,
    _execution_status,
    _start,
    _steps,
    _two_step,
)

from agentworld.domain.enums import DispatchStatus
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.dispatch import LeaseReaper
from agentworld.services.executions import ExecutionService
from agentworld.services.reconcile import AdvanceReconciler
from helpers import Actor

pytestmark = pytest.mark.integration


@pytest.fixture
async def provider() -> Any:
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


def _cancel(scenario: Scenario, execution_id: Any) -> Any:
    return scenario.client.post(
        f"/v1/projects/{scenario.project['id']}/executions/{execution_id}/cancel",
        headers=scenario.actor.headers,
    )


async def _audit_actions(owner_database: Database, execution_id: Any) -> list[str]:
    async with owner_database.session() as session:
        rows = (
            await session.execute(
                text("SELECT action FROM audit_log WHERE resource_id = :id ORDER BY created_at"),
                {"id": str(execution_id)},
            )
        ).all()
    return [str(row.action) for row in rows]


# ----------------------------------------------------------- queued work ---


async def test_cancelling_a_queued_execution_drops_its_work(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    provider: FakeProvider,
) -> None:
    """Nothing has started, so cancelling is complete and says so."""
    execution_id = scenario.run()
    queued = await _dispatches(owner_database, execution_id)
    assert [row.status for row in queued] == ["pending"]

    response = _cancel(scenario, execution_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["previous_status"] == "running"
    assert body["cancelled_dispatches"] == 1
    assert body["in_flight_dispatches"] == 0
    assert body["cancelling"] is False, "nothing was in flight, so the stop is complete"
    assert body["execution"]["status"] == "cancelled"
    assert body["execution"]["finished_at"] is not None, (
        "cancelled is terminal, so the fold must stamp a finish time"
    )

    settled = await _dispatches(owner_database, execution_id)
    assert [row.status for row in settled] == ["cancelled"]

    # And the work is really gone: the worker finds nothing to do.
    assert await _worker(factory, provider).run_once() is None
    assert provider.captured == [], "a cancelled dispatch reached the provider"


async def test_cancelling_stops_the_next_step_from_ever_running(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    provider: FakeProvider,
) -> None:
    """The point of cancelling a sequence: the rest of it does not happen.

    The researcher has already run and its answer is kept - cancelling is
    not a rollback. What stops is everything that had not started.
    """
    provider.responses = [_answer("Acme raised a round.")]
    workflow_id = _two_step(scenario)
    execution_id = _start(scenario, workflow_id)
    worker = _worker(factory, provider)

    assert await worker.run_once() is not None, "the researcher never ran"
    after_first = await _dispatches(owner_database, execution_id)
    assert [row.step_key for row in after_first] == ["research", "write"]
    assert after_first[1].status == "pending", "the writer should be queued and waiting"

    body = _cancel(scenario, execution_id).json()

    assert body["cancelled_dispatches"] == 1, "the writer's queued dispatch was not dropped"
    assert body["in_flight_dispatches"] == 0
    assert body["execution"]["status"] == "cancelled"

    # The writer never runs, and the researcher's result survives.
    assert await worker.run_once() is None, "the writer ran after the execution was cancelled"
    assert len(provider.captured) == 1, "a second provider call was made after cancelling"
    steps = await _steps(owner_database, execution_id)
    assert steps["research"].status == "succeeded"
    assert steps["research"].output["text"] == "Acme raised a round."
    final = await _dispatches(owner_database, execution_id)
    assert [row.status for row in final] == ["succeeded", "cancelled"]


# -------------------------------------------------------- in-flight work ---


async def test_work_a_worker_already_holds_is_cancelling_not_cancelled(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The honesty requirement, asserted on the response and on the row.

    The dispatch is claimed but not reported - a worker holding a lease, its
    call possibly on the wire. Cancelling must not touch it.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="holder", lease_seconds=120)
    assert claimed is not None

    body = _cancel(scenario, execution_id).json()

    assert body["cancelled_dispatches"] == 0, "a leased dispatch must not be dropped"
    assert body["in_flight_dispatches"] == 1
    assert body["cancelling"] is True, (
        "work is still in flight, so the API must not report a completed stop"
    )
    assert body["execution"]["status"] == "cancelled"

    held = await _dispatches(owner_database, execution_id)
    assert held[0].status == "leased", (
        "the lease was taken away, so the worker's report will find nothing to settle "
        "and a consequential call could go unrecorded"
    )


async def test_a_worker_reporting_after_a_cancellation_still_settles(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The reason the lease is left alone: the outcome still has to land.

    A call that went out must be recorded whatever the user pressed. The
    execution stays cancelled - the fold refuses to patch a settled
    execution - and no further work is dispatched.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="holder", lease_seconds=120)
    assert claimed is not None
    assert _cancel(scenario, execution_id).json()["cancelling"] is True

    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        settled = await _claims(work, scenario).settle(
            claimed.id,
            status=DispatchStatus.SUCCEEDED,
        )
    assert settled is True, "the report was lost: the cancellation stole the lease"

    rows = await _dispatches(owner_database, execution_id)
    assert [row.status for row in rows] == ["succeeded"]
    assert await _execution_status(owner_database, execution_id) == "cancelled", (
        "a late report reopened a cancelled execution"
    )


# ------------------------------------------------------------- refusals ---


async def test_a_finished_execution_cannot_be_cancelled(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    provider: FakeProvider,
) -> None:
    """Refused rather than ignored.

    Pressing cancel on a completed execution means the caller believes
    something is still running. Answering 204 would leave them believing it.
    """
    execution_id = scenario.run()
    assert await _worker(factory, provider).run_once() is not None
    assert await _execution_status(owner_database, execution_id) == "completed"

    response = _cancel(scenario, execution_id)

    assert response.status_code == 409, response.text
    assert "completed" in response.text


async def test_cancelling_twice_is_refused(scenario: Scenario, owner_database: Database) -> None:
    execution_id = scenario.run()
    assert _cancel(scenario, execution_id).status_code == 200

    again = _cancel(scenario, execution_id)

    assert again.status_code == 409, again.text
    assert "cancelled" in again.text
    rows = await _dispatches(owner_database, execution_id)
    assert [row.status for row in rows] == ["cancelled"], "the second call changed a row"


async def test_an_unknown_execution_is_not_found(scenario: Scenario) -> None:
    response = _cancel(scenario, "00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------- audit ---


async def test_the_cancellation_is_audited(scenario: Scenario, owner_database: Database) -> None:
    """Who stopped it, and what it stopped, in the append-only table."""
    execution_id = scenario.run()

    _cancel(scenario, execution_id)

    assert "execution.cancel" in await _audit_actions(owner_database, execution_id)


# ------------------------------------------- the side effect must survive ---


async def _error_code(owner_database: Database, execution_id: Any) -> str | None:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text("SELECT last_error_code FROM dispatches WHERE execution_id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()


async def _side_effect_marked(owner_database: Database, execution_id: Any) -> bool:
    async with owner_database.session() as session:
        return bool(
            (
                await session.execute(
                    text(
                        "SELECT side_effect_started_at IS NOT NULL FROM dispatches "
                        "WHERE execution_id = :id"
                    ),
                    {"id": str(execution_id)},
                )
            ).scalar_one()
        )


async def test_cancelling_never_erases_a_call_that_may_have_landed(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The marker outlives the cancellation, because the call might not have.

    `side_effect_started_at` is the record that a request left the process.
    Once it is set, nobody can say whether the thing happened - and that is
    exactly when a user is most likely to press cancel. If cancelling
    cleared or settled the row, the evidence that something may have gone
    out would vanish with it.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claims = _claims(work, scenario)
        claimed = await claims.claim(worker="holder", lease_seconds=120)
        assert claimed is not None
        await claims.mark_side_effect_started(claimed.id)
    assert await _side_effect_marked(owner_database, execution_id) is True

    body = _cancel(scenario, execution_id).json()

    assert body["in_flight_dispatches"] == 1
    assert body["cancelling"] is True
    assert await _side_effect_marked(owner_database, execution_id) is True, (
        "cancelling erased the record that a call may already have landed"
    )
    rows = await _dispatches(owner_database, execution_id)
    assert rows[0].status == "leased", "the row was settled on the user's behalf"


async def test_a_late_failure_after_cancellation_is_still_recorded(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """A failure is evidence too, and cancelling must not swallow it."""
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="holder", lease_seconds=120)
    assert claimed is not None
    _cancel(scenario, execution_id)

    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        settled = await _claims(work, scenario).settle(
            claimed.id,
            status=DispatchStatus.FAILED,
            error="provider refused",
            error_code="provider_error",
        )
    assert settled is True, "the late failure was lost"

    rows = await _dispatches(owner_database, execution_id)
    assert rows[0].status == "failed"
    assert await _error_code(owner_database, execution_id) == "provider_error", (
        "the cancellation overwrote the real reason the call failed"
    )
    assert await _execution_status(owner_database, execution_id) == "cancelled"


# ------------------------------------ the reaper and the reconciler after ---


async def test_the_reconciler_does_not_advance_a_cancelled_execution(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """Nothing re-animates it. `advance()` refuses a non-live status."""
    execution_id = scenario.run()
    _cancel(scenario, execution_id)

    reconciler = AdvanceReconciler(factory, executions=ExecutionService(factory), grace_seconds=0)
    result = await reconciler.sweep(limit=10)

    assert result.advanced == 0, f"a cancelled execution was advanced: {result}"
    assert result.failures == 0
    rows = await _dispatches(owner_database, execution_id)
    assert [row.status for row in rows] == ["cancelled"], "the sweep created work"


async def test_a_lease_that_expires_after_cancellation_does_not_resume_the_work(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    provider: FakeProvider,
) -> None:
    """The case the asymmetry creates, and the one worth proving.

    A dispatch leased when the user cancelled is left alone on purpose. If
    its worker then dies, the reaper returns it to `pending` - because its
    call had not started, so retrying is safe *as far as the reaper can
    see*. But the execution is cancelled, and performing that work now would
    be a real model call for work a user stopped.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="doomed", lease_seconds=0)
    assert claimed is not None
    _cancel(scenario, execution_id)

    returned, escalated = await LeaseReaper(factory).sweep()
    assert (returned, escalated) == (1, 0), (
        "the reaper did not return the abandoned lease, so this test is not exercising "
        "the window it exists for"
    )

    # Whatever the row now reads, no provider call may be made for an
    # execution the user stopped.
    performed = await _worker(factory, provider).run_once()
    assert provider.captured == [], (
        "a provider call was made for a cancelled execution after its lease expired. "
        f"The worker claimed {performed.id if performed else None}"
    )
    assert await _execution_status(owner_database, execution_id) == "cancelled"

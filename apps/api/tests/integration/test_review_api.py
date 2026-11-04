"""Resolving an uncertain outcome: who may, how once, and what is kept.

`needs_review` is the only state in this system that requires a person, and
the thing being tested is mostly refusal: a viewer may look and not decide, a
retry without acknowledgement is refused, a second resolution loses, and a
resolution that fails leaves the dispatch exactly as stuck as it was.

The history tests are the ones that answer the question T6b was set: can
`audit_log` carry the recovery history, or does it need a column? Every
`dispatch.recovered` row survives a resolution, and the count a reviewer sees
is read from them.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_dispatch_worker import (
    FakeProvider,
    Scenario,
    _claims,
    _dispatch_row,
    _park,
    _states,
)

from agentworld.domain.enums import DispatchStatus, ExecutionStatus, MemberRole, StepStatus
from agentworld.domain.errors import ConflictError, ValidationFailedError
from agentworld.domain.principals import UserPrincipal
from agentworld.infra.db import Database
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.context import CallContext
from agentworld.services.dispatch import LeaseReaper
from agentworld.services.executions import ExecutionService
from agentworld.services.review import Resolution, ReviewService
from helpers import Actor, FakeAuthProvider, seed_membership, seed_user

pytestmark = pytest.mark.integration

NOTE = "Checked the provider dashboard: the call never arrived."


@pytest.fixture
async def provider() -> Any:
    from test_dispatch_worker import provider as upstream

    async for state in upstream.__wrapped__():  # type: ignore[attr-defined]
        yield state


@pytest.fixture
def scenario(client: TestClient, actor: Actor, provider: FakeProvider) -> Scenario:
    return Scenario(client, actor, provider)


async def _stick_it(factory: UnitOfWorkFactory, scenario: Scenario) -> tuple[uuid.UUID, uuid.UUID]:
    """Get one dispatch into `needs_review` the way production does.

    A worker claims it with a zero-second lease, marks its outbound call in
    flight, and vanishes. The reaper then finds a lease that expired with a
    call in flight, which is the one case nobody can resolve automatically.
    """
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="doomed", lease_seconds=0)
        assert claimed is not None
        await _claims(work, scenario).mark_side_effect_started(claimed.id)
    assert await LeaseReaper(factory).sweep() == (0, 1)
    return execution_id, claimed.id


def _queue(scenario: Scenario, actor: Actor | None = None) -> Any:
    who = actor or scenario.actor
    return scenario.client.get(
        f"/v1/projects/{scenario.project_id}/dispatches", headers=who.headers
    )


def _resolve(
    scenario: Scenario, dispatch_id: uuid.UUID, actor: Actor | None = None, **body: Any
) -> Any:
    who = actor or scenario.actor
    payload: dict[str, Any] = {"outcome": "failed", "note": NOTE}
    payload.update(body)
    return scenario.client.post(
        f"/v1/projects/{scenario.project_id}/dispatches/{dispatch_id}/resolve",
        json=payload,
        headers=who.headers,
    )


async def _audit(owner_database: Database, action: str) -> Sequence[Any]:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text(
                    "SELECT actor_type, actor_id, resource_id, metadata::text AS metadata "
                    "FROM audit_log WHERE action = :action ORDER BY created_at"
                ),
                {"action": action},
            )
        ).all()


# ------------------------------------------------------------- the queue ---


async def test_the_queue_shows_what_is_stuck_and_why(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """A viewer can see that work is stuck without being able to decide."""
    execution_id, dispatch_id = await _stick_it(factory, scenario)

    response = _queue(scenario)
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == str(dispatch_id)
    assert item["execution_id"] == str(execution_id)
    assert item["step_key"] == "research"
    assert item["kind"] == "managed_agent_turn"
    assert "unknown" in (item["last_error"] or "")
    # One recovery got it here, counted from the audit trail rather than a
    # column - which is the whole of what T6b needed from the history.
    assert item["previous_recoveries"] == 1


async def test_an_empty_queue_is_an_empty_list(scenario: Scenario) -> None:
    scenario.run()
    assert _queue(scenario).json() == []


async def test_the_count_grows_with_each_recovery(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Two trips through the reaper, two records, and the reviewer sees both.

    This is the concrete requirement `audit_log` had to satisfy: a person
    deciding what to do needs to know this has happened before. It does, and
    no column was needed.
    """
    _, dispatch_id = await _stick_it(factory, scenario)

    # Put it back and let it get stuck again, as a retry would.
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE dispatches SET status = 'pending' WHERE id = :id"),
            {"id": str(dispatch_id)},
        )
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        claimed = await _claims(work, scenario).claim(worker="doomed-again", lease_seconds=0)
        assert claimed is not None
        await _claims(work, scenario).mark_side_effect_started(claimed.id)
    assert await LeaseReaper(factory).sweep() == (0, 1)

    assert _queue(scenario).json()[0]["previous_recoveries"] == 2
    assert len(await _audit(owner_database, "dispatch.recovered")) == 2


# -------------------------------------------------------- authorization ---


async def test_a_viewer_may_look_and_may_not_resolve(
    client: TestClient,
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    auth: FakeAuthProvider,
    owner_database: Database,
) -> None:
    """The split in privilege: reading is `viewer`, deciding is `admin`.

    Seeing that an execution is stuck is operational awareness. Asserting
    what happened to an effect nobody observed is not.
    """
    _, dispatch_id = await _stick_it(factory, scenario)

    viewer = Actor(auth, "viewer@example.com")
    await seed_user(owner_database, viewer.user_id, viewer.email)
    await seed_membership(owner_database, scenario.workspace_id, viewer.user_id, MemberRole.VIEWER)

    assert _queue(scenario, viewer).status_code == 200
    refused = _resolve(scenario, dispatch_id, viewer)
    assert refused.status_code == 403, refused.text

    row = await _dispatch_row(owner_database, uuid.UUID(_queue(scenario).json()[0]["execution_id"]))
    assert row.status == DispatchStatus.NEEDS_REVIEW


async def test_a_member_may_not_resolve_either(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    auth: FakeAuthProvider,
    owner_database: Database,
) -> None:
    """`execution.retry_step` is `member`; this is not that.

    Retrying a step whose outcome is known is routine. Asserting the outcome
    of an unobservable external effect is a different decision, and the
    matrix says so.
    """
    _, dispatch_id = await _stick_it(factory, scenario)
    member = Actor(auth, "member@example.com")
    await seed_user(owner_database, member.user_id, member.email)
    await seed_membership(owner_database, scenario.workspace_id, member.user_id, MemberRole.MEMBER)
    assert _resolve(scenario, dispatch_id, member).status_code == 403


async def test_a_non_member_sees_nothing(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    auth: FakeAuthProvider,
    owner_database: Database,
) -> None:
    _, dispatch_id = await _stick_it(factory, scenario)
    outsider = Actor(auth, "outsider@example.com")
    await seed_user(owner_database, outsider.user_id, outsider.email)
    assert _queue(scenario, outsider).status_code == 404
    assert _resolve(scenario, dispatch_id, outsider).status_code == 404


# ------------------------------------------------------- the three outcomes ---


async def test_asserting_it_succeeded_advances_the_execution(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """The person says the effect landed, and the execution finishes.

    Through `advance_once`, the same path the run and the worker use - not a
    second way to move an execution.
    """
    execution_id, dispatch_id = await _stick_it(factory, scenario)

    response = _resolve(
        scenario,
        dispatch_id,
        outcome="succeeded",
        note="Confirmed in the provider's dashboard: the call was served.",
        output={"text": "recovered by hand"},
    )
    assert response.status_code == 204, response.text

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.SUCCEEDED
    assert row.side_effect_started_at is None

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
    assert output["text"] == "recovered by hand"
    # The event says a person asserted this rather than a provider reporting
    # it, which six months later is the whole of what a reader needs.
    assert output.get("resolved_by_review") is None, "the flag belongs on the event"
    async with owner_database.session() as session:
        payload = (
            await session.execute(
                text(
                    "SELECT payload FROM events WHERE execution_id = :id "
                    "AND type = 'workflow.step_succeeded'"
                ),
                {"id": str(execution_id)},
            )
        ).scalar_one()
    assert payload["resolved_by_review"] is True


async def test_asserting_it_failed_fails_the_execution(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """`failed` becomes `dead`, not `failed`.

    A human has decided this work is over, and `failed` is the retryable
    state - nothing should pick it up again because attempts happened to be
    left on the row.
    """
    execution_id, dispatch_id = await _stick_it(factory, scenario)
    assert _resolve(scenario, dispatch_id, outcome="failed").status_code == 204

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.DEAD
    # `_dispatch_row` selects a fixed column list, so this one is its own
    # query rather than an attribute that is not there.
    async with owner_database.session() as session:
        finished = (
            await session.execute(
                text("SELECT finished_at FROM dispatches WHERE execution_id = :id"),
                {"id": str(execution_id)},
            )
        ).scalar_one()
    assert finished is not None

    execution, step = await _states(owner_database, execution_id)
    assert step == StepStatus.FAILED
    assert execution == ExecutionStatus.FAILED


async def test_retry_requires_acknowledgement(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """The gate on the one outcome that acts rather than records.

    Retrying may repeat something that already happened, so consent is not
    inferred from the request having been sent.
    """
    execution_id, dispatch_id = await _stick_it(factory, scenario)

    refused = _resolve(scenario, dispatch_id, outcome="retry")
    assert refused.status_code == 422, refused.text
    assert "already happened" in refused.text

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.NEEDS_REVIEW, "a refusal must change nothing"

    accepted = _resolve(scenario, dispatch_id, outcome="retry", acknowledged=True)
    assert accepted.status_code == 204, accepted.text

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING
    assert row.side_effect_started_at is None, "the next attempt starts its own clock"
    _, step = await _states(owner_database, execution_id)
    assert step == StepStatus.DISPATCHED, "a retry asserts nothing about the step"


async def test_a_retried_dispatch_can_be_claimed_and_run(
    scenario: Scenario,
    provider: FakeProvider,
    factory: UnitOfWorkFactory,
    owner_database: Database,
) -> None:
    """The retry is real work, not a status change.

    And it carries the same idempotency token, so a receiver that honours one
    still sees a single request.
    """
    from test_dispatch_worker import _worker

    execution_id, dispatch_id = await _stick_it(factory, scenario)
    async with owner_database.session() as session:
        token = (
            await session.execute(
                text("SELECT idempotency_token FROM dispatches WHERE id = :id"),
                {"id": str(dispatch_id)},
            )
        ).scalar_one()

    assert _resolve(scenario, dispatch_id, outcome="retry", acknowledged=True).status_code == 204
    assert await _worker(factory, provider).run_once() is not None

    assert provider.requests[-1]["idempotency-key"] == str(token)
    execution, step = await _states(owner_database, execution_id)
    assert step == StepStatus.SUCCEEDED
    assert execution == ExecutionStatus.COMPLETED


# ------------------------------------------------ once, and only once ---


async def test_a_second_resolution_is_refused(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Duplicate resolutions: the first wins, the second is a 409."""
    _, dispatch_id = await _stick_it(factory, scenario)
    assert _resolve(scenario, dispatch_id, outcome="failed").status_code == 204

    again = _resolve(scenario, dispatch_id, outcome="succeeded")
    assert again.status_code == 409, again.text
    assert "not awaiting review" in again.text

    # And the first decision stands.
    assert len(await _audit(owner_database, "dispatch.resolve_review")) == 1


async def test_two_concurrent_resolutions_produce_one(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Conflicting resolutions, raced at the service rather than over HTTP.

    The first version of this test fired two `TestClient` requests through
    `asyncio.to_thread`, and the non-vacuity pass showed it was not racing:
    removing the status guard from the UPDATE broke nothing, because
    Starlette's test portal serialised the two requests and the service's
    pre-read check caught the second. Two calls into `ReviewService` on one
    event loop, each in its own transaction, is a real race.

    One resolution lands; the other finds nothing in `needs_review` to
    resolve. The alternative - two audit rows asserting different things
    about the same uncertain effect - is what the guard on the UPDATE
    prevents.
    """
    _, dispatch_id = await _stick_it(factory, scenario)
    principal = _admin_principal(scenario)
    service = ReviewService(factory, executions=ExecutionService(factory))

    outcomes = await asyncio.gather(
        service.resolve(
            principal,
            CallContext.background(),
            scenario.project_id,
            dispatch_id,
            outcome=Resolution.SUCCEEDED,
            note="Asserting it landed, concurrently.",
        ),
        service.resolve(
            principal,
            CallContext.background(),
            scenario.project_id,
            dispatch_id,
            outcome=Resolution.FAILED,
            note="Asserting it did not land, concurrently.",
        ),
        return_exceptions=True,
    )

    conflicts = [r for r in outcomes if isinstance(r, ConflictError)]
    succeeded = [r for r in outcomes if not isinstance(r, BaseException)]
    assert len(succeeded) == 1, f"both resolutions were applied: {outcomes}"
    assert len(conflicts) == 1, f"the loser did not report a conflict: {outcomes}"
    assert len(await _audit(owner_database, "dispatch.resolve_review")) == 1


async def test_the_note_floor_is_the_services_rule_not_only_the_schemas(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """Tested at the service because the schema shadows it over HTTP.

    `ResolveRequest` has `min_length`, so a short note is a 422 from pydantic
    before the service is reached - which the non-vacuity pass exposed:
    removing the service's own check broke nothing. The rule belongs to the
    service, because the worker, a future CLI and anything else that resolves
    one must obey it too.
    """
    _, dispatch_id = await _stick_it(factory, scenario)
    service = ReviewService(factory, executions=ExecutionService(factory))

    with pytest.raises(ValidationFailedError, match="at least"):
        await service.resolve(
            _admin_principal(scenario),
            CallContext.background(),
            scenario.project_id,
            dispatch_id,
            outcome=Resolution.FAILED,
            note="ok",
        )


def _admin_principal(scenario: Scenario) -> UserPrincipal:
    """The scenario's owner, as the service sees them.

    The two tests above call the service directly - one because HTTP cannot
    produce a real race here, the other because the schema answers first - so
    they build the principal a request would have built.
    """
    return UserPrincipal(
        user_id=scenario.actor.user_id,
        email=scenario.actor.email,
        memberships={scenario.workspace_id: MemberRole.OWNER},
    )


async def test_a_dispatch_not_awaiting_review_cannot_be_resolved(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """A pending dispatch is not a question anybody has to answer."""
    execution_id = scenario.run()
    async with factory.begin(visible_workspaces={scenario.workspace_id}) as work:
        dispatches = await work.dispatch_reads(
            __import__("agentworld.repositories.base", fromlist=["TenantScope"]).TenantScope(
                scenario.workspace_id, scenario.project_id
            )
        ).for_execution(execution_id)
    response = _resolve(scenario, dispatches[0].id, outcome="failed")
    assert response.status_code == 409
    assert "not awaiting review" in response.text


# ----------------------------------------------------- the note and the audit ---


@pytest.mark.parametrize("note", ["", "   ", "ok", "done"])
async def test_a_note_that_says_nothing_is_refused(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database, note: str
) -> None:
    """The note is the only record of *why*, so it is mandatory.

    The floor is crude on purpose: it is not judging prose, it is refusing
    the two words a person types when a field is in their way.
    """
    execution_id, dispatch_id = await _stick_it(factory, scenario)
    response = _resolve(scenario, dispatch_id, outcome="failed", note=note)
    assert response.status_code == 422, response.text

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.NEEDS_REVIEW


async def test_the_audit_row_names_the_person_and_keeps_the_note(
    scenario: Scenario, actor: Actor, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """Who decided, what they asserted, and why - in one row nothing can edit."""
    _, dispatch_id = await _stick_it(factory, scenario)
    assert (
        _resolve(scenario, dispatch_id, outcome="retry", acknowledged=True, note=NOTE).status_code
        == 204
    )

    rows = await _audit(owner_database, "dispatch.resolve_review")
    assert len(rows) == 1
    entry = rows[0]
    assert entry.actor_type == "user"
    assert entry.actor_id == actor.user_id
    assert entry.resource_id == dispatch_id

    metadata = json.loads(entry.metadata)
    assert metadata["outcome"] == "retry"
    assert metadata["note"] == NOTE
    assert metadata["acknowledged"] is True
    assert metadata["from"] == "needs_review"
    assert metadata["to"] == "pending"
    assert metadata["step_key"] == "research"
    # What the reaper had recorded, carried forward rather than lost.
    assert "unknown" in metadata["previous_error"]


async def _dispatch_finished_at(owner_database: Database, dispatch_id: uuid.UUID) -> Any:
    async with owner_database.session() as session:
        return (
            await session.execute(
                text("SELECT finished_at FROM dispatches WHERE id = :id"),
                {"id": str(dispatch_id)},
            )
        ).scalar_one()


@pytest.mark.parametrize(
    ("outcome", "acknowledged", "finishes"),
    [
        (Resolution.SUCCEEDED, False, True),
        (Resolution.FAILED, False, True),
        (Resolution.RETRY, True, False),
    ],
)
async def test_a_resolution_stamps_finished_at_only_when_it_ends_the_work(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    outcome: Resolution,
    acknowledged: bool,
    finishes: bool,
) -> None:
    """The other writer of `finished_at`, held to the same rule as `settle`.

    `succeeded` and `failed` end the dispatch - `failed` becomes `dead` - so
    both get a finish time. `retry` puts the row back to `pending`, which is
    the work about to happen again, so it must not be stamped. Both writers
    now read `DispatchStatus.is_terminal`, and this is what stops them
    drifting apart.
    """
    _, dispatch_id = await _stick_it(factory, scenario)
    assert await _dispatch_finished_at(owner_database, dispatch_id) is None

    response = _resolve(scenario, dispatch_id, outcome=outcome.value, acknowledged=acknowledged)
    assert response.status_code == 204, response.text

    stamped = await _dispatch_finished_at(owner_database, dispatch_id) is not None
    assert stamped is finishes, (
        f"resolving as {outcome.value}: finished_at stamped={stamped}, expected {finishes}"
    )

    if not finishes:
        # A `retry` resolution leaves the row `pending` with `run_after` now,
        # and `DispatchWorker` claims across *every* workspace - so leaving it
        # claimable lets a later test's worker pick up this row instead of its
        # own. That is exactly how this test first broke
        # `test_a_retried_dispatch_can_be_claimed_and_run`, whose provider was
        # then never called.
        await _park(owner_database, dispatch_id)


async def test_resolving_preserves_every_earlier_recovery_record(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """The requirement, stated as a test.

    A resolution adds a row and replaces none. It could not overwrite one if
    it tried: the application role holds `INSERT, SELECT` on `audit_log` and
    nothing else, which is why the history needed no column of its own.
    """
    _, dispatch_id = await _stick_it(factory, scenario)
    before = await _audit(owner_database, "dispatch.recovered")
    assert len(before) == 1

    assert _resolve(scenario, dispatch_id, outcome="failed").status_code == 204

    after = await _audit(owner_database, "dispatch.recovered")
    assert [row.metadata for row in after] == [row.metadata for row in before]
    assert len(await _audit(owner_database, "dispatch.resolve_review")) == 1


async def test_a_failed_resolution_leaves_the_dispatch_stuck(
    scenario: Scenario,
    factory: UnitOfWorkFactory,
    owner_database: Database,
    monkeypatch: Any,
) -> None:
    """One transaction: the status, the step event and the audit row.

    A resolution that fails halfway must leave the dispatch exactly as stuck
    as it was - the alternative is a step told something a human never
    managed to say.
    """
    execution_id, dispatch_id = await _stick_it(factory, scenario)

    from agentworld.repositories.audit import AuditLogRepository

    original = AuditLogRepository.append

    async def explode(self: Any, **kwargs: Any) -> int:
        if kwargs.get("action") == "dispatch.resolve_review":
            raise RuntimeError("the audit write failed")
        return await original(self, **kwargs)

    monkeypatch.setattr(AuditLogRepository, "append", explode)

    # `TestClient` re-raises server exceptions rather than returning the 500
    # the handler would produce, so the assertion is on the exception. What
    # matters is below it: the state the failed resolution left behind.
    with pytest.raises(RuntimeError, match="the audit write failed"):
        _resolve(scenario, dispatch_id, outcome="succeeded")

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.NEEDS_REVIEW, "the status change was not rolled back"
    _, step = await _states(owner_database, execution_id)
    assert step == StepStatus.DISPATCHED, "the step was told something nobody asserted"
    assert await _audit(owner_database, "dispatch.resolve_review") == []


# ------------------------------------- the budget, and the project boundary ---


async def test_a_retry_resolution_does_not_grant_another_attempt(
    scenario: Scenario, factory: UnitOfWorkFactory, owner_database: Database
) -> None:
    """`retry` re-enters the queue; it does not buy a fresh attempt.

    This was true by construction - `resolve_review`'s value dict never
    mentions `attempt`, `max_attempts` or `claim_count`, and a trigger makes
    `attempt` immutable - but nothing asserted it, so nothing would notice a
    well-meaning `attempt=attempt + 1` added later to "give the human's retry
    a fair chance". The consequence of that change is a dispatch that can
    exceed its own ceiling, which is the one thing the ceiling is for.
    """
    execution_id, dispatch_id = await _stick_it(factory, scenario)

    async def budget() -> tuple[int, int, int]:
        async with owner_database.session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT attempt, max_attempts, claim_count FROM dispatches WHERE id = :id"
                    ),
                    {"id": str(dispatch_id)},
                )
            ).one()
            return (row.attempt, row.max_attempts, row.claim_count)

    before = await budget()
    assert _resolve(scenario, dispatch_id, outcome="retry", acknowledged=True).status_code == 204

    row = await _dispatch_row(owner_database, execution_id)
    assert row.status == DispatchStatus.PENDING, "the retry did not re-enter the queue"
    assert await budget() == before, "the resolution moved the attempt budget"


async def test_the_queue_and_the_resolution_stop_at_the_project_boundary(
    scenario: Scenario, factory: UnitOfWorkFactory
) -> None:
    """Project scope is the second half of the authorization story.

    Both routes are addressed by project, and the same person is `owner` of
    every project in this workspace - so role alone does not stop them
    reading or resolving a dispatch through a project that does not own it.
    The `project_id` predicate in `needing_review` and `resolve_review` is
    what does, and before this test nothing exercised it: every other case
    uses one project, where a missing predicate is invisible.

    The second half matters more than the first. Listing the wrong project's
    queue returning nothing is tidy; being able to *resolve* another
    project's uncertain outcome by putting its id in a URL would be a
    cross-project write.
    """
    _, dispatch_id = await _stick_it(factory, scenario)
    elsewhere = scenario.client.post(
        f"/v1/workspaces/{scenario.workspace_id}/projects",
        json={"name": "Elsewhere"},
        headers=scenario.actor.headers,
    )
    assert elsewhere.status_code == 201, elsewhere.text
    other_id = elsewhere.json()["id"]

    queue = scenario.client.get(
        f"/v1/projects/{other_id}/dispatches", headers=scenario.actor.headers
    )
    assert queue.status_code == 200, queue.text
    assert queue.json() == [], "another project's stuck dispatch is in this project's queue"

    resolved = scenario.client.post(
        f"/v1/projects/{other_id}/dispatches/{dispatch_id}/resolve",
        json={"outcome": "succeeded", "note": NOTE},
        headers=scenario.actor.headers,
    )
    assert resolved.status_code == 404, (
        f"a dispatch was resolvable through a project that does not own it: {resolved.text}"
    )

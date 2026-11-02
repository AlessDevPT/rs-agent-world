"""Out-of-order and duplicate events, against the real columns. T0 / D2.

These exercise `DerivedStateRepository.apply` rather than the HTTP ingest
endpoint, deliberately. The guards are predicates in the UPDATE statement, so
this is the layer where they either exist or do not; and from D1 onwards a
`workflow.step_*` event is only ingestible for a step dispatched to the
posting agent, which would make an end-to-end version of these tests about
producer authority instead of about ordering.

What each test asserts, in both directions: the in-order case still applies,
and the out-of-order case does not. Only the pair is meaningful. A guard that
refuses everything passes the second half on its own, which is exactly how
migration 0016's first attempt looked right.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from agentworld.domain.enums import ApiKeyScope
from agentworld.domain.principals import KeyPrincipal
from agentworld.domain.protocol import PROTOCOL_VERSION, parse_envelope
from agentworld.domain.reducers import fold
from agentworld.infra.db import Database
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.ingest import IngestService

pytestmark = pytest.mark.integration

#: Both in the recent past, relative to the run. Fixed dates would eventually
#: drift into the future, and ingest refuses an event dated more than
#: `INGEST_MAX_FUTURE_SKEW_SECONDS` ahead - which would make these tests fail
#: for a reason that has nothing to do with ordering.
_NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
EARLIER = _NOW - dt.timedelta(minutes=10)
LATER = _NOW - dt.timedelta(minutes=5)


class Fixture:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.workflow_id = uuid.uuid4()
        self.version_id = uuid.uuid4()
        self.step_id = uuid.uuid4()
        self.execution_id = uuid.uuid4()
        self.execution_step_id = uuid.uuid4()
        #: The ref a producer uses for the step: `<execution>:<step_key>`.
        self.step_ref = f"{self.execution_id}:shape"


@pytest.fixture
async def seeded(owner_database: Database) -> Fixture:
    """One execution, one step, both freshly created and not yet folded."""
    f = Fixture()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'stale@example.com')"),
            {"i": f.user_id},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :s, :n, :u)"),
            {"i": f.workspace_id, "s": "stale", "n": "Stale", "u": f.user_id},
        )
        await session.execute(
            text("INSERT INTO projects (id, workspace_id, slug, name) VALUES (:i, :w, :s, :n)"),
            {"i": f.project_id, "w": f.workspace_id, "s": "stale", "n": "Stale"},
        )
        await session.execute(
            text(
                "INSERT INTO workflows (id, workspace_id, project_id, slug, name) "
                "VALUES (:i, :w, :p, 'flow', 'Flow')"
            ),
            {"i": f.workflow_id, "w": f.workspace_id, "p": f.project_id},
        )
        await session.execute(
            text(
                "INSERT INTO workflow_versions "
                "(id, workflow_id, workspace_id, project_id, version, graph) "
                "VALUES (:i, :wf, :w, :p, 1, '{}'::jsonb)"
            ),
            {"i": f.version_id, "wf": f.workflow_id, "w": f.workspace_id, "p": f.project_id},
        )
        await session.execute(
            text(
                "INSERT INTO workflow_steps "
                "(id, workflow_version_id, workspace_id, project_id, step_key, name, kind) "
                "VALUES (:i, :v, :w, :p, 'shape', 'Shape', 'transform')"
            ),
            {"i": f.step_id, "v": f.version_id, "w": f.workspace_id, "p": f.project_id},
        )
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(id, workspace_id, project_id, workflow_id, workflow_version_id, "
                " initiator_kind, initiator_user_id, initiator_label) "
                "VALUES (:i, :w, :p, :wf, :v, 'user', :u, 'stale@example.com')"
            ),
            {
                "i": f.execution_id,
                "w": f.workspace_id,
                "p": f.project_id,
                "wf": f.workflow_id,
                "v": f.version_id,
                "u": f.user_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO execution_steps "
                "(id, execution_id, workspace_id, project_id, workflow_version_id, "
                " workflow_step_id, step_key, status) "
                "VALUES (:i, :e, :w, :p, :v, :s, 'shape', 'pending')"
            ),
            {
                "i": f.execution_step_id,
                "e": f.execution_id,
                "w": f.workspace_id,
                "p": f.project_id,
                "v": f.version_id,
                "s": f.step_id,
            },
        )
    return f


async def _apply(
    factory: UnitOfWorkFactory,
    f: Fixture,
    event_type: str,
    kind: str,
    ref: str,
    when: dt.datetime,
    payload: dict[str, object] | None = None,
) -> int:
    """Fold one event and apply it, as ingest would, returning rows changed."""
    envelope = parse_envelope(
        {
            "dedup_key": f"{event_type}:{when.isoformat()}",
            "type": event_type,
            "protocol_version": PROTOCOL_VERSION,
            "occurred_at": when.isoformat().replace("+00:00", "Z"),
            "subject": {"kind": kind, "ref": ref},
            "payload": payload or {},
        }
    )
    scope = TenantScope(workspace_id=f.workspace_id, project_id=f.project_id)
    changed = 0
    async with factory.begin(visible_workspaces={f.workspace_id}) as work:
        derived = work.derived(scope)
        event_id = uuid.uuid4()
        for mutation in fold(envelope):
            changed += await derived.apply(mutation, event_id=event_id)
    return changed


async def _status(db: Database, table: str, row_id: uuid.UUID) -> str:
    async with db.session() as session:
        return str(
            (
                await session.execute(
                    # `table` comes from this module, never from a caller or a request.
                    text(f"SELECT status FROM {table} WHERE id = :i"),
                    {"i": row_id},
                )
            ).scalar_one()
        )


# ----------------------------------------------------------- execution steps ---


async def test_a_late_step_started_does_not_revert_a_succeeded_step(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """The failure this work item exists for.

    A worker reconnects and flushes its backlog; `step_started` from 07:00
    lands after the `step_succeeded` from 07:05 that followed it.
    """
    assert (
        await _apply(
            factory, seeded, "workflow.step_succeeded", "execution_step", seeded.step_ref, LATER
        )
        == 1
    )
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "succeeded"

    changed = await _apply(
        factory, seeded, "workflow.step_started", "execution_step", seeded.step_ref, EARLIER
    )

    assert changed == 0, "a stale event moved a step that had already finished"
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "succeeded"


async def test_events_in_order_still_apply(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """The other half of the pair. Without this, a guard that refused every
    event would pass the test above."""
    assert (
        await _apply(
            factory, seeded, "workflow.step_started", "execution_step", seeded.step_ref, EARLIER
        )
        == 1
    )
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "running"

    assert (
        await _apply(
            factory, seeded, "workflow.step_succeeded", "execution_step", seeded.step_ref, LATER
        )
        == 1
    )
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "succeeded"


async def test_a_same_timestamp_event_cannot_reopen_a_settled_step(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """The gap a timestamp guard alone leaves.

    `not_before` compares with `<=`, so an event carrying the *same*
    `occurred_at` passes it. A producer that stamps a whole batch with one
    timestamp would otherwise still be able to revert a finished step. This
    is what `Patch.unless` is for.
    """
    assert (
        await _apply(
            factory, seeded, "workflow.step_succeeded", "execution_step", seeded.step_ref, LATER
        )
        == 1
    )

    changed = await _apply(
        factory, seeded, "workflow.step_started", "execution_step", seeded.step_ref, LATER
    )

    assert changed == 0, "a same-timestamp event reopened a settled step"
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "succeeded"


async def test_a_retry_may_reopen_a_settled_step(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """The one deliberate exception, asserted positively.

    `workflow.step_retried` carries no `unless`, because reopening a failed
    step is the entire point of a retry. If this ever starts failing, the
    retry path has been disabled by a guard rather than fixed by one.
    """
    await _apply(
        factory, seeded, "workflow.step_failed", "execution_step", seeded.step_ref, EARLIER
    )
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "failed"

    changed = await _apply(
        factory,
        seeded,
        "workflow.step_retried",
        "execution_step",
        seeded.step_ref,
        LATER,
        {"attempt": 1},
    )

    assert changed == 1, "a retry could not reopen the step it exists to reopen"
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "ready"


async def test_a_late_event_cannot_move_a_step_backwards_between_live_statuses(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """The case only the timestamp guard can catch.

    Both `dispatched` and `running` are live, so the monotonicity guard has
    nothing to say about them - `unless` only protects settled statuses. A
    backlog that replays `step_dispatched` after `step_started` would
    otherwise walk the step backwards and leave the island showing a step
    waiting for a worker that is already running it.

    Found by weakening `not_before` and discovering that no test failed: the
    settled-status guard was quietly covering every case the suite had.
    """
    assert (
        await _apply(
            factory, seeded, "workflow.step_started", "execution_step", seeded.step_ref, LATER
        )
        == 1
    )
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "running"

    changed = await _apply(
        factory, seeded, "workflow.step_dispatched", "execution_step", seeded.step_ref, EARLIER
    )

    assert changed == 0, "a stale event walked a running step back to dispatched"
    assert await _status(owner_database, "execution_steps", seeded.execution_step_id) == "running"


# --------------------------------------------------------------- executions ---


async def test_a_late_running_does_not_revive_a_finished_execution(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    execution_ref = str(seeded.execution_id)
    assert (
        await _apply(
            factory,
            seeded,
            "workflow.execution_completed",
            "workflow_execution",
            execution_ref,
            LATER,
        )
        == 1
    )
    assert await _status(owner_database, "workflow_executions", seeded.execution_id) == "completed"

    changed = await _apply(
        factory,
        seeded,
        "workflow.execution_started",
        "workflow_execution",
        execution_ref,
        EARLIER,
    )

    assert changed == 0, "a stale event restarted a finished execution"
    assert await _status(owner_database, "workflow_executions", seeded.execution_id) == "completed"


async def test_a_paused_execution_can_still_be_resumed(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """`paused` is live, so it must not be in the settled set - otherwise the
    guard would make pausing an execution permanent."""
    execution_ref = str(seeded.execution_id)
    await _apply(
        factory,
        seeded,
        "workflow.execution_paused",
        "workflow_execution",
        execution_ref,
        EARLIER,
    )
    assert await _status(owner_database, "workflow_executions", seeded.execution_id) == "paused"

    changed = await _apply(
        factory, seeded, "workflow.execution_resumed", "workflow_execution", execution_ref, LATER
    )

    assert changed == 1, "a paused execution could not be resumed"
    assert await _status(owner_database, "workflow_executions", seeded.execution_id) == "running"


async def test_a_late_pause_cannot_override_a_newer_resume(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """The execution-level twin of the step case above.

    `paused` and `running` are both live, so only the timestamp guard
    separates them. Without it, a replayed pause would park an execution that
    is already running again.
    """
    execution_ref = str(seeded.execution_id)
    assert (
        await _apply(
            factory,
            seeded,
            "workflow.execution_resumed",
            "workflow_execution",
            execution_ref,
            LATER,
        )
        == 1
    )

    changed = await _apply(
        factory,
        seeded,
        "workflow.execution_paused",
        "workflow_execution",
        execution_ref,
        EARLIER,
    )

    assert changed == 0, "a stale pause overrode a newer resume"
    assert await _status(owner_database, "workflow_executions", seeded.execution_id) == "running"


# ------------------------------------------- concurrent duplicates (D6) ---


def _raw(dedup_key: str) -> dict[str, object]:
    return {
        "dedup_key": dedup_key,
        "type": "agent.heartbeat",
        "protocol_version": PROTOCOL_VERSION,
        "occurred_at": LATER.isoformat().replace("+00:00", "Z"),
        "subject": {"kind": "agent", "ref": "worker-1"},
        "payload": {},
    }


async def test_two_concurrent_batches_with_one_dedup_key_report_a_duplicate(
    owner_database: Database, factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """Dedup is a read followed by a write, so it has to be serialised.

    Before the cursor lock moved to the top of the transaction it was taken
    on the first *accepted* event, which is after the first `seen()`. Both
    batches below would then have seen `seen() is False`, and the loser would
    have hit the partition's unique index mid-transaction - failing the whole
    batch with an integrity error instead of reporting one duplicate.

    Two independent `factory.begin` calls, so these really are two
    transactions against one project rather than two awaits on one.
    """
    principal = KeyPrincipal(
        api_key_id=uuid.uuid4(),
        workspace_id=seeded.workspace_id,
        project_id=seeded.project_id,
        scope=ApiKeyScope.INGEST,
    )
    service = IngestService(factory)
    batch = [_raw("hb-concurrent")]

    first, second = await asyncio.gather(
        service.ingest(principal, batch),
        service.ingest(principal, batch),
    )

    accepted = {first.accepted, second.accepted}
    assert accepted == {0, 1}, f"expected exactly one acceptance, got {accepted}"
    assert {first.duplicates, second.duplicates} == {0, 1}

    async with owner_database.session() as session:
        stored = (
            await session.execute(
                text(
                    "SELECT count(*) FROM events "
                    "WHERE project_id = :p AND dedup_key = 'hb-concurrent'"
                ),
                {"p": seeded.project_id},
            )
        ).scalar_one()
    assert stored == 1, "the same dedup_key was stored twice"


async def test_an_all_duplicate_batch_still_takes_the_lock(
    factory: UnitOfWorkFactory, seeded: Fixture
) -> None:
    """The case that previously took no lock at all.

    Every event in the batch is already stored, so the loop never reached
    `next_seq` and so never locked anything. It is only observable as a
    missing lock, which no assertion can see directly - what this test
    guards is that the lock call is not quietly skipped on this path, by
    exercising it.
    """
    principal = KeyPrincipal(
        api_key_id=uuid.uuid4(),
        workspace_id=seeded.workspace_id,
        project_id=seeded.project_id,
        scope=ApiKeyScope.INGEST,
    )
    service = IngestService(factory)
    batch = [_raw("hb-once")]

    assert (await service.ingest(principal, batch)).accepted == 1
    repeat = await service.ingest(principal, batch)

    assert repeat.accepted == 0
    assert repeat.duplicates == 1
    assert repeat.outcomes[0].seq is None, "a duplicate must not consume a sequence number"

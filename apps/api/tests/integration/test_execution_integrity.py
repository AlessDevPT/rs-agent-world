"""Execution integrity: the initiator, the version binding, the outbox.

Each constraint here prevents something that would otherwise be discovered
much later - an execution with no recorded initiator, a step attributed to a
graph it never ran against, two dispatches for one step attempt, or a leased
row the reaper can never reclaim.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration


class Fixture:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.workflow_id = uuid.uuid4()
        self.version_a = uuid.uuid4()
        self.version_b = uuid.uuid4()
        self.step_a = uuid.uuid4()
        self.step_b = uuid.uuid4()
        self.execution_id = uuid.uuid4()
        self.execution_step_id = uuid.uuid4()


@pytest.fixture
async def seeded(owner_database: Database) -> Fixture:
    """One workflow, two published versions, one step in each, one execution."""
    f = Fixture()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'exec@example.com')"),
            {"i": f.user_id},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :s, :n, :u)"),
            {"i": f.workspace_id, "s": "exec", "n": "Exec", "u": f.user_id},
        )
        await session.execute(
            text("INSERT INTO projects (id, workspace_id, slug, name) VALUES (:i, :w, :s, :n)"),
            {"i": f.project_id, "w": f.workspace_id, "s": "exec", "n": "Exec"},
        )
        await session.execute(
            text(
                "INSERT INTO workflows (id, workspace_id, project_id, slug, name) "
                "VALUES (:i, :w, :p, :s, :n)"
            ),
            {"i": f.workflow_id, "w": f.workspace_id, "p": f.project_id, "s": "run", "n": "Run"},
        )
        for version_id, number in ((f.version_a, 1), (f.version_b, 2)):
            await session.execute(
                text(
                    "INSERT INTO workflow_versions "
                    "(id, workflow_id, workspace_id, project_id, version, graph, published_at) "
                    "VALUES (:i, :wf, :w, :p, :v, '{}'::jsonb, now())"
                ),
                {
                    "i": version_id,
                    "wf": f.workflow_id,
                    "w": f.workspace_id,
                    "p": f.project_id,
                    "v": number,
                },
            )
        for step_id, version_id in ((f.step_a, f.version_a), (f.step_b, f.version_b)):
            await session.execute(
                text(
                    "INSERT INTO workflow_steps "
                    "(id, workflow_version_id, workspace_id, project_id, step_key, name, kind) "
                    "VALUES (:i, :v, :w, :p, :key, :name, 'transform')"
                ),
                {
                    "i": step_id,
                    "v": version_id,
                    "w": f.workspace_id,
                    "p": f.project_id,
                    "key": "shape",
                    "name": "Shape",
                },
            )
        # An execution bound to version A, initiated by a user.
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(id, workspace_id, project_id, workflow_id, workflow_version_id, "
                " initiator_kind, initiator_user_id, initiator_label) "
                "VALUES (:i, :w, :p, :wf, :v, 'user', :u, :label)"
            ),
            {
                "i": f.execution_id,
                "w": f.workspace_id,
                "p": f.project_id,
                "wf": f.workflow_id,
                "v": f.version_a,
                "u": f.user_id,
                "label": "exec@example.com",
            },
        )
    return f


async def _insert_execution(
    db: Database, f: Fixture, *, kind: str, user_id: uuid.UUID | None, trigger_id: uuid.UUID | None
) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(workspace_id, project_id, workflow_id, workflow_version_id, "
                " initiator_kind, initiator_user_id, initiator_trigger_id, initiator_label) "
                "VALUES (:w, :p, :wf, :v, :kind, :u, :t, :label)"
            ),
            {
                "w": f.workspace_id,
                "p": f.project_id,
                "wf": f.workflow_id,
                "v": f.version_a,
                "kind": kind,
                "u": user_id,
                "t": trigger_id,
                "label": f"{kind} initiator",
            },
        )


# ------------------------------------------------------------- the initiator ---


async def test_a_user_initiated_execution_names_a_user(
    owner_database: Database, seeded: Fixture
) -> None:
    await _insert_execution(
        owner_database, seeded, kind="user", user_id=seeded.user_id, trigger_id=None
    )


async def test_a_system_initiated_execution_names_no_user(
    owner_database: Database, seeded: Fixture
) -> None:
    """The whole point: a scheduled run borrows no account's authority."""
    await _insert_execution(
        owner_database, seeded, kind="system", user_id=None, trigger_id=uuid.uuid4()
    )


@pytest.mark.parametrize(
    ("kind", "with_user"),
    [
        # A user initiator with nobody named is an audit hole.
        ("user", False),
        # A system initiator naming a user is the mistake this constraint
        # exists to prevent: it would let a run borrow that user's identity.
        ("system", True),
    ],
)
async def test_a_contradictory_initiator_is_refused(
    owner_database: Database, seeded: Fixture, kind: str, with_user: bool
) -> None:
    with pytest.raises(Exception) as caught:
        await _insert_execution(
            owner_database,
            seeded,
            kind=kind,
            user_id=seeded.user_id if with_user else None,
            trigger_id=None,
        )
    assert "initiator_user_xor_system" in str(caught.value)


async def test_an_initiator_label_is_required(owner_database: Database, seeded: Fixture) -> None:
    """Plain text, so an audit survives the trigger and the user row going."""
    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO workflow_executions "
                    "(workspace_id, project_id, workflow_id, workflow_version_id, "
                    " initiator_kind, initiator_user_id) "
                    "VALUES (:w, :p, :wf, :v, 'user', :u)"
                ),
                {
                    "w": seeded.workspace_id,
                    "p": seeded.project_id,
                    "wf": seeded.workflow_id,
                    "v": seeded.version_a,
                    "u": seeded.user_id,
                },
            )
    assert "initiator_label" in str(caught.value)


async def test_an_initiating_user_cannot_be_hard_deleted(
    owner_database: Database, seeded: Fixture
) -> None:
    """ON DELETE RESTRICT: the record must not lose who started the run.

    Deleted here is a *second* user who initiated an execution and created
    nothing else. The fixture's own user also created the workspace, so
    `workspaces_created_by_fkey` would block the delete first and the test
    would pass while proving something else.
    """
    other_user = uuid.uuid4()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'runner@example.com')"),
            {"i": other_user},
        )
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(workspace_id, project_id, workflow_id, workflow_version_id, "
                " initiator_kind, initiator_user_id, initiator_label) "
                "VALUES (:w, :p, :wf, :v, 'user', :u, 'runner@example.com')"
            ),
            {
                "w": seeded.workspace_id,
                "p": seeded.project_id,
                "wf": seeded.workflow_id,
                "v": seeded.version_a,
                "u": other_user,
            },
        )

    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": other_user})
    assert "workflow_executions_initiator_user_fkey" in str(caught.value)


async def test_one_execution_per_idempotency_key(owner_database: Database, seeded: Fixture) -> None:
    """A trigger firing twice in a minute yields one execution."""
    for _ in range(2):
        async with owner_database.session() as session:
            statement = text(
                "INSERT INTO workflow_executions "
                "(workspace_id, project_id, workflow_id, workflow_version_id, "
                " initiator_kind, initiator_label, idempotency_key) "
                "VALUES (:w, :p, :wf, :v, 'system', 'schedule run', 'tick-1')"
            )
            params = {
                "w": seeded.workspace_id,
                "p": seeded.project_id,
                "wf": seeded.workflow_id,
                "v": seeded.version_a,
            }
            if _ == 0:
                await session.execute(statement, params)
            else:
                with pytest.raises(Exception) as caught:
                    await session.execute(statement, params)
                assert "workflow_executions_idempotency_key" in str(caught.value)


# --------------------------------------------------------- the version binding ---


async def _insert_step(
    db: Database, f: Fixture, *, version_id: uuid.UUID, step_id: uuid.UUID, attempt: int = 0
) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO execution_steps "
                "(execution_id, workspace_id, project_id, workflow_version_id, "
                " workflow_step_id, step_key, attempt) "
                "VALUES (:e, :w, :p, :v, :s, 'shape', :attempt)"
            ),
            {
                "e": f.execution_id,
                "w": f.workspace_id,
                "p": f.project_id,
                "v": version_id,
                "s": step_id,
                "attempt": attempt,
            },
        )


async def test_an_execution_step_in_the_bound_version_is_accepted(
    owner_database: Database, seeded: Fixture
) -> None:
    await _insert_step(owner_database, seeded, version_id=seeded.version_a, step_id=seeded.step_a)


async def test_an_execution_step_cannot_name_another_versions_step(
    owner_database: Database, seeded: Fixture
) -> None:
    """The execution is bound to version A; step B lives in version B.

    Claiming version B on the row is caught by the execution binding, and
    claiming version A while naming step B is caught by the step binding -
    so there is no way to record a step from a graph this execution never ran.
    """
    with pytest.raises(Exception) as caught:
        await _insert_step(
            owner_database, seeded, version_id=seeded.version_b, step_id=seeded.step_b
        )
    assert "execution_steps_execution_version_fkey" in str(caught.value)

    with pytest.raises(Exception) as caught:
        await _insert_step(
            owner_database, seeded, version_id=seeded.version_a, step_id=seeded.step_b
        )
    assert "execution_steps_step_fkey" in str(caught.value)


async def test_one_execution_step_row_per_attempt(
    owner_database: Database, seeded: Fixture
) -> None:
    await _insert_step(
        owner_database, seeded, version_id=seeded.version_a, step_id=seeded.step_a, attempt=0
    )
    # A retry is a new attempt, and a new row.
    await _insert_step(
        owner_database, seeded, version_id=seeded.version_a, step_id=seeded.step_a, attempt=1
    )
    with pytest.raises(Exception) as caught:
        await _insert_step(
            owner_database, seeded, version_id=seeded.version_a, step_id=seeded.step_a, attempt=1
        )
    assert "execution_steps_attempt_key" in str(caught.value)


# ----------------------------------------------------------------- the outbox ---


async def _insert_dispatch(
    db: Database,
    f: Fixture,
    *,
    attempt: int = 0,
    status: str = "pending",
    leased: bool = False,
    kind: str = "tool_call",
) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO dispatches "
                "(workspace_id, project_id, execution_id, execution_step_id, kind, status, "
                " side_effect_class, instruction, attempt, leased_by, leased_until) "
                "VALUES (:w, :p, :e, :s, :kind, :status, 'write', '{}'::jsonb, :attempt, "
                "        :leased_by, :leased_until)"
            ),
            {
                "w": f.workspace_id,
                "p": f.project_id,
                "e": f.execution_id,
                "s": f.execution_step_id,
                "kind": kind,
                "status": status,
                "attempt": attempt,
                "leased_by": "worker-1" if leased else None,
                # A datetime, not a string: asyncpg will not coerce a text
                # literal into timestamptz through a bind parameter.
                "leased_until": dt.datetime(2099, 1, 1, tzinfo=dt.UTC) if leased else None,
            },
        )


@pytest.fixture
async def with_step(owner_database: Database, seeded: Fixture) -> Fixture:
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO execution_steps "
                "(id, execution_id, workspace_id, project_id, workflow_version_id, "
                " workflow_step_id, step_key) "
                "VALUES (:i, :e, :w, :p, :v, :s, 'shape')"
            ),
            {
                "i": seeded.execution_step_id,
                "e": seeded.execution_id,
                "w": seeded.workspace_id,
                "p": seeded.project_id,
                "v": seeded.version_a,
                "s": seeded.step_a,
            },
        )
    return seeded


async def test_one_dispatch_per_step_attempt(owner_database: Database, with_step: Fixture) -> None:
    """What makes `advance()` safe to call liberally.

    Two concurrent calls produce a constraint violation on the second, not
    two Slack messages.
    """
    await _insert_dispatch(owner_database, with_step, attempt=0)
    # A retry is a new attempt, and a new row.
    await _insert_dispatch(owner_database, with_step, attempt=1)
    with pytest.raises(Exception) as caught:
        await _insert_dispatch(owner_database, with_step, attempt=1)
    assert "dispatches_step_attempt_key" in str(caught.value)


async def test_a_different_kind_is_a_different_dispatch(
    owner_database: Database, with_step: Fixture
) -> None:
    """The key includes `kind`, so a step may both offer a task and push."""
    await _insert_dispatch(owner_database, with_step, kind="tool_call")
    await _insert_dispatch(owner_database, with_step, kind="agent_push")


async def test_a_leased_dispatch_must_carry_its_lease(
    owner_database: Database, with_step: Fixture
) -> None:
    """A leased row with no lease could never be reclaimed by the reaper.

    It would sit `leased` forever, holding a step that never completes, and
    nothing would notice. The constraint makes the state unrepresentable.
    """
    await _insert_dispatch(owner_database, with_step, status="leased", leased=True)

    with pytest.raises(Exception) as caught:
        await _insert_dispatch(owner_database, with_step, status="leased", leased=False, attempt=1)
    assert "leased_dispatch_has_a_lease" in str(caught.value)


async def test_a_dispatch_gets_an_idempotency_token_by_default(
    owner_database: Database, with_step: Fixture
) -> None:
    """Forwarded outbound, so a cooperating receiver can collapse a retry."""
    await _insert_dispatch(owner_database, with_step)
    async with owner_database.session() as session:
        token = (
            await session.execute(text("SELECT idempotency_token FROM dispatches"))
        ).scalar_one()
    assert token is not None


async def test_execution_tables_are_tenant_isolated(
    database: Database, owner_database: Database, with_step: Fixture
) -> None:
    """RLS on all three, with queries that carry no tenant filter."""
    await _insert_dispatch(owner_database, with_step)

    async def counts(visible: list[uuid.UUID] | None) -> dict[str, int]:
        async with database.session(visible_workspaces=visible) as session:
            out: dict[str, int] = {}
            for table in ("workflow_executions", "execution_steps", "dispatches"):
                # A fixed literal list, not user input.
                result = await session.execute(text(f"SELECT count(*) FROM {table}"))
                out[table] = int(result.scalar_one())
            return out

    unset = await counts(None)
    assert all(value == 0 for value in unset.values()), f"unset tenant saw {unset}"

    visible = await counts([with_step.workspace_id])
    assert visible == {
        "workflow_executions": 1,
        "execution_steps": 1,
        "dispatches": 1,
    }

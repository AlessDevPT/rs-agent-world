"""Task and run integrity: dependencies, bounds, the generated duration.

Also the two circular foreign keys migration 0010 closes, which earlier
slices could only leave as bare uuid columns.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration


class Work:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.other_project_id = uuid.uuid4()
        self.agent_id = uuid.uuid4()
        self.task_a = uuid.uuid4()
        self.task_b = uuid.uuid4()
        self.task_other = uuid.uuid4()


@pytest.fixture
async def work(owner_database: Database) -> Work:
    """Two projects in one workspace, an agent, and three tasks."""
    w = Work()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'work@example.com')"),
            {"i": w.user_id},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :s, :n, :u)"),
            {"i": w.workspace_id, "s": "work", "n": "Work", "u": w.user_id},
        )
        for project_id, slug in ((w.project_id, "main"), (w.other_project_id, "other")):
            await session.execute(
                text("INSERT INTO projects (id, workspace_id, slug, name) VALUES (:i, :w, :s, :n)"),
                {"i": project_id, "w": w.workspace_id, "s": slug, "n": slug},
            )
        await session.execute(
            text(
                "INSERT INTO agents (id, workspace_id, project_id, agent_key, "
                "interaction_mode, display_name) "
                "VALUES (:i, :w, :p, :key, 'dispatchable', :name)"
            ),
            {
                "i": w.agent_id,
                "w": w.workspace_id,
                "p": w.project_id,
                "key": "worker",
                "name": "Worker",
            },
        )
        for task_id, project_id, title in (
            (w.task_a, w.project_id, "First"),
            (w.task_b, w.project_id, "Second"),
            (w.task_other, w.other_project_id, "Elsewhere"),
        ):
            await session.execute(
                text(
                    "INSERT INTO tasks (id, workspace_id, project_id, title) "
                    "VALUES (:i, :w, :p, :title)"
                ),
                {"i": task_id, "w": w.workspace_id, "p": project_id, "title": title},
            )
    return w


# ------------------------------------------------------------- dependencies ---


async def _depend(
    db: Database, w: Work, *, task: uuid.UUID, on: uuid.UUID, project: uuid.UUID
) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO task_dependencies (workspace_id, project_id, task_id, depends_on_id) "
                "VALUES (:w, :p, :task, :on)"
            ),
            {"w": w.workspace_id, "p": project, "task": task, "on": on},
        )


async def test_a_dependency_within_one_project_is_accepted(
    owner_database: Database, work: Work
) -> None:
    await _depend(owner_database, work, task=work.task_b, on=work.task_a, project=work.project_id)


async def test_a_dependency_cannot_cross_projects(owner_database: Database, work: Work) -> None:
    """Both ends go through `(project_id, id)`.

    A task waiting on one in another project would be nonsense, and - between
    two projects a user may not both see - a cross-tenant information channel.
    """
    with pytest.raises(Exception) as caught:
        await _depend(
            owner_database, work, task=work.task_a, on=work.task_other, project=work.project_id
        )
    assert "task_dependencies_depends_on_fkey" in str(caught.value)


async def test_a_task_cannot_depend_on_itself(owner_database: Database, work: Work) -> None:
    with pytest.raises(Exception) as caught:
        await _depend(
            owner_database, work, task=work.task_a, on=work.task_a, project=work.project_id
        )
    assert "no_self_dependency" in str(caught.value)


# ------------------------------------------------------------------- bounds ---


@pytest.mark.parametrize("percent", [-1, 101, 1000])
async def test_progress_outside_zero_to_one_hundred_is_refused(
    owner_database: Database, work: Work, percent: int
) -> None:
    """It arrives from an agent over the wire and is rendered as a bar."""
    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text("UPDATE tasks SET progress_percent = :p WHERE id = :i"),
                {"p": percent, "i": work.task_a},
            )
    assert "progress_is_a_percentage" in str(caught.value)


@pytest.mark.parametrize("percent", [0, 50, 100, None])
async def test_progress_within_range_is_accepted(
    owner_database: Database, work: Work, percent: int | None
) -> None:
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE tasks SET progress_percent = :p WHERE id = :i"),
            {"p": percent, "i": work.task_a},
        )


# ------------------------------------------------- the generated duration ---


async def test_run_duration_is_computed_by_the_database(
    owner_database: Database, work: Work
) -> None:
    """A generated column, so no writer can disagree with it."""
    run_id = uuid.uuid4()
    started = dt.datetime(2026, 11, 1, 9, 0, 0, tzinfo=dt.UTC)
    finished = started + dt.timedelta(milliseconds=2500)

    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO runs (id, workspace_id, project_id, agent_id, "
                "started_at, finished_at) "
                "VALUES (:i, :w, :p, :a, :started, :finished)"
            ),
            {
                "i": run_id,
                "w": work.workspace_id,
                "p": work.project_id,
                "a": work.agent_id,
                "started": started,
                "finished": finished,
            },
        )
        duration = (
            await session.execute(text("SELECT duration_ms FROM runs WHERE id = :i"), {"i": run_id})
        ).scalar_one()
    assert duration == 2500


async def test_run_duration_is_null_while_a_run_is_in_flight(
    owner_database: Database, work: Work
) -> None:
    run_id = uuid.uuid4()
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO runs (id, workspace_id, project_id, agent_id) VALUES (:i, :w, :p, :a)"
            ),
            {"i": run_id, "w": work.workspace_id, "p": work.project_id, "a": work.agent_id},
        )
        duration = (
            await session.execute(text("SELECT duration_ms FROM runs WHERE id = :i"), {"i": run_id})
        ).scalar_one()
    assert duration is None


async def test_run_duration_cannot_be_written(owner_database: Database, work: Work) -> None:
    """Generated means generated: an agent cannot report a flattering number."""
    run_id = uuid.uuid4()
    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO runs (id, workspace_id, project_id, agent_id, duration_ms) "
                    "VALUES (:i, :w, :p, :a, 1)"
                ),
                {"i": run_id, "w": work.workspace_id, "p": work.project_id, "a": work.agent_id},
            )
    assert "duration_ms" in str(caught.value)


# --------------------------------------------- the closed circular references ---


async def test_an_agent_can_now_name_its_current_task(owner_database: Database, work: Work) -> None:
    """The foreign key migration 0007 had to leave as a bare uuid."""
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE agents SET current_task_id = :t WHERE id = :i"),
            {"t": work.task_a, "i": work.agent_id},
        )

    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text("UPDATE agents SET current_task_id = :t WHERE id = :i"),
                {"t": uuid.uuid4(), "i": work.agent_id},
            )
    assert "agents_current_task_fkey" in str(caught.value)


async def test_deleting_a_task_clears_the_agents_pointer(
    owner_database: Database, work: Work
) -> None:
    """SET NULL, so a finished-and-purged task does not block the agent."""
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE agents SET current_task_id = :t WHERE id = :i"),
            {"t": work.task_a, "i": work.agent_id},
        )
    async with owner_database.session() as session:
        await session.execute(text("DELETE FROM tasks WHERE id = :i"), {"i": work.task_a})
        remaining = (
            await session.execute(
                text("SELECT current_task_id FROM agents WHERE id = :i"), {"i": work.agent_id}
            )
        ).scalar_one()
    assert remaining is None


# ---------------------------------------------------------------------- RLS ---


async def test_agent_work_tables_are_tenant_isolated(
    database: Database, owner_database: Database, work: Work
) -> None:
    run_id = uuid.uuid4()
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO runs (id, workspace_id, project_id, agent_id) VALUES (:i, :w, :p, :a)"
            ),
            {"i": run_id, "w": work.workspace_id, "p": work.project_id, "a": work.agent_id},
        )
        await session.execute(
            text(
                "INSERT INTO run_steps (run_id, workspace_id, project_id, seq, name) "
                "VALUES (:r, :w, :p, 0, 'turn')"
            ),
            {"r": run_id, "w": work.workspace_id, "p": work.project_id},
        )
        await session.execute(
            text(
                "INSERT INTO agent_status_history "
                "(agent_id, workspace_id, project_id, to_status) "
                "VALUES (:a, :w, :p, 'idle')"
            ),
            {"a": work.agent_id, "w": work.workspace_id, "p": work.project_id},
        )

    tables = ("tasks", "runs", "run_steps", "agent_status_history")

    async def counts(visible: list[uuid.UUID] | None) -> dict[str, int]:
        async with database.session(visible_workspaces=visible) as session:
            out: dict[str, int] = {}
            for table in tables:
                # A fixed literal list, not user input.
                result = await session.execute(text(f"SELECT count(*) FROM {table}"))
                out[table] = int(result.scalar_one())
            return out

    unset = await counts(None)
    assert all(value == 0 for value in unset.values()), f"unset tenant saw {unset}"

    visible = await counts([work.workspace_id])
    assert visible == {"tasks": 3, "runs": 1, "run_steps": 1, "agent_status_history": 1}

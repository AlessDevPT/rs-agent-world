"""Graph integrity, enforced by the database rather than by the service layer.

Each constraint here prevents a graph that would look fine in the editor and
fail - or worse, half-run - in the orchestrator. They are tested against a
real PostgreSQL because that is where they live; a service-layer check would
be a different guarantee with a different failure mode.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration


class Graph:
    """Ids for one workflow with two versions, and a second workflow with one.

    `version_a` is the open draft and is where every test below writes: the
    graph tables are immutable once a version is published (migration 0018),
    so the draft is the only version a step or an edge can be added to.
    `version_b` is the published version 1 below it, holding `step_b` - the
    other version of the same workflow that a cross-version reference can
    point at. The letters pair each step with its version rather than
    ordering the two.
    """

    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.workflow_id = uuid.uuid4()
        self.other_workflow_id = uuid.uuid4()
        self.version_a = uuid.uuid4()
        self.version_b = uuid.uuid4()
        self.other_version = uuid.uuid4()
        self.step_a = uuid.uuid4()
        self.step_a2 = uuid.uuid4()
        self.step_b = uuid.uuid4()


@pytest.fixture
async def graph(owner_database: Database) -> Graph:
    g = Graph()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'graph@example.com')"),
            {"i": g.user_id},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :s, :n, :u)"),
            {"i": g.workspace_id, "s": "graph", "n": "Graph", "u": g.user_id},
        )
        await session.execute(
            text("INSERT INTO projects (id, workspace_id, slug, name) VALUES (:i, :w, :s, :n)"),
            {"i": g.project_id, "w": g.workspace_id, "s": "graph", "n": "Graph"},
        )
        for workflow_id, slug in (
            (g.workflow_id, "brief"),
            (g.other_workflow_id, "other"),
        ):
            await session.execute(
                text(
                    "INSERT INTO workflows (id, workspace_id, project_id, slug, name) "
                    "VALUES (:i, :w, :p, :slug, :name)"
                ),
                {
                    "i": workflow_id,
                    "w": g.workspace_id,
                    "p": g.project_id,
                    "slug": slug,
                    "name": slug,
                },
            )
        # Each version with its steps, and published before the next one is
        # opened above it. Two constraints force this: migration 0018 refuses
        # a step on a published version, and `workflow_versions_open_draft_key`
        # refuses a second open draft of one workflow - so a fixture cannot
        # hold two half-built versions at once any more than a publish can.
        for version_id, workflow_id, number, publish, steps in (
            (g.version_b, g.workflow_id, 1, True, ((g.step_b, "research"),)),
            (g.version_a, g.workflow_id, 2, False, ((g.step_a, "research"), (g.step_a2, "write"))),
            (g.other_version, g.other_workflow_id, 1, False, ()),
        ):
            await session.execute(
                text(
                    "INSERT INTO workflow_versions "
                    "(id, workflow_id, workspace_id, project_id, version, graph) "
                    "VALUES (:i, :wf, :w, :p, :v, '{}'::jsonb)"
                ),
                {
                    "i": version_id,
                    "wf": workflow_id,
                    "w": g.workspace_id,
                    "p": g.project_id,
                    "v": number,
                },
            )
            for step_id, key in steps:
                await session.execute(
                    text(
                        "INSERT INTO workflow_steps (id, workflow_version_id, workspace_id, "
                        "project_id, step_key, name, kind) "
                        "VALUES (:i, :v, :w, :p, :key, :name, 'transform')"
                    ),
                    {
                        "i": step_id,
                        "v": version_id,
                        "w": g.workspace_id,
                        "p": g.project_id,
                        "key": key,
                        # Separate bind from `key`: one parameter used for
                        # both a citext and a text column leaves asyncpg
                        # unable to deduce a single type for it.
                        "name": key.title(),
                    },
                )
            if publish:
                await session.execute(
                    text("UPDATE workflow_versions SET published_at = now() WHERE id = :i"),
                    {"i": version_id},
                )
    return g


async def _add_edge(
    db: Database,
    g: Graph,
    *,
    version_id: uuid.UUID,
    from_step: uuid.UUID | None,
    to_step: uuid.UUID,
    label: str | None = None,
) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_edges "
                "(workflow_version_id, workspace_id, project_id, from_step_id, to_step_id, label) "
                "VALUES (:v, :w, :p, :from_step, :to_step, :label)"
            ),
            {
                "v": version_id,
                "w": g.workspace_id,
                "p": g.project_id,
                "from_step": from_step,
                "to_step": to_step,
                "label": label,
            },
        )


async def test_a_valid_edge_is_accepted(owner_database: Database, graph: Graph) -> None:
    """So that every refusal below is about the rule and not about the setup."""
    await _add_edge(
        owner_database,
        graph,
        version_id=graph.version_a,
        from_step=graph.step_a,
        to_step=graph.step_a2,
    )
    await _add_edge(
        owner_database, graph, version_id=graph.version_a, from_step=None, to_step=graph.step_a
    )


async def test_an_edge_cannot_wire_steps_from_another_version(
    owner_database: Database, graph: Graph
) -> None:
    """The composite foreign key.

    A plain FK to `workflow_steps(id)` would allow this, and the orchestrator
    would then be handed a step that is not in the graph it is executing.
    """
    with pytest.raises(Exception) as caught:
        await _add_edge(
            owner_database,
            graph,
            version_id=graph.version_a,
            from_step=graph.step_a,
            # Belongs to version B.
            to_step=graph.step_b,
        )
    assert "workflow_edges_to_step_fkey" in str(caught.value)


async def test_an_entry_edge_cannot_be_duplicated(owner_database: Database, graph: Graph) -> None:
    """`UNIQUE NULLS NOT DISTINCT`.

    An entry edge has a NULL `from_step_id` and usually a NULL `label`. Under
    the default SQL semantics NULLs are distinct, so the natural key would
    permit the same entry edge any number of times - and the orchestrator
    would dispatch that step once per duplicate.
    """
    await _add_edge(
        owner_database, graph, version_id=graph.version_a, from_step=None, to_step=graph.step_a
    )
    with pytest.raises(Exception) as caught:
        await _add_edge(
            owner_database,
            graph,
            version_id=graph.version_a,
            from_step=None,
            to_step=graph.step_a,
        )
    assert "workflow_edges_edge_key" in str(caught.value)


async def test_two_labelled_edges_between_the_same_steps_are_allowed(
    owner_database: Database, graph: Graph
) -> None:
    """The label is part of the key, because a condition branches on it."""
    for label in ("approved", "rejected"):
        await _add_edge(
            owner_database,
            graph,
            version_id=graph.version_a,
            from_step=graph.step_a,
            to_step=graph.step_a2,
            label=label,
        )


async def test_a_self_edge_is_refused(owner_database: Database, graph: Graph) -> None:
    """A self-loop is a cycle, and loops are not a v1 feature."""
    with pytest.raises(Exception) as caught:
        await _add_edge(
            owner_database,
            graph,
            version_id=graph.version_a,
            from_step=graph.step_a,
            to_step=graph.step_a,
        )
    assert "no_self_edge" in str(caught.value)


async def test_a_workflow_cannot_point_at_another_workflows_version(
    owner_database: Database, graph: Graph
) -> None:
    """The other composite foreign key.

    Without it, publishing could leave a workflow whose live version belongs
    to a different workflow entirely - and every run of it would execute
    somebody else's graph.
    """
    # Its own version is fine.
    async with owner_database.session() as session:
        await session.execute(
            text("UPDATE workflows SET current_version_id = :v WHERE id = :i"),
            {"v": graph.version_a, "i": graph.workflow_id},
        )

    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text("UPDATE workflows SET current_version_id = :v WHERE id = :i"),
                {"v": graph.other_version, "i": graph.workflow_id},
            )
    assert "workflows_current_version_fkey" in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "constraint"),
    [("agent_task", "agent_task_needs_agent"), ("tool_call", "tool_call_needs_tool")],
)
async def test_a_step_must_declare_what_it_needs(
    owner_database: Database, graph: Graph, kind: str, constraint: str
) -> None:
    """An unrunnable step should not be storable at all.

    That leaves `workflow.validate` to report the problems a constraint
    cannot express - an unreachable step, a cycle, an observed agent assigned
    work - rather than this one.
    """
    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO workflow_steps "
                    "(workflow_version_id, workspace_id, project_id, step_key, name, kind) "
                    "VALUES (:v, :w, :p, :key, :name, :kind)"
                ),
                {
                    "v": graph.version_a,
                    "w": graph.workspace_id,
                    "p": graph.project_id,
                    "key": f"needs-{kind}",
                    "name": f"needs {kind}",
                    "kind": kind,
                },
            )
    assert constraint in str(caught.value)


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("max_attempts", 0, "max_attempts_is_positive"),
        ("timeout_seconds", 0, "timeout_is_positive"),
        ("on_failure", "'explode'", "on_failure_is_known"),
    ],
)
async def test_step_settings_are_bounded(
    owner_database: Database, graph: Graph, column: str, value: object, constraint: str
) -> None:
    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            # The column name is from this test's own table, not user input.
            await session.execute(
                text(
                    "INSERT INTO workflow_steps (workflow_version_id, workspace_id, "
                    f"project_id, step_key, name, kind, {column}) "
                    f"VALUES (:v, :w, :p, :key, :name, 'transform', {value})"
                ),
                {
                    "v": graph.version_a,
                    "w": graph.workspace_id,
                    "p": graph.project_id,
                    "key": f"bad-{column}",
                    "name": f"bad {column}",
                },
            )
    assert constraint in str(caught.value)


async def test_a_version_number_must_be_positive(owner_database: Database, graph: Graph) -> None:
    with pytest.raises(Exception) as caught:
        async with owner_database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO workflow_versions "
                    "(workflow_id, workspace_id, project_id, version, graph) "
                    "VALUES (:wf, :w, :p, 0, '{}'::jsonb)"
                ),
                {"wf": graph.workflow_id, "w": graph.workspace_id, "p": graph.project_id},
            )
    assert "version_is_positive" in str(caught.value)


async def test_workflow_tables_are_tenant_isolated(
    database: Database, owner_database: Database, graph: Graph
) -> None:
    """RLS on all four, exercised with queries that carry no tenant filter."""
    await _add_edge(
        owner_database, graph, version_id=graph.version_a, from_step=None, to_step=graph.step_a
    )

    async def counts(visible: list[uuid.UUID] | None) -> dict[str, int]:
        async with database.session(visible_workspaces=visible) as session:
            out: dict[str, int] = {}
            for table in (
                "workflows",
                "workflow_versions",
                "workflow_steps",
                "workflow_edges",
            ):
                # Table names are a fixed literal list, not user input.
                result = await session.execute(text(f"SELECT count(*) FROM {table}"))
                out[table] = int(result.scalar_one())
            return out

    unset = await counts(None)
    assert all(value == 0 for value in unset.values()), (
        f"an unset tenant must see nothing, saw {unset}"
    )

    visible = await counts([graph.workspace_id])
    assert visible["workflows"] == 2
    assert visible["workflow_versions"] == 3
    assert visible["workflow_steps"] == 3
    assert visible["workflow_edges"] == 1

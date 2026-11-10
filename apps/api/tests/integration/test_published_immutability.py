"""A published version cannot be edited, and the app role cannot talk round it.

T0 / D3. Every write below is made **as `agentworld_app`**, not through a
service, because that is the whole point: the app role holds `UPDATE` and
`DELETE` on these tables, so a check in a service would be advice. The
trigger is not.

Each refusal is paired with the write that must still succeed. A trigger that
refused everything would pass the negative halves and make publishing
impossible, which is the failure mode this codebase has already hit twice.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from agentworld.infra.db import Database

pytestmark = pytest.mark.integration


class Fixture:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.workflow_id = uuid.uuid4()
        self.draft_version = uuid.uuid4()
        self.live_version = uuid.uuid4()
        self.draft_step = uuid.uuid4()
        self.live_step = uuid.uuid4()


@pytest.fixture
async def published(owner_database: Database) -> Fixture:
    """One published version with a step, and one unpublished draft version.

    Note the order: for each version, the step first and `published_at` last,
    and version 1 published before version 2 is inserted at all. Neither is a
    stylistic choice - the trigger refuses adding a step to a version that is
    already published, and `workflow_versions_open_draft_key` refuses a second
    open draft, so this is the order a real publish must use.
    """
    f = Fixture()
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:i, 'frozen@example.com')"),
            {"i": f.user_id},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, slug, name, created_by) VALUES (:i, :s, :n, :u)"),
            {"i": f.workspace_id, "s": "frozen", "n": "Frozen", "u": f.user_id},
        )
        await session.execute(
            text("INSERT INTO projects (id, workspace_id, slug, name) VALUES (:i, :w, :s, :n)"),
            {"i": f.project_id, "w": f.workspace_id, "s": "frozen", "n": "Frozen"},
        )
        await session.execute(
            text(
                "INSERT INTO workflows (id, workspace_id, project_id, slug, name) "
                "VALUES (:i, :w, :p, 'flow', 'Flow')"
            ),
            {"i": f.workflow_id, "w": f.workspace_id, "p": f.project_id},
        )
        for version_id, number, step_id, publish in (
            (f.live_version, 1, f.live_step, True),
            (f.draft_version, 2, f.draft_step, False),
        ):
            await session.execute(
                text(
                    "INSERT INTO workflow_versions "
                    "(id, workflow_id, workspace_id, project_id, version, graph) "
                    "VALUES (:i, :wf, :w, :p, :v, '{}'::jsonb)"
                ),
                {
                    "i": version_id,
                    "wf": f.workflow_id,
                    "w": f.workspace_id,
                    "p": f.project_id,
                    "v": number,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO workflow_steps "
                    "(id, workflow_version_id, workspace_id, project_id, step_key, name, kind) "
                    "VALUES (:i, :v, :w, :p, 'shape', 'Shape', 'transform')"
                ),
                {"i": step_id, "v": version_id, "w": f.workspace_id, "p": f.project_id},
            )
            if publish:
                # Before version 2 exists, not after: migration 0023 permits
                # one open draft per workflow, so the live version has to be
                # frozen before the next one is opened above it. Which is
                # what publishing does.
                await session.execute(
                    text("UPDATE workflow_versions SET published_at = now() WHERE id = :i"),
                    {"i": version_id},
                )
    return f


async def _as_app(database: Database, statement: str, params: dict[str, object]) -> None:
    """Run one statement as `agentworld_app`, with its tenant visible."""
    async with database.session() as session:
        await session.execute(
            text("SELECT set_config('agentworld.workspace_ids', :w, true)"),
            {"w": params["_workspace"]},
        )
        binds = {key: value for key, value in params.items() if key != "_workspace"}
        await session.execute(text(statement), binds)


# ------------------------------------------------------- what must be refused ---


async def test_a_published_version_cannot_be_edited(database: Database, published: Fixture) -> None:
    """The graph an execution is bound to cannot change under it."""
    with pytest.raises(DBAPIError, match="published"):
        await _as_app(
            database,
            "UPDATE workflow_versions SET graph = '{\"steps\": []}'::jsonb WHERE id = :i",
            {"i": published.live_version, "_workspace": str(published.workspace_id)},
        )


async def test_a_published_version_cannot_be_unpublished(
    database: Database, published: Fixture
) -> None:
    """Clearing `published_at` would be a way round every other rule here."""
    with pytest.raises(DBAPIError, match="published"):
        await _as_app(
            database,
            "UPDATE workflow_versions SET published_at = NULL WHERE id = :i",
            {"i": published.live_version, "_workspace": str(published.workspace_id)},
        )


async def test_a_step_cannot_be_added_to_a_published_version(
    database: Database, published: Fixture
) -> None:
    with pytest.raises(DBAPIError, match="published"):
        await _as_app(
            database,
            "INSERT INTO workflow_steps "
            "(workflow_version_id, workspace_id, project_id, step_key, name, kind) "
            "VALUES (:v, :w, :p, 'smuggled', 'Smuggled', 'transform')",
            {
                "v": published.live_version,
                "w": published.workspace_id,
                "p": published.project_id,
                "_workspace": str(published.workspace_id),
            },
        )


async def test_a_published_steps_config_cannot_be_changed(
    database: Database, published: Fixture
) -> None:
    """The subtler one. Leaving the step rows writable would mean the version
    was frozen and its contents were not."""
    with pytest.raises(DBAPIError, match="published"):
        await _as_app(
            database,
            "UPDATE workflow_steps SET name = 'Rewritten' WHERE id = :i",
            {"i": published.live_step, "_workspace": str(published.workspace_id)},
        )


async def test_an_edge_cannot_be_added_to_a_published_version(
    database: Database, published: Fixture
) -> None:
    with pytest.raises(DBAPIError, match="published"):
        await _as_app(
            database,
            "INSERT INTO workflow_edges "
            "(workflow_version_id, workspace_id, project_id, from_step_id, to_step_id) "
            "VALUES (:v, :w, :p, :s, :s)",
            {
                "v": published.live_version,
                "w": published.workspace_id,
                "p": published.project_id,
                "s": published.live_step,
                "_workspace": str(published.workspace_id),
            },
        )


# ------------------------------------------------------ what must still work ---


async def test_an_unpublished_draft_can_still_be_edited(
    database: Database, published: Fixture
) -> None:
    """The non-vacuity pair for the first test. Drafts are the thing people
    spend all their time editing."""
    await _as_app(
        database,
        "UPDATE workflow_versions SET graph = '{\"steps\": []}'::jsonb WHERE id = :i",
        {"i": published.draft_version, "_workspace": str(published.workspace_id)},
    )


async def test_a_step_can_still_be_added_to_a_draft(database: Database, published: Fixture) -> None:
    await _as_app(
        database,
        "INSERT INTO workflow_steps "
        "(workflow_version_id, workspace_id, project_id, step_key, name, kind) "
        "VALUES (:v, :w, :p, 'second', 'Second', 'transform')",
        {
            "v": published.draft_version,
            "w": published.workspace_id,
            "p": published.project_id,
            "_workspace": str(published.workspace_id),
        },
    )


async def test_publishing_is_permitted(database: Database, published: Fixture) -> None:
    """The path the trigger has to leave open, or nothing can ever be
    published at all. `WHEN (OLD.published_at IS NOT NULL)` is what does it:
    at this moment the old value is still null."""
    await _as_app(
        database,
        "UPDATE workflow_versions SET published_at = now() WHERE id = :i",
        {"i": published.draft_version, "_workspace": str(published.workspace_id)},
    )

    async with database.session() as session:
        await session.execute(
            text("SELECT set_config('agentworld.workspace_ids', :w, true)"),
            {"w": str(published.workspace_id)},
        )
        when = (
            await session.execute(
                text("SELECT published_at FROM workflow_versions WHERE id = :i"),
                {"i": published.draft_version},
            )
        ).scalar_one()
    assert when is not None


async def test_the_guard_does_not_depend_on_row_visibility(
    database: Database, published: Fixture
) -> None:
    """`SECURITY DEFINER`, asserted rather than assumed.

    The graph trigger reads `workflow_versions` to find the parent's
    `published_at`. An invoker-rights function would see no row when the
    session's visibility is unset, read NULL, and let the write through - a
    guard that fails open exactly when the caller is least trusted.

    Here the visibility is deliberately *not* set, so the insert's own RLS
    check would refuse it too; what this asserts is that the refusal is the
    trigger's, naming the published version, rather than a policy violation.
    """
    with pytest.raises(DBAPIError, match="published"):
        async with database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO workflow_steps "
                    "(workflow_version_id, workspace_id, project_id, step_key, name, kind) "
                    "VALUES (:v, :w, :p, 'invisible', 'Invisible', 'transform')"
                ),
                {
                    "v": published.live_version,
                    "w": published.workspace_id,
                    "p": published.project_id,
                },
            )


async def test_a_version_that_has_run_cannot_be_deleted(
    owner_database: Database, published: Fixture
) -> None:
    """The other half of immutability, and the reason the trigger covers
    `UPDATE` rather than `DELETE`.

    Deletion of a version that has executed is already refused, by the
    `ON DELETE RESTRICT` on `workflow_executions.workflow_version_id`. A
    trigger raising on delete as well would break the cascade from
    `workspaces`, so this constraint is what carries that half - asserted
    here so the two mechanisms are documented in one place.
    """
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(workspace_id, project_id, workflow_id, workflow_version_id, "
                " initiator_kind, initiator_user_id, initiator_label) "
                "VALUES (:w, :p, :wf, :v, 'user', :u, 'frozen@example.com')"
            ),
            {
                "w": published.workspace_id,
                "p": published.project_id,
                "wf": published.workflow_id,
                "v": published.live_version,
                "u": published.user_id,
            },
        )

    with pytest.raises(DBAPIError):
        async with owner_database.session() as session:
            await session.execute(
                text("DELETE FROM workflow_versions WHERE id = :i"),
                {"i": published.live_version},
            )

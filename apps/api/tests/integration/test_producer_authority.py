"""Who may post which event type, over real HTTP. T0 / D1.

The hole this closes: ingest checked the key's scope, its project pin, the
clock skew and - for events naming an agent - the agent allowlist, and never
the event *type*. So any holder of an ingest key could post
`approval.granted`, which the reducer folds to `execution_steps.status =
ready`, which `advance()` dispatches. "The gate is the absence of a dispatch
row" defends against a buggy approval UI; it does not defend against a forged
event, because the forged event makes the orchestrator create the dispatch.

Each refusal below is paired with the case that must still work. A rule that
refuses everything would pass the negative half on its own, which is how two
earlier guards in this codebase looked correct while doing nothing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentworld.domain.protocol import PROTOCOL_VERSION
from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration

KEY_HEADER = "X-AgentWorld-Key"


class Fixture:
    """A project, an ingest key scoped to one agent, and an execution."""

    def __init__(self, client: TestClient, actor: Actor) -> None:
        self.workspace = client.post(
            "/v1/workspaces", json={"name": "Producers"}, headers=actor.headers
        ).json()
        self.project = client.post(
            f"/v1/workspaces/{self.workspace['id']}/projects",
            json={"name": "Producers"},
            headers=actor.headers,
        ).json()
        minted = client.post(
            f"/v1/workspaces/{self.workspace['id']}/api-keys",
            json={
                "name": "Researcher worker",
                "scope": "ingest",
                "project_id": self.project["id"],
                # Pinned to one agent, so "may act as" is a real question
                # rather than the NULL-allowlist default.
                "agent_key_allowlist": ["researcher"],
            },
            headers=actor.headers,
        )
        assert minted.status_code == 201, minted.text
        self.key = str(minted.json()["key"])

        self.workflow_id = uuid.uuid4()
        self.version_id = uuid.uuid4()
        self.step_id = uuid.uuid4()
        self.execution_id = uuid.uuid4()
        self.step_row_id = uuid.uuid4()

    @property
    def headers(self) -> dict[str, str]:
        return {KEY_HEADER: self.key}

    @property
    def project_id(self) -> uuid.UUID:
        return uuid.UUID(self.project["id"])

    @property
    def workspace_id(self) -> uuid.UUID:
        return uuid.UUID(self.workspace["id"])

    @property
    def step_ref(self) -> str:
        return f"{self.execution_id}:shape"


@pytest.fixture
def producers(client: TestClient, actor: Actor) -> Fixture:
    return Fixture(client, actor)


@pytest.fixture
async def world(owner_database: Database, producers: Fixture) -> dict[str, uuid.UUID]:
    """Two agents and one execution whose single step is `pending`.

    `pending` to start with, so a test has to move it deliberately before the
    step-outcome rule can pass - the state is part of what is being checked.
    """
    agent_ids: dict[str, uuid.UUID] = {}
    async with owner_database.session() as session:
        for agent_key in ("researcher", "writer"):
            agent_id = uuid.uuid4()
            agent_ids[agent_key] = agent_id
            await session.execute(
                text(
                    "INSERT INTO agents (id, workspace_id, project_id, agent_key, "
                    " interaction_mode, display_name) "
                    "VALUES (:i, :w, :p, :key, 'dispatchable', :name)"
                ),
                {
                    "i": agent_id,
                    "w": producers.workspace_id,
                    "p": producers.project_id,
                    "key": agent_key,
                    "name": agent_key.title(),
                },
            )
        await session.execute(
            text(
                "INSERT INTO workflows (id, workspace_id, project_id, slug, name) "
                "VALUES (:i, :w, :p, 'flow', 'Flow')"
            ),
            {"i": producers.workflow_id, "w": producers.workspace_id, "p": producers.project_id},
        )
        await session.execute(
            text(
                "INSERT INTO workflow_versions "
                "(id, workflow_id, workspace_id, project_id, version, graph) "
                "VALUES (:i, :wf, :w, :p, 1, '{}'::jsonb)"
            ),
            {
                "i": producers.version_id,
                "wf": producers.workflow_id,
                "w": producers.workspace_id,
                "p": producers.project_id,
            },
        )
        # `agent_task` carries a check constraint requiring an agent, which
        # is the right shape here: the step is the work handed to one.
        await session.execute(
            text(
                "INSERT INTO workflow_steps "
                "(id, workflow_version_id, workspace_id, project_id, step_key, name, kind, "
                " agent_id) "
                "VALUES (:i, :v, :w, :p, 'shape', 'Shape', 'agent_task', :a)"
            ),
            {
                "i": producers.step_id,
                "v": producers.version_id,
                "w": producers.workspace_id,
                "p": producers.project_id,
                "a": agent_ids["researcher"],
            },
        )
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(id, workspace_id, project_id, workflow_id, workflow_version_id, "
                " initiator_kind, initiator_label) "
                "VALUES (:i, :w, :p, :wf, :v, 'system', 'a trigger')"
            ),
            {
                "i": producers.execution_id,
                "w": producers.workspace_id,
                "p": producers.project_id,
                "wf": producers.workflow_id,
                "v": producers.version_id,
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
                "i": producers.step_row_id,
                "e": producers.execution_id,
                "w": producers.workspace_id,
                "p": producers.project_id,
                "v": producers.version_id,
                "s": producers.step_id,
            },
        )
    return agent_ids


async def dispatch_step_to(
    db: Database, producers: Fixture, agent_id: uuid.UUID, *, status: str = "dispatched"
) -> None:
    async with db.session() as session:
        await session.execute(
            text("UPDATE execution_steps SET status = :st, agent_id = :a WHERE id = :i"),
            {"st": status, "a": agent_id, "i": producers.step_row_id},
        )


async def step_status(db: Database, producers: Fixture) -> str:
    async with db.session() as session:
        return str(
            (
                await session.execute(
                    text("SELECT status FROM execution_steps WHERE id = :i"),
                    {"i": producers.step_row_id},
                )
            ).scalar_one()
        )


def event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "dedup_key": f"p-{uuid.uuid4()}",
        "type": "agent.heartbeat",
        "protocol_version": PROTOCOL_VERSION,
        "occurred_at": (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat(),
        "subject": {"kind": "agent", "ref": "researcher"},
    }
    base.update(overrides)
    return base


def post(client: TestClient, producers: Fixture, *events: dict[str, Any]) -> Any:
    return client.post(
        "/v1/ingest/events", json={"events": list(events)}, headers=producers.headers
    )


# ------------------------------------------------- the control plane is shut ---


async def test_an_ingest_key_cannot_grant_an_approval(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """The headline case. A worker credential must not open its own gate."""
    await dispatch_step_to(owner_database, producers, world["researcher"])

    response = post(
        client,
        producers,
        event(
            type="approval.granted",
            subject={"kind": "approval", "ref": str(uuid.uuid4())},
            correlation={
                "execution_step_ref": producers.step_ref,
                "execution_ref": str(producers.execution_id),
            },
        ),
    )

    assert response.status_code == 403, response.text
    # The status check is the one that matters. A 403 that still folded would
    # be the real failure, and the status code alone would not show it.
    assert await step_status(owner_database, producers) == "dispatched"


async def test_an_ingest_key_cannot_report_an_execution_outcome(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID]
) -> None:
    response = post(
        client,
        producers,
        event(
            type="workflow.execution_completed",
            subject={"kind": "workflow_execution", "ref": str(producers.execution_id)},
        ),
    )
    assert response.status_code == 403, response.text


async def test_an_ingest_key_cannot_dispatch_a_step(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID]
) -> None:
    """Creating work is the orchestrator's job, and `step_dispatched` is its
    record of having done it."""
    response = post(
        client,
        producers,
        event(
            type="workflow.step_dispatched",
            subject={"kind": "execution_step", "ref": producers.step_ref},
        ),
    )
    assert response.status_code == 403, response.text


async def test_the_refusal_says_which_event_in_the_batch(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID]
) -> None:
    """An integrator posting a batch needs to know which event was refused.

    `index` only. `LOCATOR_KEYS` deliberately keeps the rest of the error's
    context out of the response - the type is something the caller sent, so
    echoing it back adds nothing, and the detail line already names it.
    """
    response = post(
        client,
        producers,
        event(),
        event(
            type="approval.denied",
            subject={"kind": "approval", "ref": str(uuid.uuid4())},
            correlation={"execution_step_ref": producers.step_ref},
        ),
    )
    assert response.status_code == 403
    problem = response.json()
    assert problem["index"] == 1
    assert problem["type"].endswith("permission_denied"), problem
    # And nothing landed: the batch is one transaction.
    assert "event_type" not in problem


# ------------------------------------------ a step outcome, properly scoped ---


async def test_an_agent_may_report_its_own_dispatched_step(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """The positive case, and the proof the rule is not simply refusing
    everything in the namespace."""
    await dispatch_step_to(owner_database, producers, world["researcher"])

    response = post(
        client,
        producers,
        event(
            type="workflow.step_succeeded",
            subject={"kind": "execution_step", "ref": producers.step_ref},
            payload={"output": {"findings": 3}},
        ),
    )

    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 1
    assert await step_status(owner_database, producers) == "succeeded"


async def test_an_agent_cannot_report_another_agents_step(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """The step exists and is dispatched - just not to this key's agent."""
    await dispatch_step_to(owner_database, producers, world["writer"])

    response = post(
        client,
        producers,
        event(
            type="workflow.step_succeeded",
            subject={"kind": "execution_step", "ref": producers.step_ref},
            payload={"output": {"findings": 3}},
        ),
    )

    assert response.status_code == 403, response.text
    assert await step_status(owner_database, producers) == "dispatched"


async def test_an_agent_cannot_report_a_step_that_was_never_dispatched(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """A step nobody handed out has no outcome to report. Without the state
    check, an agent on the allowlist could settle every step in the project
    the moment it was created."""
    response = post(
        client,
        producers,
        event(
            type="workflow.step_succeeded",
            subject={"kind": "execution_step", "ref": producers.step_ref},
        ),
    )

    assert response.status_code == 403, response.text
    assert await step_status(owner_database, producers) == "pending"


async def test_an_agent_cannot_settle_a_step_twice(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """Once the step is settled it is no longer dispatched, so the second
    report is refused by the same rule - before the monotonicity guard is
    even reached."""
    await dispatch_step_to(owner_database, producers, world["researcher"])
    first = post(
        client,
        producers,
        event(
            type="workflow.step_succeeded",
            subject={"kind": "execution_step", "ref": producers.step_ref},
        ),
    )
    assert first.status_code == 202, first.text

    second = post(
        client,
        producers,
        event(
            type="workflow.step_failed",
            subject={"kind": "execution_step", "ref": producers.step_ref},
            payload={"error": {"code": "late_failure"}},
        ),
    )

    assert second.status_code == 403, second.text
    assert await step_status(owner_database, producers) == "succeeded"


async def test_a_running_step_may_still_be_reported(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID], owner_database: Database
) -> None:
    """`running` counts as outstanding, not just `dispatched`.

    A push-style agent's step moves to `running` when it reports starting;
    refusing the outcome after that would strand the step until its timeout.
    """
    await dispatch_step_to(owner_database, producers, world["researcher"], status="running")

    response = post(
        client,
        producers,
        event(
            type="workflow.step_succeeded",
            subject={"kind": "execution_step", "ref": producers.step_ref},
        ),
    )

    assert response.status_code == 202, response.text
    assert await step_status(owner_database, producers) == "succeeded"


# ------------------------------------------------- the open namespace stays ---


async def test_an_unknown_type_is_still_accepted(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID]
) -> None:
    """The first compatibility promise. An unrecognised type is stored and
    folds into nothing, so it is safe whatever it is called."""
    response = post(
        client,
        producers,
        event(type="custom.deployment_noticed"),
        event(type="future.namespace_from_a_later_release"),
    )
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 2


async def test_an_agents_own_events_are_unaffected(
    client: TestClient, producers: Fixture, world: dict[str, uuid.UUID]
) -> None:
    """The whole `agent.*`, `task.*`, `run.*`, `tool.*` surface still works -
    this rule narrowed two namespaces, not the protocol."""
    response = post(
        client,
        producers,
        event(type="agent.registered"),
        event(type="agent.heartbeat"),
        event(type="run.started", subject={"kind": "run", "ref": str(uuid.uuid4())}),
    )
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 3

"""D4.1 over HTTP: what an agent read says about the heartbeat.

Nothing here is seeded. The heartbeats arrive the way an agent's do - over
`/v1/ingest/heartbeat` for a live one, and as an `agent.heartbeat` event with
an older `occurred_at` for one that went quiet, which ingest accepts because
only *future* skew is bounded. So these tests exercise the same path a real
worker uses, and the liveness they read back is derived at the moment of the
request from the column that path wrote.

The test that earns its place is the last one. A stale heartbeat is a claim
about beats, not about health, and an agent that reports runs while never
heartbeating is `stale` and working at the same time. If a later stage makes
`liveness` consider run activity, that test is the one that will fail - which
is exactly when somebody should be made to look at this decision again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_ingest import Fixture, event, post

from agentworld.domain.enums import AgentLiveness
from helpers import Actor

pytestmark = pytest.mark.integration


@pytest.fixture
def ingest(client: TestClient, actor: Actor) -> Fixture:
    return Fixture(client, actor)


# ------------------------------------------------------------------ helpers ---


def make_agent(
    client: TestClient,
    actor: Actor,
    ingest: Fixture,
    *,
    agent_key: str,
    mode: str = "dispatchable",
) -> dict[str, Any]:
    """One agent, created the way a person creates one.

    An agent row is configuration: nothing in the protocol can create one,
    so every test here starts from the control plane and only then sends
    events about it.
    """
    body: dict[str, Any] = {
        "display_name": agent_key.title(),
        "agent_key": agent_key,
        "interaction_mode": mode,
    }
    if mode == "dispatchable":
        body["dispatch"] = {"transport": "webhook", "url": "https://example.test/hook"}
    created = client.post(
        f"/v1/projects/{ingest.project_id}/agents", json=body, headers=actor.headers
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def read_agent(client: TestClient, actor: Actor, ingest: Fixture, agent_id: Any) -> dict[str, Any]:
    response = client.get(
        f"/v1/projects/{ingest.project_id}/agents/{agent_id}", headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def list_agents(
    client: TestClient, actor: Actor, ingest: Fixture, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    response = client.get(
        f"/v1/projects/{ingest.project_id}/agents",
        params={"include_archived": include_archived},
        headers=actor.headers,
    )
    assert response.status_code == 200, response.text
    return [dict(row) for row in response.json()]


def heartbeat_at(agent_key: str, when: dt.datetime, *, dedup_key: str) -> dict[str, Any]:
    """An `agent.heartbeat` dated in the past, which ingest accepts.

    The skew check bounds how far *ahead* of the server a producer's clock
    may be; a timestamp in the past is a worker whose backlog took a while
    to flush, which is a thing that happens.
    """
    return event(
        dedup_key=dedup_key,
        type="agent.heartbeat",
        subject={"kind": "agent", "ref": agent_key},
        occurred_at=when.isoformat(),
    )


def minutes_ago(minutes: float) -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes)


def seconds_ago(seconds: float) -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)


# --------------------------------------------------------- fresh, stale, never ---


async def test_an_agent_that_just_heartbeated_reads_fresh(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """Through `/v1/ingest/heartbeat`, which is how a worker does it."""
    agent = make_agent(client, actor, ingest, agent_key="researcher")
    beat = client.post(
        "/v1/ingest/heartbeat", json={"agent_key": "researcher"}, headers=ingest.headers
    )
    assert beat.status_code == 202, beat.text

    body = read_agent(client, actor, ingest, agent["id"])
    assert body["liveness"] == AgentLiveness.FRESH
    assert body["seconds_since_heartbeat"] is not None
    assert 0 <= body["seconds_since_heartbeat"] < 60, "just heartbeated, so barely any age"
    assert body["last_heartbeat_at"] is not None


async def test_an_agent_silent_longer_than_its_ttl_reads_stale(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """One hour of silence against the default sixty seconds."""
    agent = make_agent(client, actor, ingest, agent_key="researcher")
    assert (
        post(
            client, ingest, heartbeat_at("researcher", minutes_ago(60), dedup_key="hb-old")
        ).status_code
        == 202
    )

    body = read_agent(client, actor, ingest, agent["id"])
    assert body["liveness"] == AgentLiveness.STALE
    assert body["heartbeat_ttl_seconds"] == 60
    assert body["seconds_since_heartbeat"] >= 3_600


async def test_an_agent_that_has_never_heartbeated_reads_never_reported(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """Created and nothing else: not stale, because nothing went quiet.

    `status` says `provisioning` - "registered, never seen" - and this
    field must not contradict it by calling the same agent stale.
    """
    agent = make_agent(client, actor, ingest, agent_key="researcher")
    assert agent["liveness"] == AgentLiveness.NEVER_REPORTED, "already true in the create response"
    assert agent["seconds_since_heartbeat"] is None

    body = read_agent(client, actor, ingest, agent["id"])
    assert body["liveness"] == AgentLiveness.NEVER_REPORTED
    assert body["status"] == "provisioning"
    assert body["last_heartbeat_at"] is None


async def test_a_managed_agent_reads_not_applicable_however_long_it_has_been_quiet(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """A heartbeat from a hundred days ago, and still not stale.

    AgentWorld runs a managed agent's loop itself, so there is nothing
    external to hear from. Without the exemption this agent would read
    `stale` on every listing for ever.
    """
    agent = make_agent(client, actor, ingest, agent_key="worker", mode="managed")
    ancient = dt.datetime.now(dt.UTC) - dt.timedelta(days=100)
    assert (
        post(client, ingest, heartbeat_at("worker", ancient, dedup_key="hb-ancient")).status_code
        == 202
    )

    body = read_agent(client, actor, ingest, agent["id"])
    assert body["liveness"] == AgentLiveness.NOT_APPLICABLE
    assert body["seconds_since_heartbeat"] >= 100 * 86_400, (
        "the age is still reported; only the verdict is withheld"
    )


async def test_the_threshold_is_the_agents_own_column(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """Two agents, one silence, two answers.

    Both last heartbeated thirty seconds ago - deliberately between the two
    thresholds. One has been configured to promise a beat every five
    seconds and the other keeps the default sixty, so the only thing that
    can produce different verdicts is the column, which is what makes this
    a test of the per-agent TTL rather than of a constant.
    """
    impatient = make_agent(client, actor, ingest, agent_key="impatient")
    patient = make_agent(client, actor, ingest, agent_key="patient")
    tightened = client.patch(
        f"/v1/projects/{ingest.project_id}/agents/{impatient['id']}",
        json={"heartbeat_ttl_seconds": 5},
        headers=actor.headers,
    )
    assert tightened.status_code == 200, tightened.text

    quiet = seconds_ago(30)
    assert (
        post(
            client,
            ingest,
            heartbeat_at("impatient", quiet, dedup_key="hb-i"),
            heartbeat_at("patient", quiet, dedup_key="hb-p"),
        ).status_code
        == 202
    )

    assert read_agent(client, actor, ingest, impatient["id"])["liveness"] == AgentLiveness.STALE
    assert read_agent(client, actor, ingest, patient["id"])["liveness"] == AgentLiveness.FRESH


# ------------------------------------------------------------ the awkward reads ---


async def test_an_archived_agent_still_reports_its_liveness(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """Archiving rewrites nothing derived, so the answer does not change.

    `AgentService.restore` already promised this in words - a reader judges
    staleness from `last_heartbeat_at` for an archived agent exactly as for
    one that stopped reporting without being archived - and this is the
    first read that can be held to it.
    """
    agent = make_agent(client, actor, ingest, agent_key="researcher")
    assert (
        post(
            client, ingest, heartbeat_at("researcher", minutes_ago(60), dedup_key="hb-old")
        ).status_code
        == 202
    )
    before = read_agent(client, actor, ingest, agent["id"])

    archived = client.delete(
        f"/v1/projects/{ingest.project_id}/agents/{agent['id']}", headers=actor.headers
    )
    assert archived.status_code == 204, archived.text

    after = read_agent(client, actor, ingest, agent["id"])
    assert after["archived_at"] is not None
    assert after["liveness"] == before["liveness"] == AgentLiveness.STALE


async def test_the_listing_and_the_single_read_agree(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """Three agents, three different answers, two endpoints.

    The listing judges a whole page against one clock while a single read
    judges one agent against its own, so the two could drift at a boundary.
    These are nowhere near one: a hundred-day silence, a heartbeat seconds
    old, and no heartbeat at all.
    """
    stale = make_agent(client, actor, ingest, agent_key="stale-one")
    fresh = make_agent(client, actor, ingest, agent_key="fresh-one")
    never = make_agent(client, actor, ingest, agent_key="never-one")
    assert (
        post(
            client,
            ingest,
            heartbeat_at(
                "stale-one", dt.datetime.now(dt.UTC) - dt.timedelta(days=100), dedup_key="l-s"
            ),
        ).status_code
        == 202
    )
    assert (
        client.post(
            "/v1/ingest/heartbeat", json={"agent_key": "fresh-one"}, headers=ingest.headers
        ).status_code
        == 202
    )

    listed = {row["id"]: row for row in list_agents(client, actor, ingest)}
    assert set(listed) == {stale["id"], fresh["id"], never["id"]}
    assert listed[stale["id"]]["liveness"] == AgentLiveness.STALE
    assert listed[fresh["id"]]["liveness"] == AgentLiveness.FRESH
    assert listed[never["id"]]["liveness"] == AgentLiveness.NEVER_REPORTED

    for agent_id, row in listed.items():
        assert read_agent(client, actor, ingest, agent_id)["liveness"] == row["liveness"]


async def test_a_stale_agent_with_a_live_run_is_still_stale(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """**The distinction this field exists to keep.**

    This agent last heartbeated an hour ago and reported a run a minute
    ago. It is demonstrably working - the trace is there to read - and its
    heartbeat is demonstrably stale, because beats and work are different
    event types folded into different columns: `agent.heartbeat` moves
    `agents.last_heartbeat_at`, and no `run.*` event touches the agent row
    at all.

    So `liveness` says `stale` and means it, about the heartbeat. Anyone
    who wants to know whether the agent is making progress reads its runs,
    which is why the assertion below reads them. If a later stage widens
    `liveness` to consider run activity, this test fails - and that is the
    moment to decide the question again rather than discover it in
    production.
    """
    agent = make_agent(client, actor, ingest, agent_key="researcher")
    assert (
        post(
            client, ingest, heartbeat_at("researcher", minutes_ago(60), dedup_key="hb-old")
        ).status_code
        == 202
    )
    working = minutes_ago(1)
    assert (
        post(
            client,
            ingest,
            event(
                dedup_key="live-1",
                type="run.started",
                subject={"kind": "run", "ref": "run-1"},
                correlation={"agent_key": "researcher"},
                occurred_at=working.isoformat(),
            ),
            event(
                dedup_key="live-2",
                type="run.step_started",
                subject={"kind": "run", "ref": "run-1"},
                correlation={"agent_key": "researcher"},
                occurred_at=working.isoformat(),
                payload={"seq": 0, "name": "turn"},
            ),
            event(
                dedup_key="live-3",
                type="run.step_finished",
                subject={"kind": "run", "ref": "run-1"},
                correlation={"agent_key": "researcher"},
                occurred_at=working.isoformat(),
                payload={"seq": 0},
            ),
        ).status_code
        == 202
    )

    body = read_agent(client, actor, ingest, agent["id"])
    assert body["liveness"] == AgentLiveness.STALE, "the heartbeat is an hour old and says so"

    runs = client.get(
        f"/v1/projects/{ingest.project_id}/agents/{agent['id']}/runs", headers=actor.headers
    )
    assert runs.status_code == 200, runs.text
    reported = runs.json()
    assert [row["run_ref"] for row in reported] == ["run-1"]
    assert reported[0]["status"] == "running"
    assert reported[0]["step_count"] == 1, "it was working a minute ago, and the trace proves it"


async def test_liveness_is_not_something_a_client_can_send_back(
    client: TestClient, actor: Actor, ingest: Fixture
) -> None:
    """Derived, like every other read-only field on this view.

    `AgentUpdate` has no `liveness` and no `seconds_since_heartbeat`, so an
    unknown key is dropped rather than stored - the same treatment `status`
    gets, and for the same reason: a person asserting "this agent is fresh"
    would be asserting something only its heartbeat can establish.
    """
    agent = make_agent(client, actor, ingest, agent_key="researcher")
    patched = client.patch(
        f"/v1/projects/{ingest.project_id}/agents/{agent['id']}",
        json={"liveness": "fresh", "seconds_since_heartbeat": 0},
        headers=actor.headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["liveness"] == AgentLiveness.NEVER_REPORTED, (
        "the key was dropped, not stored: the agent still has no heartbeat"
    )

    body = read_agent(client, actor, ingest, uuid.UUID(str(agent["id"])))
    assert body["liveness"] == AgentLiveness.NEVER_REPORTED
    assert body["seconds_since_heartbeat"] is None

"""D4.1: heartbeat freshness, and the three answers that are not `stale`.

`Agent.liveness` is pure and takes its clock as an argument, so every case
below is a question about arithmetic rather than about timing. That is the
whole reason the computation lives in the domain: the awkward cases - the
boundary second, the heartbeat from the future, the agent nobody has ever
heard from - are cheap to ask about here and expensive to arrange over HTTP.

**What these tests pin is a claim about the heartbeat and nothing else.** An
agent's beats and its work are different event types folded into different
columns, so `stale` here is not evidence that an agent is dead, stuck, or
unable to make progress. `test_agent_liveness_api.py` in the integration
suite has the case that makes that concrete: a stale agent with a live run.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from agentworld.domain.entities import Agent
from agentworld.domain.enums import AgentInteractionMode, AgentLiveness, AgentStatus

NOW = dt.datetime(2026, 11, 13, 12, 0, 0, tzinfo=dt.UTC)

#: Every outcome, pinned. A member added to the enum fails this until
#: somebody decides what produces it - the same arrangement
#: `test_dispatch_status.py` uses, and for the same reason: a value nothing
#: produces is a value no reader can be written against.
OUTCOMES = {
    AgentLiveness.NOT_APPLICABLE,
    AgentLiveness.NEVER_REPORTED,
    AgentLiveness.FRESH,
    AgentLiveness.STALE,
}

#: The modes that report for themselves. `managed` is the exception and has
#: its own tests; these two are judged identically, which is the point of
#: listing them rather than testing `dispatchable` and assuming.
REPORTING_MODES = (AgentInteractionMode.DISPATCHABLE, AgentInteractionMode.OBSERVED)


def agent(
    *,
    mode: AgentInteractionMode = AgentInteractionMode.DISPATCHABLE,
    heartbeat: dt.datetime | None = None,
    ttl: int = 60,
    status: AgentStatus = AgentStatus.IDLE,
    archived: dt.datetime | None = None,
) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_key="researcher",
        interaction_mode=mode,
        display_name="Researcher",
        heartbeat_ttl_seconds=ttl,
        status=status,
        last_heartbeat_at=heartbeat,
        archived_at=archived,
    )


def seconds_ago(seconds: float) -> dt.datetime:
    return NOW - dt.timedelta(seconds=seconds)


# ------------------------------------------------------------ fresh or stale ---


@pytest.mark.parametrize("mode", REPORTING_MODES)
def test_an_agent_heard_from_inside_its_ttl_is_fresh(mode: AgentInteractionMode) -> None:
    assert agent(mode=mode, heartbeat=seconds_ago(1), ttl=60).liveness(NOW) is AgentLiveness.FRESH


@pytest.mark.parametrize("mode", REPORTING_MODES)
def test_an_agent_silent_longer_than_its_ttl_is_stale(mode: AgentInteractionMode) -> None:
    assert agent(mode=mode, heartbeat=seconds_ago(600), ttl=60).liveness(NOW) is AgentLiveness.STALE


def test_the_boundary_is_the_ttl_itself_and_not_the_second_before_it() -> None:
    """`>` rather than `>=`, asserted on the second either side.

    An agent heard from exactly its TTL ago has kept the promise it made,
    so the cut-off is more than `heartbeat_ttl_seconds` of silence. The
    third case pins the flooring: the comparison is in whole seconds, the
    same units the API reports, so 60.9 seconds of silence against a
    60-second TTL is still 60 seconds and still fresh.
    """
    assert agent(heartbeat=seconds_ago(59), ttl=60).liveness(NOW) is AgentLiveness.FRESH
    assert agent(heartbeat=seconds_ago(60), ttl=60).liveness(NOW) is AgentLiveness.FRESH
    assert agent(heartbeat=seconds_ago(60.9), ttl=60).liveness(NOW) is AgentLiveness.FRESH
    assert agent(heartbeat=seconds_ago(61), ttl=60).liveness(NOW) is AgentLiveness.STALE


def test_the_threshold_is_this_agents_own_ttl_and_not_a_default() -> None:
    """Six seconds of silence, two agents, two answers.

    The only difference between them is the column, which is what makes
    this a test of the per-agent TTL rather than of the number 60.
    """
    quiet = seconds_ago(6)
    assert agent(heartbeat=quiet, ttl=5).liveness(NOW) is AgentLiveness.STALE
    assert agent(heartbeat=quiet, ttl=60).liveness(NOW) is AgentLiveness.FRESH
    assert agent(heartbeat=quiet, ttl=3600).liveness(NOW) is AgentLiveness.FRESH


# -------------------------------------------------------- the other two answers ---


@pytest.mark.parametrize(
    "status",
    [AgentStatus.PROVISIONING, AgentStatus.IDLE, AgentStatus.WORKING, AgentStatus.ERROR],
)
def test_an_agent_that_never_reported_is_not_stale(status: AgentStatus) -> None:
    """`never_reported`, whatever the status column says.

    An agent nobody has ever heard from is not an agent that went quiet,
    and `AgentStatus.PROVISIONING` already means "registered, never seen".
    Parametrized over the status because the two are independent: a status
    an agent reported for itself cannot make a heartbeat exist.
    """
    judged = agent(heartbeat=None, status=status)
    assert judged.liveness(NOW) is AgentLiveness.NEVER_REPORTED
    assert judged.seconds_since_heartbeat(NOW) is None


def test_a_managed_agent_is_exempt_however_long_it_has_been_quiet() -> None:
    """A hundred days of silence, and still `not_applicable`.

    AgentWorld runs a managed agent's loop itself, so there is no external
    worker to hear from and no silence to measure. The ancient heartbeat is
    the case that matters: without the exemption this agent would read
    `stale` for ever, on every listing, which is noise rather than
    information. Its work is in `workflow_executions`.
    """
    managed = agent(mode=AgentInteractionMode.MANAGED, heartbeat=seconds_ago(100 * 86_400), ttl=60)
    assert managed.liveness(NOW) is AgentLiveness.NOT_APPLICABLE
    assert managed.seconds_since_heartbeat(NOW) == 100 * 86_400, (
        "the elapsed time is still reported; only the verdict is withheld"
    )


def test_a_managed_agent_that_never_reported_is_exempt_rather_than_unseen() -> None:
    """The exemption is checked first, deliberately.

    `never_reported` would be true of a managed agent and useless: it will
    never report, so the answer that says something is the one about its
    mode.
    """
    managed = agent(mode=AgentInteractionMode.MANAGED, heartbeat=None)
    assert managed.liveness(NOW) is AgentLiveness.NOT_APPLICABLE


# --------------------------------------------------------------- elapsed time ---


def test_elapsed_seconds_is_floored_rather_than_rounded() -> None:
    assert agent(heartbeat=seconds_ago(0.4)).seconds_since_heartbeat(NOW) == 0
    assert agent(heartbeat=seconds_ago(1.9)).seconds_since_heartbeat(NOW) == 1
    assert agent(heartbeat=seconds_ago(90)).seconds_since_heartbeat(NOW) == 90


def test_a_heartbeat_ahead_of_the_server_reads_as_zero_rather_than_negative() -> None:
    """Producer clocks are checked, not corrected.

    `/v1/ingest/events` bounds how far ahead an `occurred_at` may be and
    records the skew; it never rewrites it. So a heartbeat timestamped
    slightly in this server's future is a legitimate stored value, and
    "last seen -4 seconds ago" would read as a bug in whatever printed it.
    """
    ahead = agent(heartbeat=NOW + dt.timedelta(seconds=4))
    assert ahead.seconds_since_heartbeat(NOW) == 0
    assert ahead.liveness(NOW) is AgentLiveness.FRESH


def test_the_number_and_the_verdict_cannot_disagree() -> None:
    """Both read the same floored delta, so the boundary is one place.

    Walked across the cut-off a second at a time: the verdict changes
    exactly where the reported number passes the TTL, which is the property
    that keeps a rendered page self-consistent.
    """
    for elapsed in range(0, 10):
        judged = agent(heartbeat=seconds_ago(elapsed), ttl=5)
        reported = judged.seconds_since_heartbeat(NOW)
        assert reported == elapsed
        expected = AgentLiveness.STALE if reported > 5 else AgentLiveness.FRESH
        assert judged.liveness(NOW) is expected


# -------------------------------------------------------------------- archived ---


def test_archiving_changes_nothing_about_liveness() -> None:
    """Two agents alike but for `archived_at`, judged identically.

    `AgentService.restore` promises exactly this: nothing derived is
    rewritten by archiving or restoring, so "a reader judges staleness from
    `last_heartbeat_at` exactly as they must for an agent that stopped
    reporting without being archived". This is the first code to make that
    sentence true, so it is the first place it can be asserted.
    """
    quiet = seconds_ago(600)
    assert agent(heartbeat=quiet).liveness(NOW) is AgentLiveness.STALE
    assert agent(heartbeat=quiet, archived=seconds_ago(300)).liveness(NOW) is AgentLiveness.STALE


# ----------------------------------------------------------------- the outcomes ---


def test_every_outcome_is_reachable() -> None:
    """A member added to the enum fails this until something produces it."""
    produced = {
        agent(mode=AgentInteractionMode.MANAGED, heartbeat=None).liveness(NOW),
        agent(heartbeat=None).liveness(NOW),
        agent(heartbeat=seconds_ago(1), ttl=60).liveness(NOW),
        agent(heartbeat=seconds_ago(600), ttl=60).liveness(NOW),
    }
    assert produced == OUTCOMES == set(AgentLiveness)

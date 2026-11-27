"""What the raw `event` channel may carry, and what it may not.

The `events` channel is in the **default** subscription set and is readable at
`viewer`, so whatever reaches it is readable by the weakest role in the
workspace without asking for it. Two rules hold there, and only the first was
tested before this file:

1. **Definition events never reach it** (P10.3), because configuration
   history would otherwise reach every viewer through the feed. Asserted in
   `test_realtime_resume.py`.
2. **A step event's payload is withheld**, because it is the unnarrowed source
   of `ExecutionStepView.output` and `.error` - and those are narrowed on
   purpose. `api/v1/executions.py`'s `_safe_output` withholds `transform`
   output whole, projects an `agent_task` turn by field name, and says in its
   own docstring that serving what it drops "would make the narrowing below
   decorative". Serving the payload on this channel would do exactly that.

The second rule is what this file is for. The first test is total over the
taxonomy, so a type added later is covered without being listed; the second
drives the concrete case the narrowing names - a **dispatchable agent**
reporting its own step with fields of its choosing - through the real ingest
plane.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from realtime_helpers import connect, fence, of_type, publish
from test_producer_authority import (  # noqa: F401 - `producers`/`world` are fixtures
    Fixture,
    dispatch_step_to,
    event,
    post,
    producers,
    world,
)

from agentworld.api.v1.executions import ExecutionStepView
from agentworld.api.ws.frames import NARROWED_BY_READ_MODEL, RawEventView, _raw_event
from agentworld.domain.protocol.taxonomy import KNOWN_EVENTS
from agentworld.infra.db import Database
from helpers import Actor

pytestmark = pytest.mark.integration


class Row:
    """The columns `_raw_event` reads, with a payload worth withholding."""

    def __init__(self, event_type: str) -> None:
        self.id = uuid.uuid4()
        self.seq = 7
        self.type = event_type
        self.occurred_at = dt.datetime.now(dt.UTC)
        self.subject_kind = "execution_step"
        self.subject_ref = f"{uuid.uuid4()}:research"
        self.correlation = {}
        self.severity = "info"
        self.labels = {}
        self.payload = {"output": {"text": "fine", "provider_raw": "sk-secret"}}


# ------------------------------------------------- total over the taxonomy ---


@pytest.mark.parametrize("event_type", sorted(KNOWN_EVENTS))
def test_a_payload_is_withheld_exactly_where_a_read_model_narrows_it(
    event_type: str,
) -> None:
    """One rule, checked against every registered type rather than a list.

    `folds_into` is the honest answer to "what does this event change?", and
    `NARROWED_BY_READ_MODEL` names the tables whose read model serves less
    than the row holds. A type added to the taxonomy later is covered here
    the moment it declares what it folds into, which is the property a list
    of type names would not have.
    """
    spec = KNOWN_EVENTS[event_type]
    view = _raw_event(Row(event_type), folds=spec.folds_into)
    narrows = bool(NARROWED_BY_READ_MODEL & set(spec.folds_into))

    assert view.payload_withheld is narrows, event_type
    if narrows:
        assert view.payload == {}, event_type
        assert "sk-secret" not in str(view.model_dump(mode="json")), event_type
    else:
        # Narrow by design: everything else keeps the payload the producer
        # sent, because no read model narrows it and the feed is the only
        # place it is served at all.
        assert view.payload == {"output": {"text": "fine", "provider_raw": "sk-secret"}}


def test_the_narrowed_set_is_what_the_api_layer_actually_narrows() -> None:
    """The set is one member, and that is a claim about `api/v1`, not a guess.

    `_safe_output` and `_safe_error` are the only narrowing in the API layer
    and `execution_steps` is what they narrow; `ExecutionView.output` is
    served as stored, which `_safe_output`'s docstring states in as many
    words. If a second narrowing is added and this set is not, the payload
    behind it starts reaching the feed - so the set is pinned here beside the
    reason for its size.
    """
    assert frozenset({"execution_steps"}) == NARROWED_BY_READ_MODEL
    assert "workflow_executions" not in NARROWED_BY_READ_MODEL


def test_a_withheld_payload_says_so_rather_than_looking_empty() -> None:
    """`{}` alone would mean both "sent nothing" and "withheld"."""
    withheld = _raw_event(Row("workflow.step_succeeded"), folds=("execution_steps",))
    kept = _raw_event(Row("log.emitted"), folds=())

    assert withheld.payload_withheld is True
    assert kept.payload_withheld is False
    assert "payload_withheld" in RawEventView.model_fields


# ------------------------------------- the concrete case, through ingest ---


async def test_a_dispatchable_agents_step_output_does_not_reach_the_feed(
    client: TestClient,
    actor: Actor,
    producers: Fixture,  # noqa: F811
    world: dict[str, uuid.UUID],  # noqa: F811
    owner_database: Database,
) -> None:
    """The case `_safe_output` names, end to end.

    `workflow.step_succeeded` is `AGENT_FOR_DISPATCHED_STEP`: the agent
    writes the payload, so it may put anything in it - which is why the
    payload is withheld whole rather than filtered by key. Here it sends a
    field the turn whitelist drops (`findings`) and one no whitelist would
    ever have guessed at (`operator_note`), and neither may appear on a
    channel a viewer holds by default.
    """
    await dispatch_step_to(owner_database, producers, world["researcher"])
    secret = "token-ABC123-should-never-be-served"

    with connect(client, str(producers.project_id), actor.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        accepted = post(
            client,
            producers,
            event(
                type="workflow.step_succeeded",
                subject={"kind": "execution_step", "ref": producers.step_ref},
                payload={
                    "output": {"findings": 3, "provider_raw": secret},
                    "operator_note": secret,
                },
            ),
        )
        assert accepted.status_code == 202, accepted.text
        publish(client)
        frames = fence(ws)

    raw = of_type(frames, "event")
    assert len(raw) == 1, "a step outcome still appears on the feed"
    assert raw[0]["data"]["payload"] == {}
    assert raw[0]["data"]["payload_withheld"] is True
    # The whole frame, not just the payload key: nothing carried it sideways
    # through `labels`, `correlation` or the subject.
    assert secret not in str(raw[0])
    # What the feed keeps is what the feed is for.
    assert raw[0]["data"]["type"] == "workflow.step_succeeded"
    assert raw[0]["data"]["subject"]["ref"] == producers.step_ref

    # And the authorized view still arrives, on the same cursor, narrowed by
    # the same model the REST route returns - so nothing was lost, only moved
    # to the path that is allowed to serve it.
    steps = of_type(frames, "step.updated")
    assert len(steps) == 1
    assert steps[0]["cursor"] == raw[0]["cursor"]
    view = ExecutionStepView.model_validate(steps[0]["data"])
    assert view.step_key == "shape"
    assert secret not in str(steps[0])
    assert "findings" not in str(steps[0]["data"]["output"] or {})


async def test_an_ordinary_agent_event_keeps_its_payload_on_the_feed(
    client: TestClient,
    actor: Actor,
    producers: Fixture,  # noqa: F811
    world: dict[str, uuid.UUID],  # noqa: F811
) -> None:
    """The rule is narrow, and this is the half that proves it.

    A heartbeat folds into `agents`, which no read model narrows, so its
    payload is served as sent. Without this, withholding every payload would
    pass the test above and leave the activity feed empty.
    """
    with connect(client, str(producers.project_id), actor.headers) as ws:
        ws.receive_json()
        publish(client)
        fence(ws)

        accepted = post(
            client,
            producers,
            event(payload={"capabilities": ["search"]}),
        )
        assert accepted.status_code == 202, accepted.text
        publish(client)
        frames = fence(ws)

    raw = of_type(frames, "event")
    assert len(raw) == 1
    assert raw[0]["data"]["payload"] == {"capabilities": ["search"]}
    assert raw[0]["data"]["payload_withheld"] is False

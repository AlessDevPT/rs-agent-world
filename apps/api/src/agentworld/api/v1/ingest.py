"""The ingest plane's four write endpoints.

Everything here authenticates with `X-AgentWorld-Key` and nothing here reads.
That asymmetry is the plane's whole point: a key that leaks from a worker can
add to the record and cannot enumerate the workspace it sits in.

The request models are deliberately thin. `events` is a list of free-form
objects rather than a typed union of 44 event shapes, because the protocol's
first promise is that an unknown type is accepted and stored - a pydantic
model per type would turn every new producer field into a server release, and
a discriminated union would reject exactly the producer the promise exists to
protect. Validation lives in `domain/protocol/envelope.py`, where it can be
tested without HTTP.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from agentworld.api.deps import CurrentKey, IngestServiceDep
from agentworld.domain.enums import EventSeverity
from agentworld.repositories.events import LogLine, MetricPoint

router = APIRouter(prefix="/v1/ingest", tags=["ingest"])

AgentKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="The agent this key is speaking for.",
        examples=["researcher"],
    ),
]


# ------------------------------------------------------------- responses ---


class EventOutcomeModel(BaseModel):
    """What happened to one event, in the order it was sent."""

    dedup_key: str
    accepted: bool = Field(
        description="False when this project already held an event with this key. "
        "A duplicate is not an error: it means a retry worked."
    )
    seq: int | None = Field(
        default=None,
        description="The project cursor this event was given. Absent for a duplicate.",
    )
    rows_changed: int = Field(
        default=0,
        description="How many rows folding this event changed. Zero is normal - an "
        "event about something that no longer exists is still recorded.",
    )


class IngestAccepted(BaseModel):
    accepted: int
    duplicates: int
    highest_seq: int | None = Field(
        default=None, description="Resume from here to read everything in this batch."
    )
    outcomes: list[EventOutcomeModel]


class TelemetryAccepted(BaseModel):
    accepted: int


# -------------------------------------------------------------- requests ---


class EventBatch(BaseModel):
    events: list[dict[str, Any]] = Field(
        description="Raw event envelopes. See the protocol documentation for the shape.",
        examples=[
            [
                {
                    "dedup_key": "run-3f9a:step-1",
                    "type": "run.step_finished",
                    "protocol_version": "1.0",
                    "occurred_at": "2026-11-04T07:00:04.113Z",
                    "subject": {"kind": "run", "ref": "run-3f9a"},
                    "payload": {"seq": 1, "kind": "llm"},
                }
            ]
        ],
    )


class HeartbeatRequest(BaseModel):
    agent_key: AgentKey


class LogLineModel(BaseModel):
    message: str = Field(min_length=1, max_length=16_384)
    occurred_at: dt.datetime
    severity: EventSeverity = EventSeverity.INFO
    context: dict[str, Any] = Field(default_factory=dict)


class LogBatch(BaseModel):
    agent_key: AgentKey | None = None
    lines: list[LogLineModel]


class MetricModel(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    value: float
    occurred_at: dt.datetime
    unit: str | None = Field(default=None, max_length=32)
    labels: dict[str, str] = Field(default_factory=dict)


class MetricBatch(BaseModel):
    agent_key: AgentKey | None = None
    samples: list[MetricModel]


# --------------------------------------------------------------- routes ---


@router.post(
    "/events",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Append a batch of events",
)
async def ingest_events(
    body: EventBatch, principal: CurrentKey, service: IngestServiceDep
) -> IngestAccepted:
    """Validate, dedupe, append and fold a batch.

    202 rather than 201: the events are durable when this returns, but what
    they *mean* - a workflow advancing, a dispatch being written - is decided
    by the orchestrator afterwards and is not what this status is promising.

    The batch is one transaction. A single malformed event fails the request
    and nothing is written, so a retry of the whole batch is simply correct
    rather than something the dedup key has to rescue.
    """
    result = await service.ingest(principal, body.events)
    return IngestAccepted(
        accepted=result.accepted,
        duplicates=result.duplicates,
        highest_seq=result.highest_seq,
        outcomes=[
            EventOutcomeModel(
                dedup_key=outcome.dedup_key,
                accepted=outcome.accepted,
                seq=outcome.seq,
                rows_changed=outcome.rows_changed,
            )
            for outcome in result.outcomes
        ],
    )


@router.post(
    "/heartbeat",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Report that an agent is alive",
)
async def heartbeat(
    body: HeartbeatRequest, principal: CurrentKey, service: IngestServiceDep
) -> IngestAccepted:
    """A convenience for the commonest event there is.

    It goes through the same pipeline as everything else, so there is exactly
    one way `last_heartbeat_at` can move. Two heartbeats for one agent within
    the same second share a dedup key and the second is reported as a
    duplicate, which is the right answer to a retry.
    """
    result = await service.heartbeat(principal, body.agent_key)
    return IngestAccepted(
        accepted=result.accepted,
        duplicates=result.duplicates,
        highest_seq=result.highest_seq,
        outcomes=[
            EventOutcomeModel(
                dedup_key=outcome.dedup_key, accepted=outcome.accepted, seq=outcome.seq
            )
            for outcome in result.outcomes
        ],
    )


@router.post(
    "/logs",
    response_model=TelemetryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Append log lines",
)
async def ingest_logs(
    body: LogBatch, principal: CurrentKey, service: IngestServiceDep
) -> TelemetryAccepted:
    """Log lines are output, not events: storing one changes no state.

    They have their own table and their own endpoint because they arrive at a
    volume that would churn the event cursor, and a producer filling `events`
    with debug lines would push its own real events out of a reader's window.
    """
    written = await service.ingest_logs(
        principal,
        [
            LogLine(
                message=line.message,
                occurred_at=line.occurred_at,
                severity=line.severity,
                context=line.context,
            )
            for line in body.lines
        ],
        agent_key=body.agent_key,
    )
    return TelemetryAccepted(accepted=written)


@router.post(
    "/metrics",
    response_model=TelemetryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Append metric samples",
)
async def ingest_metrics(
    body: MetricBatch, principal: CurrentKey, service: IngestServiceDep
) -> TelemetryAccepted:
    written = await service.ingest_metrics(
        principal,
        [
            MetricPoint(
                name=sample.name,
                value=sample.value,
                occurred_at=sample.occurred_at,
                unit=sample.unit,
                labels=sample.labels,
            )
            for sample in body.samples
        ],
        agent_key=body.agent_key,
    )
    return TelemetryAccepted(accepted=written)

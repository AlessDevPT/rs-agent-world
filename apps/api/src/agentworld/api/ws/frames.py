"""Turning committed event rows into the frames a client sees.

The frame set is `03-API-AND-FRONTEND.md` §4 and `03-API.md` §6; nothing here
invents one. What this module is for is the **single** place that maps a row of
`events` to frames, and it earns its keep by being single: the live path and
the replay path call the same function on the same rows, so R10's "exactly what
was missed" is structural rather than two implementations that agree today.

**A frame is not the record behind it** (P10). The durable record of a
definition change carries identity and the operation only - `instructions`,
`labels`, `model_overrides` and `dispatch` are free-form and may hold an
endpoint or a token, so they are kept out of an append-only, retained log. The
whole entity in `data` is read from the definition table here, at build time,
through the same view model the REST route returns. That split is the whole of
why a payload of two fields can produce a delta a client can apply.

**The mapping, which is P11.4's table and not a choice made here.**

| Persisted row | Frame | Channel |
|---|---|---|
| `definition.agent.{created,updated,archived,restored}` | `agent.updated` | `agents` |
| `definition.workflow.{created,updated,archived,restored}` | `workflow.updated` | `workflows` |
| `definition.connection.{created,updated}` | `connection.updated` | `connections` |
| `definition.connection.deleted` | tombstone `connection.deleted` | `connections` |
| anything folding into `workflow_executions` | `execution.updated` | `executions` |
| anything folding into `execution_steps` | `step.updated` | `executions` |
| anything folding into `agents` / `agent_status_history` | `agent.updated` | `agents` |
| any non-definition row | `event` | `events` |

Archive and restore are `agent.updated` and `workflow.updated` because an
archived entity stays addressable - the detail route asks for archived rows on
purpose - so an archive is an update to something that remains in the read
model, not a removal from it (P11.4). Connections are the only hard delete
among the three, so the tombstone occurs exactly once in this table.

**A step event's payload is withheld from the `events` channel.** Not the
frame - the payload. For an event that folds into `execution_steps`, the
payload is the unnarrowed source of `ExecutionStepView.output` and `.error`,
which the steps route narrows on purpose; serving it on a `viewer`-readable
feed would hand back exactly what that narrowing withholds. The authorized
entity arrives on `step.updated` in the same batch and on the same cursor.
`_raw_event` has the full argument, including why it is withheld whole rather
than filtered by key.

**Definition rows never reach the `events` channel** (P10.3). That channel is
the raw activity feed and the island's animation source, and it is readable at
`viewer`; configuration history must not arrive through it. A definition change
reaches a client on its own channel, carrying what the REST route would return,
or not at all.

**Which entity a non-definition row touched is read from `folds_into`.** Its
own docstring calls it "the honest answer to *what does this event change?*",
which is exactly the question a delta asks, and it is paired here with the
reference the row actually resolved: no `agent_id`, no `agent.updated`. The
alternative - re-deriving the envelope and folding it again - would run the
reducers a second time to recover a fact the row already carries.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, Field

from agentworld.api.v1.agents import AgentView
from agentworld.api.v1.executions import ExecutionStepView, ExecutionView
from agentworld.api.v1.workflows import ConnectionView, WorkflowView
from agentworld.domain.protocol import spec_for
from agentworld.domain.protocol.refs import split_step_ref
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork

#: The channels a client may subscribe to. One per key of the snapshot's
#: `data`, plus `events` for the raw feed, so nothing a client was shown in
#: the snapshot can change without a channel to say so.
#: `03-API-AND-FRONTEND.md` §4 names six of these in passing; `connections`
#: and `integrations` are here because the snapshot has those keys.
#:
#: **Two things the rule does not give, stated because an earlier revision of
#: this comment implied it did.** It is *not* true that a client cannot be
#: sent entities its snapshot never described: `step.updated` rides
#: `executions` and no snapshot key describes steps, so a subscriber to that
#: channel must expect them. And three of these are advertised but unserved -
#: no frame carries `integrations`, `approvals` or `world`, because the first
#: two have no table yet and the third waits on the `world/nodes` read API.
#: Subscribing to one of those is a silent no-op; they are in the set so the
#: phases that fill them need no protocol change.
CHANNELS: Final[tuple[str, ...]] = (
    "agents",
    "workflows",
    "connections",
    "integrations",
    "executions",
    "approvals",
    "events",
    "world",
)

#: The namespace of a persisted definition change (P10.1). Non-folding by
#: construction: `fold()` dispatches on the part before the first dot and
#: `definition` is not a reducer, so a replay cannot re-create a definition.
DEFINITION_PREFIX: Final = "definition."


@dataclass(frozen=True, slots=True)
class Frame:
    """One frame, and the channel that decides who gets it."""

    channel: str
    type: str
    cursor: int
    data: Mapping[str, Any]

    def as_json(self) -> dict[str, Any]:
        """The wire form: `type`, `cursor`, `data`. The channel is routing."""
        return {"type": self.type, "cursor": self.cursor, "data": dict(self.data)}


class RawEventView(BaseModel):
    """The `event` frame's payload: the event as it was recorded.

    Its own model because there is no REST route for the raw feed yet, so
    there is no view to share - which is also why R7's "validates against the
    same view model the REST route returns" is about the *deltas*. What this
    does carry is the guarantee that matters for the feed: `payload` and
    `labels` as the producer sent them, and no definition row ever reaching
    it (P10.3).
    """

    id: UUID
    seq: int
    type: str
    occurred_at: dt.datetime
    subject: dict[str, Any] = Field(default_factory=dict)
    correlation: dict[str, Any] = Field(default_factory=dict)
    severity: str
    labels: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    #: True when `payload` was withheld rather than empty as sent. Said rather
    #: than left to be inferred, because `{}` would otherwise mean both "the
    #: producer sent nothing" and "a read model narrows this, read the entity
    #: delta instead" - and a feed that cannot tell those apart invites a
    #: client to conclude the step produced no output. See `_raw_event`.
    payload_withheld: bool = False


class FrameBuilder:
    """Builds frames inside one transaction, for one project.

    Holds a `UnitOfWork` rather than opening its own, so a batch of rows is
    turned into frames against one consistent read of the definition tables
    and one connection - the gateway and the publisher both have a reason to
    care, and neither may hold a connection for longer than the work needs.
    """

    def __init__(self, work: UnitOfWork, scope: TenantScope) -> None:
        self._work = work
        self._scope = scope

    # ---------------------------------------------------------- snapshot ---

    async def snapshot(self) -> Frame:
        """The first frame: every documented key, and the cursor it is true at.

        The cursor and the entities have to describe the same instant or R6
        cannot hold, which is why the caller opens this transaction at
        REPEATABLE READ: the sequence is allocated under a lock held to
        commit, so a visible seq implies every lower one is visible too, and
        "the highest seq this transaction can see" is therefore exactly the
        boundary between what is in the snapshot and what will arrive as a
        delta.

        `integrations` and `approvals` are empty lists because neither table
        exists yet - they arrive in Phases 3a and 6 - and `world` is empty
        because the `world/nodes` read API is step 7 of this phase and owns
        that view model. The keys are present rather than omitted, because a
        client that has to discover which keys exist is a client that breaks
        when one appears. None of the three is synthesised: an empty list is
        what "nothing to show" looks like, and R14 forbids inventing the
        alternative.

        **Archived agents and workflows are included**, which the REST
        listings hide. That is not an inconsistency with R7, which is about a
        payload's *shape*: an archive is an `.updated` frame carrying the
        whole entity (P11.4), so a client that had never been sent the entity
        would be told to update something it does not hold. The snapshot's set
        and the delta's set have to agree, and `archived_at` is in the view -
        what to show is the client's decision, with the same field the detail
        page uses to make it.

        Test harnesses are absent for the opposite reason and without any
        extra filtering here: `WorkflowRepository.list_all` already excludes
        them, which is exactly the argument P13 §5 gives for a harness
        emitting no definition event - it is not in the listing snapshot and
        delta keep in sync.

        Executions carry the repository's own limit, the same one the REST
        listing uses. A project with more than that has a `GET` for the rest;
        a snapshot is what a socket needs to start, not an archive.
        """
        now = dt.datetime.now(dt.UTC)
        scope = self._scope
        cursor = await self._work.event_reads(scope).latest_seq()

        agents = await self._work.agents(scope).list_all(include_archived=True)
        workflows = await self._work.workflows(scope).list_all(include_archived=True)
        connections = await self._work.connections(scope).list_all()
        executions = await self._work.executions(scope).list_in_project()

        return Frame(
            channel="snapshot",
            type="snapshot",
            cursor=cursor,
            data={
                "agents": [AgentView.of(a, now=now).model_dump(mode="json") for a in agents],
                "workflows": [WorkflowView.of(w).model_dump(mode="json") for w in workflows],
                "connections": [ConnectionView.of(c).model_dump(mode="json") for c in connections],
                "integrations": [],
                "executions": [ExecutionView.of(e).model_dump(mode="json") for e in executions],
                "approvals": [],
                "world": {"layout": {}, "placements": []},
            },
        )

    # ------------------------------------------------------------- deltas ---

    async def frames_for(self, row: Any) -> list[Frame]:
        """The frames one committed event row produces, in order.

        May be empty, and an empty list is information rather than a failure:
        a row whose entity has since been hard-deleted has nothing whole to
        describe, and a tombstone for it was already sent by the delete's own
        row. The caller still accounts for the row's `seq`, which is what
        keeps a client's gap detection honest.
        """
        if row.type.startswith(DEFINITION_PREFIX):
            return await self._definition_frames(row)
        return await self._execution_frames(row)

    async def _definition_frames(self, row: Any) -> list[Frame]:
        _, _, remainder = row.type.partition(DEFINITION_PREFIX)
        entity, _, operation = remainder.partition(".")
        entity_id = _uuid(dict(row.payload or {}).get("id"))
        if entity_id is None:  # pragma: no cover - the producer always writes one
            return []

        if entity == "agent":
            agent = await self._work.agents(self._scope).get(entity_id, include_archived=True)
            if agent is None:
                return []
            now = dt.datetime.now(dt.UTC)
            return [
                Frame(
                    channel="agents",
                    type="agent.updated",
                    cursor=row.seq,
                    data=AgentView.of(agent, now=now).model_dump(mode="json"),
                )
            ]

        if entity == "workflow":
            workflow = await self._work.workflows(self._scope).get(entity_id, include_archived=True)
            if workflow is None:
                return []
            return [
                Frame(
                    channel="workflows",
                    type="workflow.updated",
                    cursor=row.seq,
                    data=WorkflowView.of(workflow).model_dump(mode="json"),
                )
            ]

        if entity == "connection":
            if operation == "deleted":
                # The tombstone of P10.2: project, entity type, entity id and
                # the cursor, and nothing else. An entity document with its
                # fields blanked would be indistinguishable from an update
                # that cleared them, which is why this is not one.
                return [
                    Frame(
                        channel="connections",
                        type="connection.deleted",
                        cursor=row.seq,
                        data={
                            "project_id": str(self._scope.require_project()),
                            "entity": "connection",
                            "id": str(entity_id),
                        },
                    )
                ]
            connection = await self._work.connections(self._scope).get(entity_id)
            if connection is None:
                return []
            return [
                Frame(
                    channel="connections",
                    type="connection.updated",
                    cursor=row.seq,
                    data=ConnectionView.of(connection).model_dump(mode="json"),
                )
            ]

        # A `definition.*` type this release does not map. Registered types
        # are the eleven of P11, so this is reachable only from a future
        # release's row read by an older process - which must stay silent
        # rather than guess, by the same compatibility promise that keeps an
        # unknown event type storable.
        return []  # pragma: no cover - no twelfth type exists

    async def _execution_frames(self, row: Any) -> list[Frame]:
        spec = spec_for(row.type)
        folds = spec.folds_into if spec is not None else ()
        frames: list[Frame] = [
            Frame(
                channel="events",
                type="event",
                cursor=row.seq,
                data=_raw_event(row, folds=folds).model_dump(mode="json"),
            )
        ]

        if "workflow_executions" in folds and row.execution_id is not None:
            header = await self._work.executions(self._scope).header(row.execution_id)
            if header is not None:
                frames.append(
                    Frame(
                        channel="executions",
                        type="execution.updated",
                        cursor=row.seq,
                        data=ExecutionView.of(header).model_dump(mode="json"),
                    )
                )

        if "execution_steps" in folds:
            step_frame = await self._step_frame(row)
            if step_frame is not None:
                frames.append(step_frame)

        if ("agents" in folds or "agent_status_history" in folds) and row.agent_id is not None:
            agent = await self._work.agents(self._scope).get(row.agent_id, include_archived=True)
            if agent is not None:
                now = dt.datetime.now(dt.UTC)
                frames.append(
                    Frame(
                        channel="agents",
                        type="agent.updated",
                        cursor=row.seq,
                        data=AgentView.of(agent, now=now).model_dump(mode="json"),
                    )
                )
        return frames

    async def _step_frame(self, row: Any) -> Frame | None:
        """`step.updated` for the step this row is about, if it names one.

        The step is resolved from the subject ref rather than from a column:
        `events.execution_step_id` is never populated, because
        `EventRepository.resolve` has no lookup for it - the producer side is
        finished and this does not change it. The ref convention is
        `<execution_ref>:<step_key>` (`domain/protocol/refs.py`), which is the
        same reading the reducers use, so a row the reducers folded onto a
        step resolves here to that step.

        The highest attempt wins. A retry adds a row rather than replacing
        one, so "the step" in a delta is the attempt a reader is watching.
        """
        correlation = dict(row.correlation or {})
        ref = correlation.get("execution_step_ref") or (
            row.subject_ref if row.subject_kind == "execution_step" else None
        )
        if not isinstance(ref, str):
            return None
        reference = split_step_ref(ref, execution_ref=correlation.get("execution_ref"))
        if reference is None:
            return None
        execution_id = _uuid(reference.execution_ref) or row.execution_id
        if execution_id is None:
            return None

        steps = await self._work.executions(self._scope).steps(execution_id)
        matching = [step for step in steps if step.step_key == reference.step_key]
        if not matching:
            return None
        step = max(matching, key=lambda candidate: candidate.attempt)
        data = ExecutionStepView.of(step).model_dump(mode="json")
        # The execution id is addressing, not state: a step key is unique
        # within an execution and nowhere else, so a client could not place
        # the step without it. The payload still validates against the view
        # the steps route returns, which is what R7 asks of a delta.
        data["execution_id"] = str(execution_id)
        return Frame(channel="executions", type="step.updated", cursor=row.seq, data=data)


def bus_message(seq: int, frames: Sequence[Frame]) -> dict[str, Any]:
    """What travels on the bus for one event row.

    The row, not the frame, is the unit - including a row that produced no
    frames. Two things need that. A row can yield several frames on one
    cursor (an ingested step outcome is `event` **and** `step.updated`), so a
    consumer that deduplicated per frame would drop the second; and because
    the sequence is gapless per project, a consumer that knows it has seen
    seq N can tell a message for N+1 from a message for N+4 - a hole, which
    it closes from `events` rather than by hoping.
    """
    return {
        "seq": seq,
        "frames": [{"channel": frame.channel, "frame": frame.as_json()} for frame in frames],
    }


#: Tables whose read model **narrows** what it serves, so the event payload
#: that produced a row is not the same thing as the row a client may read.
#:
#: One member, and it is not a guess: `_safe_output` and `_safe_error` in
#: `api/v1/executions.py` are the only narrowing in the API layer, and
#: `execution_steps` is what they narrow. `workflow_executions` is
#: deliberately **not** here - `ExecutionView.output` is served as stored, and
#: `_safe_output`'s own docstring says so ("the execution's own `output` is
#: unaffected and still carries the result").
NARROWED_BY_READ_MODEL: Final[frozenset[str]] = frozenset({"execution_steps"})


def _raw_event(row: Any, *, folds: tuple[str, ...] = ()) -> RawEventView:
    """The event as recorded - minus a payload a read model would narrow.

    **Why a payload is ever withheld here.** The raw feed is readable at
    `viewer`, and for an event that folds into `execution_steps` its payload
    *is* the unnarrowed source of `ExecutionStepView.output` and `.error`.
    Those two are narrowed on purpose: the steps route promises that "no
    provider payload, credential or internal detail travels", `transform`
    output is withheld whole because it can be a copy of a model turn's entire
    stored output, and the fields of an `agent_task` turn are projected by
    name. Serving the payload here would hand a viewer exactly the bytes that
    narrowing exists to withhold, which would make it - in its own words -
    decorative.

    **Withheld whole rather than filtered by key**, because `workflow.step_
    succeeded` and `step_failed` are `AGENT_FOR_DISPATCHED_STEP`: a
    dispatchable agent writes that payload, so a blocklist of `output` and
    `error` would be a guess about what a producer names the rest. Whitelists
    are what `_safe_output` does, and withholding is the whitelist that needs
    no knowledge of the step's kind - and so cannot fail open when the step
    row is gone.

    **Nothing is lost that a client cannot get.** The authorized whole entity
    arrives on `step.updated` in the same batch, on the same cursor, narrowed
    by the view the REST route returns. What stays here is what the feed is
    for: the type, the subject, when it happened and how severe it was. That
    is the same identity-and-operation split P10.3 applies to definition
    events, for the same reason.

    **What this does not claim.** `labels` and `correlation` are producer-
    supplied free-form on *every* type, and so is the payload of
    `log.emitted`, `tool.*` and `agent.*` - none of which any read model
    narrows, because no REST route serves events at all. Redaction of
    agent-supplied free-form content is T7b's (P2), not this function's, and
    withholding those here would be a different and much larger decision.
    """
    withheld = bool(NARROWED_BY_READ_MODEL & set(folds))
    return RawEventView(
        id=row.id,
        seq=row.seq,
        type=row.type,
        occurred_at=row.occurred_at,
        subject={"kind": row.subject_kind, "ref": row.subject_ref},
        correlation=dict(row.correlation or {}),
        severity=str(row.severity),
        labels=dict(row.labels or {}),
        payload={} if withheld else dict(row.payload or {}),
        payload_withheld=withheld,
    )


def _uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None

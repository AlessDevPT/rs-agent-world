"""Ingest: validate, dedupe, append, fold, apply.

The one entry point for everything a producer sends. The order of the steps is
load-bearing and each one is here for a reason that is cheaper to read than to
rediscover:

1. **Authorize the key** before anything is parsed. A key pinned to the wrong
   plane or to no project cannot usefully be told which of its events were
   malformed.
2. **Check the agent allowlist** per event, because one key may speak for
   several agents and the allowlist is what stops a leaked worker credential
   impersonating a different one.
3. **Refuse a clock too far in the future.** A producer whose clock is an hour
   ahead would otherwise write events that sort before everything real for the
   next hour, and a cursor that has already passed them never returns them.
   Skew in the other direction is recorded and accepted: being late is normal.
4. **Dedupe, then allocate `seq`.** This way round, so a duplicate costs no
   sequence number - a gap is cheap but a gap caused by every retry is a
   cursor that outruns its data. The project's cursor row is locked *before*
   the first dedup check rather than on the first accepted event: dedup is a
   read followed by a write, so two concurrent batches carrying one
   `dedup_key` would otherwise both find nothing and the loser would hit the
   partition's unique index, failing the whole batch instead of reporting a
   duplicate. Migration 0015 already called this serialised; it now is.
5. **Append, fold, apply - in one transaction.** The event row, the state it
   implies and nothing else, so there is no moment where the log claims
   something the state does not show.

**The whole batch is one transaction.** One malformed event fails the request
and nothing is written. The alternative - partial success - sounds friendlier
and is worse: a producer retrying the batch would re-send the events that
already landed, and only the dedup key would save it. With all-or-nothing, a
retry is simply correct.

What is *not* here is as important. Ingest writes no `dispatches` row, calls
no provider and sends no request. It cannot: the only thing it applies is a
`Mutation`, and that vocabulary has no way to express work. That is the
seventh protocol promise, arriving at the layer with the most reason to break
it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agentworld.domain.enums import ApiKeyScope
from agentworld.domain.errors import PermissionDeniedError, ValidationFailedError
from agentworld.domain.principals import KeyPrincipal
from agentworld.domain.protocol import (
    PROTOCOL_VERSION,
    EventEnvelope,
    Producer,
    parse_envelope,
    spec_for,
)
from agentworld.domain.reducers import fold
from agentworld.repositories.base import TenantScope
from agentworld.repositories.events import EventRepository, LogLine, MetricPoint
from agentworld.repositories.unit_of_work import UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class EventOutcome:
    """What happened to one event, in the order it was sent."""

    dedup_key: str
    #: False when this project already held an event with the same key.
    accepted: bool
    #: Present only when accepted. The cursor a reader can resume from.
    seq: int | None = None
    #: How many rows the fold changed. Zero is normal and not an error - see
    #: `repositories.derived`.
    rows_changed: int = 0


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: int
    duplicates: int
    outcomes: Sequence[EventOutcome]

    @property
    def highest_seq(self) -> int | None:
        """The cursor a client can resume from after this batch."""
        allocated = [o.seq for o in self.outcomes if o.seq is not None]
        return max(allocated) if allocated else None


class IngestService:
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        *,
        max_batch_events: int = 500,
        max_future_skew_seconds: int = 300,
    ) -> None:
        self._factory = factory
        self._max_batch = max_batch_events
        self._max_future_skew = max_future_skew_seconds

    async def ingest(
        self, principal: KeyPrincipal, raw_events: Sequence[Mapping[str, Any]]
    ) -> IngestResult:
        """Validate and store a batch, folding each event into state."""
        project_id = self._authorize(principal)

        if not raw_events:
            raise ValidationFailedError("A batch needs at least one event", field="events")
        if len(raw_events) > self._max_batch:
            raise ValidationFailedError(
                f"A batch holds at most {self._max_batch} events",
                field="events",
                sent=len(raw_events),
            )

        received_at = datetime.now(UTC)
        envelopes = [
            self._validated(principal, raw, index, received_at)
            for index, raw in enumerate(raw_events)
        ]
        self._reject_repeats_within_batch(envelopes)

        scope = TenantScope(workspace_id=principal.workspace_id, project_id=project_id)
        outcomes: list[EventOutcome] = []

        async with self._factory.begin(visible_workspaces={principal.workspace_id}) as work:
            events = work.events(scope)
            derived = work.derived(scope)

            # Before the first `seen()`, not lazily on the first accepted
            # event. Dedup is a read followed by a write, so it is only sound
            # if nothing else is writing this project's events meanwhile -
            # and the partition's unique index would otherwise catch the race
            # by aborting the whole batch rather than reporting a duplicate.
            await events.lock_project_cursor()

            for index, envelope in enumerate(envelopes):
                await self._check_step_ownership(principal, events, envelope, index)

                if await events.seen(envelope.dedup_key):
                    outcomes.append(EventOutcome(envelope.dedup_key, accepted=False))
                    continue

                seq = await events.next_seq()
                refs = await events.resolve(envelope)
                event_id = await events.append(envelope, seq=seq, refs=refs)

                changed = 0
                for mutation in fold(envelope):
                    # `refs` travels with the mutations folded from this
                    # event: creating a run needs the agent that owns it, and
                    # resolving it twice could resolve it differently.
                    changed += await derived.apply(mutation, event_id=event_id, refs=refs)

                outcomes.append(
                    EventOutcome(
                        envelope.dedup_key,
                        accepted=True,
                        seq=seq,
                        rows_changed=changed,
                    )
                )

        accepted = sum(1 for outcome in outcomes if outcome.accepted)
        return IngestResult(
            accepted=accepted,
            duplicates=len(outcomes) - accepted,
            outcomes=outcomes,
        )

    # --------------------------------------------------------- the checks ---

    def _authorize(self, principal: KeyPrincipal) -> UUID:
        """An ingest key, pinned to a project. Both, or nothing is written.

        The scope check is not redundant with the route's dependency: a
        control-plane key reaching this service would otherwise write events
        into a project it was never pinned to, and `project_id is None` is
        exactly that case.
        """
        if principal.scope not in (ApiKeyScope.INGEST, ApiKeyScope.FULL):
            raise PermissionDeniedError(
                "This key may not ingest events", scope=principal.scope.value
            )
        if principal.project_id is None:
            raise PermissionDeniedError(
                "An ingest key must be pinned to a project", api_key_id=str(principal.api_key_id)
            )
        return principal.project_id

    def _validated(
        self,
        principal: KeyPrincipal,
        raw: Mapping[str, Any],
        index: int,
        received_at: datetime,
    ) -> EventEnvelope:
        try:
            envelope = parse_envelope(raw)
        except ValidationFailedError as error:
            # Re-raised with the position, because an integrator sending 500
            # events needs to know which one, and the parser cannot know.
            raise ValidationFailedError(
                error.detail, **{**error.context, "index": index}
            ) from error

        skew_ms = envelope.skew_ms(received_at)
        if skew_ms > self._max_future_skew * 1000:
            raise ValidationFailedError(
                "This event is dated too far in the future; check the producer's clock",
                field="occurred_at",
                index=index,
                skew_seconds=round(skew_ms / 1000),
            )

        producer = spec.producer if (spec := spec_for(envelope.type)) else Producer.AGENT
        if not producer.is_ingestible:
            # An `approval.*` or orchestrator event arriving over ingest is
            # not late news, it is a forgery - and `approval.granted` folds a
            # step to `ready`, which `advance()` then dispatches. Refused on
            # the type alone, before anything is looked up.
            raise PermissionDeniedError(
                f"Events of type '{envelope.type}' are not accepted from an API key",
                event_type=envelope.type,
                producer=producer.value,
                index=index,
            )

        agent_key = envelope.correlation.agent_key or (
            envelope.subject.ref if envelope.subject.kind == "agent" else None
        )
        if agent_key and not principal.may_act_as(agent_key):
            # Not a 404: the key holder knows this agent exists, they just
            # may not speak for it. Pretending otherwise would send an
            # integrator hunting for a typo in a name that is correct.
            raise PermissionDeniedError(
                f"This key may not send events for the agent '{agent_key}'",
                agent_key=agent_key,
                index=index,
            )
        return envelope

    async def _check_step_ownership(
        self,
        principal: KeyPrincipal,
        events: EventRepository,
        envelope: EventEnvelope,
        index: int,
    ) -> None:
        """A step outcome may only be reported by the agent holding the step.

        The narrow half of D1. `workflow.step_succeeded` and `step_failed` are
        how a `dispatchable` agent reports work it was handed, so they cannot
        be refused outright - but an unscoped allowance would let any key
        settle any step, with any `output`, and the next step reads that
        output through the expression language.

        The check is here rather than in `_validated` because it needs the
        step's current state, and it runs before `append` so a refused event
        is not stored. Every failure is the same 403, deliberately: which of
        "no such step", "not dispatched" and "dispatched elsewhere" applies
        would describe someone else's execution graph.
        """
        spec = spec_for(envelope.type)
        if spec is None or spec.producer is not Producer.AGENT_FOR_DISPATCHED_STEP:
            return

        holder = await events.dispatched_step_agent(envelope.subject.ref)
        if holder is None or not principal.may_act_as(holder):
            raise PermissionDeniedError(
                "This key may not report an outcome for that step",
                event_type=envelope.type,
                step_ref=envelope.subject.ref,
                index=index,
            )

    def _reject_repeats_within_batch(self, envelopes: Sequence[EventEnvelope]) -> None:
        """Two events with one key in one batch is a producer bug.

        Caught here rather than left to the per-event dedup check, which runs
        against what is *committed* and so would accept both - the first is
        not visible to the second until the transaction ends. The result
        would be two rows with one key and a dedup guarantee that silently
        stopped holding.
        """
        seen: dict[str, int] = {}
        for index, envelope in enumerate(envelopes):
            if (first := seen.get(envelope.dedup_key)) is not None:
                raise ValidationFailedError(
                    "Two events in this batch share a dedup_key",
                    field="dedup_key",
                    value=envelope.dedup_key,
                    index=index,
                    first_seen_at_index=first,
                )
            seen[envelope.dedup_key] = index

    # -------------------------------------------------- the other three ---

    async def heartbeat(self, principal: KeyPrincipal, agent_key: str) -> IngestResult:
        """A convenience that is an event like any other.

        Synthesised here rather than given its own write path, so there is
        exactly one way an agent's `last_heartbeat_at` can move. The dedup key
        is the agent and the second it arrived: a worker heartbeating twice
        within one second is a retry, not two facts.
        """
        now = datetime.now(UTC)
        return await self.ingest(
            principal,
            [
                {
                    "dedup_key": f"{agent_key}:hb:{int(now.timestamp())}",
                    "type": "agent.heartbeat",
                    "protocol_version": PROTOCOL_VERSION,
                    "occurred_at": now.isoformat(),
                    "subject": {"kind": "agent", "ref": agent_key},
                }
            ],
        )

    async def ingest_logs(
        self, principal: KeyPrincipal, lines: Sequence[LogLine], *, agent_key: str | None
    ) -> int:
        """Store log lines. Changes no state, by construction."""
        project_id = self._authorize(principal)
        self._may_act_as(principal, agent_key)
        if not lines:
            raise ValidationFailedError("A batch needs at least one line", field="lines")
        if len(lines) > self._max_batch:
            raise ValidationFailedError(
                f"A batch holds at most {self._max_batch} lines",
                field="lines",
                sent=len(lines),
            )

        scope = TenantScope(workspace_id=principal.workspace_id, project_id=project_id)
        async with self._factory.begin(visible_workspaces={principal.workspace_id}) as work:
            telemetry = work.telemetry(scope)
            refs = await telemetry.resolve_agent(agent_key)
            return await telemetry.append_logs(lines, refs=refs)

    async def ingest_metrics(
        self, principal: KeyPrincipal, samples: Sequence[MetricPoint], *, agent_key: str | None
    ) -> int:
        project_id = self._authorize(principal)
        self._may_act_as(principal, agent_key)
        if not samples:
            raise ValidationFailedError("A batch needs at least one sample", field="samples")
        if len(samples) > self._max_batch:
            raise ValidationFailedError(
                f"A batch holds at most {self._max_batch} samples",
                field="samples",
                sent=len(samples),
            )

        scope = TenantScope(workspace_id=principal.workspace_id, project_id=project_id)
        async with self._factory.begin(visible_workspaces={principal.workspace_id}) as work:
            telemetry = work.telemetry(scope)
            refs = await telemetry.resolve_agent(agent_key)
            return await telemetry.append_metrics(samples, refs=refs)

    def _may_act_as(self, principal: KeyPrincipal, agent_key: str | None) -> None:
        if agent_key and not principal.may_act_as(agent_key):
            raise PermissionDeniedError(
                f"This key may not send telemetry for the agent '{agent_key}'",
                agent_key=agent_key,
            )

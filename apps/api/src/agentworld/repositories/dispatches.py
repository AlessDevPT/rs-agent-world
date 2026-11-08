"""Claiming work, holding a lease on it, and recording what happened.

**This module never creates a dispatch.** It claims, leases, releases and
settles rows the decide-and-apply path already wrote. Separate from
`repositories/executions.py` for exactly that reason: one repository that
could both create and claim is the natural place for someone to add "create
and immediately run", and that one line would collapse the outbox into a
direct call.

`tests/unit/test_dispatch_containment.py` enforces the split by verb. INSERT
is the dangerous one - it is how work comes into being - and remains confined
to `ExecutionRepository`. UPDATE on an existing row is bookkeeping about work
that was already authorized, and is allowed here.

The claim is the interesting statement:

```sql
UPDATE dispatches SET status='leased', leased_by=:worker, leased_until=now()+:ttl
WHERE id = (SELECT id FROM dispatches
            WHERE status='pending' AND run_after <= now() AND ...
            ORDER BY run_after
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
RETURNING ...
```

`FOR UPDATE SKIP LOCKED` is what makes two workers take two different rows
instead of blocking on one. The lease is a *lease*, not a lock: a worker that
dies holds nothing after `leased_until`, and the reaper puts the row back -
which is the only way a crashed worker's work ever moves again, since nothing
in this system can ask a dead process what it was doing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentworld.domain.enums import DispatchKind, DispatchStatus, SideEffect
from agentworld.repositories import models as m
from agentworld.repositories.base import ScopedRepository


@dataclass(frozen=True, slots=True)
class ClaimedDispatch:
    """One unit of work, as the worker that holds its lease sees it.

    Everything needed to perform it, and nothing that would let the worker
    decide something: there is no graph here, no step definition and no
    policy. The decision was taken by `advance()` and is frozen in
    `instruction` and `side_effect_class`.
    """

    id: UUID
    workspace_id: UUID
    project_id: UUID
    execution_id: UUID
    execution_step_id: UUID
    step_key: str
    kind: DispatchKind
    side_effect_class: SideEffect
    instruction: Mapping[str, Any]
    attempt: int
    max_attempts: int
    claim_count: int
    #: Stable across attempts of the same work, so a receiver that honours
    #: it can dedupe a retry.
    idempotency_token: UUID
    leased_until: dt.datetime


@dataclass(frozen=True, slots=True)
class ExpiredLease:
    """A lease whose worker is gone, and what it was doing.

    Everything here is read before `reclaim` overwrites it. The pair that
    matters most is `side_effect_in_flight` - which decides retry versus
    escalate - and `previous_error`, which is the history a recovery would
    otherwise destroy.
    """

    id: UUID
    #: True when `side_effect_started_at` was set: the call was in flight and
    #: **nobody knows whether it landed**.
    side_effect_in_flight: bool
    attempt: int
    max_attempts: int
    claim_count: int
    leased_by: str | None
    previous_error: str | None
    previous_error_code: str | None


class DispatchClaimRepository(ScopedRepository):
    """Claiming work **inside one workspace**, and nothing wider.

    Scoped rather than global, and that was not the first design. The first
    tried to claim across tenants, on the reasoning that a worker serves all
    of them - and it returned nothing, every time, because `dispatches` has
    row-level security enabled *and forced*, so a session with no visibility
    set sees an empty table. The failure mode of an unset session variable is
    total invisibility, exactly as migration 0004 intended.

    That is the isolation model working, not an obstacle to route around. The
    tables with no policy are `workspaces`, `workspace_members`, `users` and
    `api_keys` - the four that answer "which tenant is this?" - and
    `dispatches` is deliberately not among them. So the worker enumerates
    workspaces through the surface that exists for that, and claims within
    one: every statement against the outbox stays under RLS, and no new
    cross-tenant query is added.

    The cost is one claim attempt per workspace when the queue is empty. For
    a few hundred workspaces that is a cheap poll; past that it wants a
    notification rather than a scan, which `15-T6-WORKER.md` records as the
    obvious next change rather than pretending otherwise.
    """

    async def claim(
        self,
        *,
        worker: str,
        lease_seconds: int,
        kinds: Sequence[DispatchKind] | None = None,
    ) -> ClaimedDispatch | None:
        """Take the oldest due dispatch, or None if there is nothing to do.

        `SKIP LOCKED` rather than a plain `FOR UPDATE`: two workers polling
        at the same moment should take two rows, not queue behind one. The
        subquery is what keeps the lock to a single row - an `UPDATE ...
        WHERE status='pending'` without it would lock every pending row in
        the table for the duration.

        `claim_count` is incremented and `attempt` is not. T0 separated them:
        `attempt` is the identity of this unit of work and is immutable per
        row - a database trigger enforces it - while `claim_count` counts how
        many times a worker has leased it, which a retry after a crash
        legitimately increases.
        """
        due = (
            sa.select(m.Dispatch.id)
            .where(
                m.Dispatch.workspace_id == self.scope.workspace_id,
                m.Dispatch.status == DispatchStatus.PENDING,
                m.Dispatch.run_after <= sa.func.now(),
            )
            .order_by(m.Dispatch.run_after, m.Dispatch.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if kinds:
            due = due.where(m.Dispatch.kind.in_(list(kinds)))

        row = (
            await self._session.execute(
                sa.update(m.Dispatch)
                .where(m.Dispatch.id == due.scalar_subquery())
                .values(
                    status=DispatchStatus.LEASED,
                    leased_by=worker,
                    leased_until=sa.func.now()
                    + sa.cast(sa.text(f"interval '{int(lease_seconds)} seconds'"), sa.Interval),
                    claim_count=m.Dispatch.claim_count + 1,
                )
                .returning(m.Dispatch)
            )
        ).scalar_one_or_none()
        if row is None:
            return None

        # The step row is authoritative for both of these. `dispatches.
        # execution_id` is nullable in the schema and `execution_step_id` is
        # not - T0 made it NOT NULL because every dispatch kind belongs to a
        # step - so the step is the field that cannot be missing, and it
        # already knows which execution it is part of. Taking the execution
        # from here rather than from the nullable column removes a branch
        # that could only ever be reached by a hand-written row.
        step = (
            await self._session.execute(
                sa.select(m.ExecutionStep.step_key, m.ExecutionStep.execution_id).where(
                    m.ExecutionStep.id == row.execution_step_id
                )
            )
        ).one()

        return ClaimedDispatch(
            id=row.id,
            workspace_id=row.workspace_id,
            project_id=row.project_id,
            execution_id=step.execution_id,
            execution_step_id=row.execution_step_id,
            step_key=str(step.step_key),
            kind=row.kind,
            side_effect_class=row.side_effect_class,
            instruction=dict(row.instruction),
            attempt=row.attempt,
            max_attempts=row.max_attempts,
            claim_count=row.claim_count,
            idempotency_token=row.idempotency_token,
            # Just set to `now() + interval` by the statement above, so it
            # cannot be null. Narrowed rather than cast, so a change that
            # stopped setting it fails here instead of further away.
            leased_until=_required(row.leased_until),
        )

    async def mark_side_effect_started(self, dispatch_id: UUID) -> None:
        """Record that an outbound call is in flight, before making it.

        The order is the whole point: set this, then call. If the lease then
        expires with it still set, the side effect was in flight when the
        worker died and **nobody knows whether it landed**. The reaper reads
        this column to tell "crashed before doing anything" from "crashed
        mid-flight", and only the first is safe to retry.
        """
        await self._session.execute(
            sa.update(m.Dispatch)
            .where(
                m.Dispatch.id == dispatch_id,
                m.Dispatch.workspace_id == self.scope.workspace_id,
            )
            .values(side_effect_started_at=sa.func.now())
        )

    async def settle(
        self,
        dispatch_id: UUID,
        *,
        status: DispatchStatus,
        error: str | None = None,
        error_code: str | None = None,
        run_after: dt.datetime | None = None,
    ) -> bool:
        """Record the outcome of a lease this worker holds.

        Returns False when the row was not `leased` any more - which is how
        a worker whose lease expired mid-flight finds out that the reaper has
        already taken the row back. It must not then overwrite whatever the
        reaper decided, because the reaper may have decided `needs_review`.

        Clears the lease, and clears `side_effect_started_at` on success: the
        call returned, so the uncertainty it recorded is resolved. A failure
        keeps the marker, because the call did go out and that is worth
        knowing; what resolved the uncertainty there is the provider's own
        answer, recorded in `last_error_code`.

        **`finished_at` is stamped for every terminal status**, which was not
        true until the Phase 3 acceptance run looked. It stamped `succeeded`,
        `dead` and `cancelled` and skipped `failed` - so a terminally failed
        dispatch carried no finish time and looked unfinished to anything
        reading that column. `failed` is terminal here by construction: a
        retryable failure is settled as `pending`, because the claim query
        filters on `pending` and a `failed` row is never claimed again. One
        predicate rather than a list, so a status added later cannot be left
        out of it.
        """
        values: dict[str, Any] = {
            "status": status,
            "leased_by": None,
            "leased_until": None,
            "last_error": error,
            "last_error_code": error_code,
        }
        if status is DispatchStatus.SUCCEEDED:
            values["side_effect_started_at"] = None
        if status.is_terminal:
            values["finished_at"] = sa.func.now()
        if run_after is not None:
            values["run_after"] = run_after

        updated = (
            await self._session.execute(
                sa.update(m.Dispatch)
                .where(
                    m.Dispatch.id == dispatch_id,
                    m.Dispatch.workspace_id == self.scope.workspace_id,
                    # Only from `leased`. A worker reporting twice finds the
                    # row already settled and changes nothing, which is what
                    # makes result reporting idempotent at the storage layer
                    # rather than only in the caller.
                    m.Dispatch.status == DispatchStatus.LEASED,
                )
                .values(**values)
                .returning(m.Dispatch.id)
            )
        ).scalar_one_or_none()
        return updated is not None

    async def cancel_pending(self, execution_id: UUID) -> tuple[int, int]:
        """Cancel this execution's queued work. Returns (cancelled, in_flight).

        **Only `pending` rows.** A `leased` dispatch is already being
        performed by a worker, and there is no way to recall a request that
        has left the process - `02-ORCHESTRATION.md` §"Timeouts and
        cancellation" is explicit that those are *asked* to stop and settle
        when they report or their lease expires. Rewriting a leased row to
        `cancelled` here would take its lease away mid-call, and the worker's
        `settle` would then find nothing to update and silently drop a real
        outcome - possibly the record that a consequential call went out.

        So the in-flight count is returned rather than hidden: it is what
        lets a caller say "cancelling" instead of "cancelled", which is the
        one thing the specification insists the UI must not overclaim.

        `finished_at` is stamped because `cancelled` is terminal, the same
        rule `settle` follows.
        """
        cancelled = len(
            (
                await self._session.execute(
                    sa.update(m.Dispatch)
                    .where(
                        m.Dispatch.execution_id == execution_id,
                        m.Dispatch.workspace_id == self.scope.workspace_id,
                        m.Dispatch.status == DispatchStatus.PENDING,
                    )
                    .values(
                        status=DispatchStatus.CANCELLED,
                        last_error="the execution was cancelled",
                        last_error_code="execution_cancelled",
                        leased_by=None,
                        leased_until=None,
                        finished_at=sa.func.now(),
                    )
                    .returning(m.Dispatch.id)
                )
            ).all()
        )
        in_flight = (
            await self._session.execute(
                sa.select(sa.func.count())
                .select_from(m.Dispatch)
                .where(
                    m.Dispatch.execution_id == execution_id,
                    m.Dispatch.workspace_id == self.scope.workspace_id,
                    m.Dispatch.status == DispatchStatus.LEASED,
                )
            )
        ).scalar_one()
        return cancelled, int(in_flight)

    async def expire_leases(self, *, limit: int = 100) -> Sequence[ExpiredLease]:
        """Take back every lease that has run out. The reaper's one query.

        Returns everything the caller needs to decide what each row becomes
        *and* to record what it was, so the recovery needs no second read.
        The decision itself is not made here - "retry or escalate" is policy
        and this is storage.

        `previous_error` and `previous_error_code` are carried out because
        `reclaim` is about to overwrite them. Without that, a second recovery
        erases the first and the only record of why a dispatch has been round
        twice is gone.
        """
        rows = (
            await self._session.execute(
                sa.select(
                    m.Dispatch.id,
                    m.Dispatch.side_effect_started_at.is_not(None).label("in_flight"),
                    m.Dispatch.attempt,
                    m.Dispatch.max_attempts,
                    m.Dispatch.claim_count,
                    m.Dispatch.leased_by,
                    m.Dispatch.last_error,
                    m.Dispatch.last_error_code,
                )
                .where(
                    m.Dispatch.workspace_id == self.scope.workspace_id,
                    m.Dispatch.status == DispatchStatus.LEASED,
                    m.Dispatch.leased_until < sa.func.now(),
                )
                .order_by(m.Dispatch.leased_until)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        return [
            ExpiredLease(
                id=row.id,
                side_effect_in_flight=bool(row.in_flight),
                attempt=row.attempt,
                max_attempts=row.max_attempts,
                claim_count=row.claim_count,
                leased_by=row.leased_by,
                previous_error=row.last_error,
                previous_error_code=row.last_error_code,
            )
            for row in rows
        ]

    async def reclaim(self, dispatch_id: UUID, *, status: DispatchStatus, reason: str) -> bool:
        """Move an expired lease to whatever the reaper decided.

        Guarded on `leased` and on the lease still being expired, so a worker
        that was merely slow - and has just renewed or reported - is not
        overruled by a reaper that read the row a moment earlier.
        """
        updated = (
            await self._session.execute(
                sa.update(m.Dispatch)
                .where(
                    m.Dispatch.id == dispatch_id,
                    m.Dispatch.workspace_id == self.scope.workspace_id,
                    m.Dispatch.status == DispatchStatus.LEASED,
                    m.Dispatch.leased_until < sa.func.now(),
                )
                .values(
                    status=status,
                    leased_by=None,
                    leased_until=None,
                    last_error=reason,
                    last_error_code="lease_expired",
                )
                .returning(m.Dispatch.id)
            )
        ).scalar_one_or_none()
        return updated is not None


class DispatchReadRepository(ScopedRepository):
    """Reading dispatches within one tenant, for tests and later for the UI."""

    async def get(self, dispatch_id: UUID) -> Any:
        return (
            await self._session.execute(
                sa.select(m.Dispatch).where(
                    m.Dispatch.id == dispatch_id,
                    m.Dispatch.project_id == self.scope.require_project(),
                )
            )
        ).scalar_one_or_none()

    async def for_execution(self, execution_id: UUID) -> Sequence[Any]:
        rows = await self._session.execute(
            sa.select(m.Dispatch)
            .where(
                m.Dispatch.execution_id == execution_id,
                m.Dispatch.project_id == self.scope.require_project(),
            )
            .order_by(m.Dispatch.created_at)
        )
        return list(rows.scalars().all())

    async def needing_review(self, *, limit: int = 100) -> Sequence[Any]:
        """Dispatches waiting for a person, oldest first.

        Oldest first because this is a queue of stuck executions, not a feed:
        the one that has been waiting longest is the one most likely to
        matter. Each row is an execution that cannot finish until somebody
        says what happened.
        """
        rows = await self._session.execute(
            sa.select(m.Dispatch)
            .where(
                m.Dispatch.project_id == self.scope.require_project(),
                m.Dispatch.status == DispatchStatus.NEEDS_REVIEW,
            )
            .order_by(m.Dispatch.created_at)
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def resolve_review(
        self,
        dispatch_id: UUID,
        *,
        status: DispatchStatus,
        note: str,
        run_after: dt.datetime | None = None,
    ) -> Any:
        """Move a dispatch out of `needs_review`, once.

        Guarded on `status = 'needs_review'`, which is what makes a duplicate
        or conflicting resolution impossible rather than merely unlikely: two
        people pressing different buttons at the same moment produce one
        resolution and one 409. The caller turns a `None` into that.

        `side_effect_started_at` is cleared. The flag means "a call was in
        flight for this attempt", and a human has now said what became of
        it - so leaving it set would make the *next* crash of a retried
        dispatch look mid-flight when it had not started. What the flag
        recorded is not lost: it is in the `dispatch.recovered` audit row
        that escalated this dispatch in the first place, which nothing can
        edit.

        `note` goes into `last_error` because that is the column an operator
        reading the row will look at, and a resolution is the last thing that
        happened to it. The authoritative record, with the person's identity,
        is the audit row the caller writes in this same transaction.
        """
        values: dict[str, Any] = {
            "status": status,
            "leased_by": None,
            "leased_until": None,
            "side_effect_started_at": None,
            "last_error": note,
            "last_error_code": "resolved_by_review",
        }
        if run_after is not None:
            values["run_after"] = run_after
        # The same rule as `settle`: terminal means finished. A `retry`
        # resolution sets `pending`, which is not terminal and must not be
        # stamped - the work is about to happen again.
        if status.is_terminal:
            values["finished_at"] = sa.func.now()

        return (
            await self._session.execute(
                sa.update(m.Dispatch)
                .where(
                    m.Dispatch.id == dispatch_id,
                    m.Dispatch.project_id == self.scope.require_project(),
                    m.Dispatch.status == DispatchStatus.NEEDS_REVIEW,
                )
                .values(**values)
                .returning(m.Dispatch)
            )
        ).scalar_one_or_none()


def _required(when: dt.datetime | None) -> dt.datetime:
    if when is None:  # pragma: no cover - the claim statement always sets it
        raise ValueError("a claimed dispatch has no lease")
    return when

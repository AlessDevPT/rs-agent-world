"""The realtime spine's own two tables, and the event reads a replay needs.

Three narrow repositories, all scoped. Nothing here is a cross-tenant surface:
a ticket names its workspace in the string the client presents, so the
visibility set is established before the row is looked for - which is why
`ws_tickets` keeps a row-level security policy where `api_keys` could not.
See migration 0027.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentworld.repositories import models as m
from agentworld.repositories.base import ScopedRepository


@dataclass(frozen=True, slots=True)
class RedeemedTicket:
    """What a spent ticket says about the connection it authorises.

    Every field is read back from the row rather than taken from the string
    the client presented: the string's workspace id serves only to make the
    row visible to row-level security, and a connection must not be scoped by
    anything a caller chose.
    """

    ticket_id: UUID
    workspace_id: UUID
    project_id: UUID
    user_id: UUID
    secret_digest: str


class WsTicketRepository(ScopedRepository):
    """Minting and spending WebSocket tickets."""

    async def create(
        self,
        *,
        ticket_id: UUID,
        user_id: UUID,
        secret_digest: str,
        expires_at: dt.datetime,
    ) -> None:
        await self._session.execute(
            sa.insert(m.WsTicket).values(
                id=ticket_id,
                workspace_id=self.scope.workspace_id,
                project_id=self.scope.require_project(),
                user_id=user_id,
                secret_digest=secret_digest,
                expires_at=expires_at,
            )
        )

    async def consume(self, ticket_id: UUID, *, now: dt.datetime) -> RedeemedTicket | None:
        """Spend the ticket, or return None if it cannot be spent.

        One statement, and that is the point. `consumed_at IS NULL` in the
        WHERE clause means two concurrent connections presenting the same
        ticket contend for the same row and exactly one of them gets a
        RETURNING row - R2's "the same ticket twice is refused the second
        time" holds for a race as well as for a retry, which a read followed
        by a write could not promise.

        Expiry is checked here too rather than after the row comes back, so
        an expired ticket is not marked consumed: it was never usable, and
        leaving it untouched keeps "spent" meaning what it says. The caller
        still has to verify the secret's digest - the id alone is not the
        credential - and a digest mismatch leaves the ticket consumed, which
        is deliberate. Presenting a real id with the wrong secret is an
        attack on that ticket, and burning it is the right response.

        `project_id` is matched against the scope, so a ticket minted for
        another project in the same workspace is refused by the same query
        rather than by a later comparison somebody could forget (R3).
        """
        row = (
            await self._session.execute(
                sa.update(m.WsTicket)
                .where(
                    m.WsTicket.id == ticket_id,
                    m.WsTicket.workspace_id == self.scope.workspace_id,
                    m.WsTicket.project_id == self.scope.require_project(),
                    m.WsTicket.consumed_at.is_(None),
                    m.WsTicket.expires_at > now,
                )
                .values(consumed_at=now)
                .returning(
                    m.WsTicket.id,
                    m.WsTicket.workspace_id,
                    m.WsTicket.project_id,
                    m.WsTicket.user_id,
                    m.WsTicket.secret_digest,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return RedeemedTicket(
            ticket_id=row.id,
            workspace_id=row.workspace_id,
            project_id=row.project_id,
            user_id=row.user_id,
            secret_digest=row.secret_digest,
        )


class PublicationCursorRepository(ScopedRepository):
    """Where the publisher got to, per project (P10.4).

    Separate from `project_event_cursors` on purpose: that row is a project's
    ingest lock. Migration 0027 has the argument.
    """

    async def position(self) -> int:
        """The highest seq already published. Zero when nothing has been.

        No lock. Two publishers for one project would each publish some of
        the rows and both would be correct, because publication is
        at-least-once and every frame carries its seq - a client that has
        applied N discards anything at or below it. Serialising them would
        buy nothing and cost the thing the separate table was for.
        """
        found = (
            await self._session.execute(
                sa.select(m.EventPublicationCursor.published_seq).where(
                    m.EventPublicationCursor.project_id == self.scope.require_project()
                )
            )
        ).scalar_one_or_none()
        return int(found or 0)

    async def record(self, seq: int) -> None:
        """Remember that everything up to `seq` has reached the bus.

        `GREATEST` rather than a plain assignment, so a slow publisher
        finishing after a fast one cannot move the position backwards and
        cause a replay of frames already sent.
        """
        await self._session.execute(
            sa.dialects.postgresql.insert(m.EventPublicationCursor)
            .values(
                workspace_id=self.scope.workspace_id,
                project_id=self.scope.require_project(),
                published_seq=seq,
            )
            .on_conflict_do_update(
                index_elements=["project_id"],
                set_={
                    "published_seq": sa.func.greatest(m.EventPublicationCursor.published_seq, seq),
                    "updated_at": sa.func.now(),
                },
            )
        )


class ProjectPublicationRepository(ScopedRepository):
    """Which of a workspace's projects have events nobody has published yet.

    Workspace-scoped rather than project-scoped: the publisher sweeps, so it
    has to be able to ask the question without already knowing the answer.
    The comparison is between two cursors - the one ingest advances and the
    one publication advances - so a project that is up to date costs one
    index read and no event rows are touched.
    """

    async def projects_with_unpublished_events(self, *, limit: int = 100) -> Sequence[UUID]:
        allocated = sa.select(
            m.ProjectEventCursor.project_id.label("project_id"),
            (m.ProjectEventCursor.next_seq - 1).label("latest_seq"),
        ).subquery()
        rows = (
            await self._session.execute(
                sa.select(allocated.c.project_id)
                .outerjoin(
                    m.EventPublicationCursor,
                    m.EventPublicationCursor.project_id == allocated.c.project_id,
                )
                .where(
                    allocated.c.latest_seq
                    > sa.func.coalesce(m.EventPublicationCursor.published_seq, 0)
                )
                .order_by(allocated.c.project_id)
                .limit(limit)
            )
        ).all()
        return [row.project_id for row in rows]


class EventReadRepository(ScopedRepository):
    """Reading committed event rows back, in cursor order.

    The replay half of P9. Deliberately a *second* repository rather than
    methods on `EventRepository`: that one is the write path, pinned to the
    ingest transaction and holding the cursor lock, and nothing that only
    reads should be reachable from it.
    """

    async def latest_seq(self) -> int:
        """The highest seq allocated in this project. Zero if none.

        Read from `project_event_cursors` rather than `max(events.seq)`: it is
        one row by primary key instead of an aggregate over a partitioned
        table, and it is the same number the allocator will hand out next.
        Because the allocation holds the cursor row until its transaction
        commits, a seq that is visible implies every lower one is too - so
        "the highest visible seq" and "the set of visible events" agree, which
        is what lets a snapshot carry one number honestly.
        """
        found = (
            await self._session.execute(
                sa.select(m.ProjectEventCursor.next_seq).where(
                    m.ProjectEventCursor.project_id == self.scope.require_project()
                )
            )
        ).scalar_one_or_none()
        return int(found) - 1 if found is not None else 0

    async def oldest_seq(self) -> int | None:
        """The lowest seq still retained, or None when nothing is.

        The other half of the replay window. `ws_replay_window_events` bounds
        how far back a cursor may *ask*; this bounds how far back the rows
        actually go, which is what a dropped partition changes. Both are
        checked, because a window that only counted would promise a replay
        from rows that are no longer there.
        """
        found = (
            await self._session.execute(
                sa.select(sa.func.min(m.Event.seq)).where(
                    m.Event.project_id == self.scope.require_project()
                )
            )
        ).scalar_one_or_none()
        return int(found) if found is not None else None

    async def after(self, seq: int, *, limit: int, up_to: int | None = None) -> Sequence[Any]:
        """Committed rows with `seq > seq`, oldest first.

        Every column a frame can be built from, and no join: resolving the
        entity is the frame builder's job and happens against the definition
        tables, which is what keeps a payload of identity-and-operation able
        to produce a whole-entity delta (P10.3).
        """
        statement = (
            sa.select(
                m.Event.id,
                m.Event.seq,
                m.Event.type,
                m.Event.occurred_at,
                m.Event.received_at,
                m.Event.subject_kind,
                m.Event.subject_ref,
                m.Event.agent_id,
                m.Event.task_id,
                m.Event.run_id,
                m.Event.execution_id,
                m.Event.correlation,
                m.Event.severity,
                m.Event.labels,
                m.Event.payload,
            )
            .where(
                m.Event.project_id == self.scope.require_project(),
                m.Event.seq > seq,
            )
            .order_by(m.Event.seq)
            .limit(limit)
        )
        if up_to is not None:
            statement = statement.where(m.Event.seq <= up_to)
        return (await self._session.execute(statement)).all()

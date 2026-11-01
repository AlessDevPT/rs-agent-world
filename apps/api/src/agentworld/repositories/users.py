"""The users mirror and workspace memberships.

Both repositories here are global rather than tenant-scoped, and both have to
be: between them they *produce* the tenant scope that every other repository
requires. See `base.GlobalRepository`.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agentworld.domain.entities import (
    Membership,
    MembershipView,
    User,
    Workspace,
    WorkspaceMembership,
)
from agentworld.domain.enums import MemberRole
from agentworld.infra.auth.provider import VerifiedIdentity
from agentworld.repositories import models as m
from agentworld.repositories.base import GlobalRepository, ScopedRepository


class UserRepository(GlobalRepository):
    """The local mirror of the identity provider's users.

    Global because a token carries a subject and nothing else: there is no
    tenant to scope by until this lookup has happened.
    """

    async def upsert_from_identity(self, identity: VerifiedIdentity) -> User:
        """Create or refresh the mirror row for a verified token.

        One statement rather than select-then-insert: two concurrent first
        requests from the same new user would otherwise race, and one of them
        would fail on the primary key.

        **Which side wins for the profile fields.** `display_name` and
        `avatar_url` keep the *stored* value and fall back to the token's, so
        the identity provider's metadata seeds them once and the user's own
        edits survive. The other direction looks more natural - the provider
        is upstream, after all - and makes `PATCH /v1/me` useless: this method
        runs on every authenticated request, so a user who renamed themselves
        would be reverted by their own next page load. The trade-off, stated
        rather than hidden: a display name changed in Supabase after the first
        login does not propagate here.

        The email is different and is overwritten from the token every time.
        It is how members are invited and how an account is identified, so the
        provider has to stay authoritative for it.
        """
        insert = pg_insert(m.User).values(
            id=identity.subject,
            email=identity.email,
            display_name=identity.display_name,
            avatar_url=identity.avatar_url,
            last_seen_at=sa.func.now(),
        )
        # COALESCE over the EXCLUDED pseudo-row rather than over a bound
        # literal: EXCLUDED carries the column's own type, so a token with no
        # display name sends a typed NULL instead of an untyped parameter that
        # asyncpg cannot infer a type for.
        statement = insert.on_conflict_do_update(
            index_elements=[m.User.id],
            set_={
                "email": insert.excluded.email,
                "display_name": sa.func.coalesce(m.User.display_name, insert.excluded.display_name),
                "avatar_url": sa.func.coalesce(m.User.avatar_url, insert.excluded.avatar_url),
                "last_seen_at": sa.func.now(),
            },
        ).returning(m.User)
        row = (await self._session.execute(statement)).scalar_one()
        return _to_user(row)

    async def get(self, user_id: UUID) -> User | None:
        row = await self._session.get(m.User, user_id)
        return _to_user(row) if row is not None else None

    async def find_by_email(self, email: str) -> User | None:
        row = (
            await self._session.execute(sa.select(m.User).where(m.User.email == email))
        ).scalar_one_or_none()
        return _to_user(row) if row is not None else None

    async def update_profile(
        self,
        user_id: UUID,
        *,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> User | None:
        """Apply a partial update. Fields left as None are not touched.

        The API layer distinguishes "absent" from "explicitly null" before
        calling: this method only ever sets what it is given, so clearing a
        field is not expressible here and is not meant to be.
        """
        values: dict[str, object] = {}
        if display_name is not None:
            values["display_name"] = display_name
        if avatar_url is not None:
            values["avatar_url"] = avatar_url
        if not values:
            return await self.get(user_id)

        row = (
            await self._session.execute(
                sa.update(m.User).where(m.User.id == user_id).values(**values).returning(m.User)
            )
        ).scalar_one_or_none()
        return _to_user(row) if row is not None else None


class MembershipRepository(GlobalRepository):
    """Workspace membership: who belongs where, and as what.

    Global for the one method that needs it - `visible_to`, which answers
    "which workspaces may this user see" and is therefore the query that
    builds the row-level security session variable. Every other method here
    takes an explicit workspace id, and the service layer has already
    authorized it.
    """

    async def visible_to(self, user_id: UUID) -> dict[UUID, MemberRole]:
        """Every workspace this user belongs to, with their role.

        Called once per authenticated request. Deliberately the only query in
        the system that enumerates workspaces by user, and the reason
        `workspace_members` carries no RLS policy: a policy here would need
        this answer in order to produce it.
        """
        rows = await self._session.execute(
            sa.select(m.WorkspaceMember.workspace_id, m.WorkspaceMember.role)
            .join(m.Workspace, m.Workspace.id == m.WorkspaceMember.workspace_id)
            .where(m.WorkspaceMember.user_id == user_id, m.Workspace.archived_at.is_(None))
        )
        return {row.workspace_id: row.role for row in rows.all()}

    async def workspaces_for(self, user_id: UUID) -> list[WorkspaceMembership]:
        """The same memberships, with the workspace rows, for `GET /v1/me`."""
        rows = await self._session.execute(
            sa.select(m.Workspace, m.WorkspaceMember.role)
            .join(m.WorkspaceMember, m.WorkspaceMember.workspace_id == m.Workspace.id)
            .where(m.WorkspaceMember.user_id == user_id, m.Workspace.archived_at.is_(None))
            .order_by(m.Workspace.created_at)
        )
        return [
            WorkspaceMembership(workspace=_to_workspace(workspace), role=role)
            for workspace, role in rows.all()
        ]

    async def get(self, workspace_id: UUID, user_id: UUID) -> Membership | None:
        row = await self._session.get(m.WorkspaceMember, (workspace_id, user_id))
        return _to_membership(row) if row is not None else None

    async def add(
        self,
        workspace_id: UUID,
        user_id: UUID,
        role: MemberRole,
        *,
        invited_by: UUID | None = None,
    ) -> Membership:
        row = (
            await self._session.execute(
                sa.insert(m.WorkspaceMember)
                .values(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    role=role,
                    invited_by=invited_by,
                )
                .returning(m.WorkspaceMember)
            )
        ).scalar_one()
        return _to_membership(row)

    async def set_role(
        self, workspace_id: UUID, user_id: UUID, role: MemberRole
    ) -> Membership | None:
        row = (
            await self._session.execute(
                sa.update(m.WorkspaceMember)
                .where(
                    m.WorkspaceMember.workspace_id == workspace_id,
                    m.WorkspaceMember.user_id == user_id,
                )
                .values(role=role)
                .returning(m.WorkspaceMember)
            )
        ).scalar_one_or_none()
        return _to_membership(row) if row is not None else None

    async def remove(self, workspace_id: UUID, user_id: UUID) -> bool:
        # RETURNING rather than rowcount: the typed Result does not expose a
        # row count, and "which row went" is a better answer than "how many".
        removed = (
            await self._session.execute(
                sa.delete(m.WorkspaceMember)
                .where(
                    m.WorkspaceMember.workspace_id == workspace_id,
                    m.WorkspaceMember.user_id == user_id,
                )
                .returning(m.WorkspaceMember.user_id)
            )
        ).scalar_one_or_none()
        return removed is not None


class MemberListRepository(ScopedRepository):
    """Listing the members of one workspace. Scoped, unlike the above."""

    async def list_members(self) -> Sequence[MembershipView]:
        rows = await self._session.execute(
            sa.select(m.WorkspaceMember, m.User)
            .join(m.User, m.User.id == m.WorkspaceMember.user_id)
            .where(m.WorkspaceMember.workspace_id == self.scope.workspace_id)
            .order_by(m.WorkspaceMember.joined_at)
        )
        return [
            MembershipView(membership=_to_membership(membership), user=_to_user(user))
            for membership, user in rows.all()
        ]


def _to_user(row: m.User) -> User:
    return User(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        avatar_url=row.avatar_url,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
    )


def _to_membership(row: m.WorkspaceMember) -> Membership:
    return Membership(
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        role=row.role,
        invited_by=row.invited_by,
        joined_at=row.joined_at,
    )


def _to_workspace(row: m.Workspace) -> Workspace:
    return Workspace(
        id=row.id,
        slug=row.slug,
        name=row.name,
        created_by=row.created_by,
        settings=dict(row.settings),
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


#: Re-exported so `workspaces.py` and the services share one converter rather
#: than each writing their own and drifting.
to_workspace = _to_workspace
to_user = _to_user


__all__ = [
    "MemberListRepository",
    "MembershipRepository",
    "UserRepository",
    "to_user",
    "to_workspace",
]

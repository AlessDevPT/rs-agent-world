"""Shared types for the integration tests.

A module rather than part of `conftest.py`, because a conftest is loaded by
pytest as a plugin and is not importable from a test file. The fixtures that
build these live in `conftest.py`; the classes themselves live here so test
modules can name them in annotations.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from agentworld.domain.enums import MemberRole
from agentworld.domain.errors import AuthenticationFailedError
from agentworld.infra.auth.provider import VerifiedIdentity
from agentworld.infra.db import Database


class FakeAuthProvider:
    """Hands out identities for registered tokens.

    Stands in for Supabase, whose own verification is tested for real in
    `tests/unit/test_supabase_auth.py` with real keys and real signatures.
    What these tests need from it is the ability to be several different
    people in one test, which is awkward to arrange with real tokens and
    irrelevant to what is being asserted.
    """

    def __init__(self) -> None:
        self._identities: dict[str, VerifiedIdentity] = {}

    def register(self, token: str, identity: VerifiedIdentity) -> VerifiedIdentity:
        self._identities[token] = identity
        return identity

    async def verify(self, token: str) -> VerifiedIdentity:
        if (identity := self._identities.get(token)) is None:
            raise AuthenticationFailedError(
                "The credential presented could not be verified", reason="unknown_test_token"
            )
        return identity

    async def close(self) -> None:
        return None


class Actor:
    """A test user: their identity, their token, their request headers."""

    def __init__(self, auth: FakeAuthProvider, email: str) -> None:
        self.user_id = uuid.uuid4()
        self.email = email
        self.token = f"token-for-{email}"
        self.identity = auth.register(
            self.token,
            VerifiedIdentity(
                subject=self.user_id,
                email=email,
                display_name=email.split("@")[0].replace(".", " ").title(),
            ),
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


# ----------------------------------------------------------------- seeding ---


async def seed_user(owner_database: Database, user_id: uuid.UUID, email: str) -> None:
    """Insert a users row directly, for members who never make a request.

    `GET /v1/me` would create it, but a test about inviting someone needs the
    invitee to exist without having to pretend they logged in first.
    """
    async with owner_database.session() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": user_id, "email": email},
        )


async def seed_membership(
    owner_database: Database, workspace_id: uuid.UUID, user_id: uuid.UUID, role: MemberRole
) -> None:
    async with owner_database.session() as session:
        await session.execute(
            text(
                "INSERT INTO workspace_members (workspace_id, user_id, role) "
                "VALUES (:workspace_id, :user_id, :role)"
            ),
            {"workspace_id": workspace_id, "user_id": user_id, "role": role.value},
        )

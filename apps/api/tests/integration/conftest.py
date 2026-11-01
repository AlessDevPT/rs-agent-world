"""Integration fixtures: a real PostgreSQL, really migrated.

These tests are about things that only exist in the database - row-level
security, partial unique indexes, native enums, privilege grants - so there is
nothing here worth faking. A mocked session would assert that the code calls
SQLAlchemy, which is not the part that can be wrong.

**Skipping is loud.** Without Postgres the whole package skips, so a
contributor with no Docker still gets a useful `pytest` run. In CI that would
be a silent hole, so setting `AGENTWORLD_REQUIRE_INTEGRATION=1` turns the skip
into a failure. The CI workflow sets it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agentworld.api.deps import get_auth_provider
from agentworld.infra.db import Database
from agentworld.main import create_app
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.settings import Settings
from helpers import Actor, FakeAuthProvider

API_ROOT = Path(__file__).resolve().parents[2]

TEST_DATABASE = "agentworld_test"
PEPPER = "integration-test-pepper"

#: Every table migration 0003 creates, for truncation between tests.
#: CASCADE reaches everything that references `projects`, which covers the
#: Phase 2 definition and execution tables - but *not* the append-only tables:
#: those deliberately carry no foreign key to `projects`, so they have to be
#: named here or event rows would leak from one test into the next.
ALL_TABLES = (
    "api_keys, audit_log, projects, workspace_members, workspaces, users, "
    "events, logs, metric_samples"
)


def _swap_database(url: str, name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


def _base_settings() -> Settings:
    """Settings for the tests, built from the environment like the app's.

    `DATABASE_URL` and `MIGRATION_DATABASE_URL` are taken from `.env` or the
    environment and then pointed at the test database, so a contributor who
    has moved Postgres to another port does not have to configure anything
    twice.
    """
    configured = Settings()
    return Settings(
        environment="test",
        log_format="json",
        api_key_pepper=PEPPER,
        database_url=_swap_database(configured.database_url, TEST_DATABASE),
        migration_database_url=_swap_database(configured.effective_migration_url, TEST_DATABASE),
    )


def _owner_url_for(settings: Settings, database_name: str) -> str:
    return _swap_database(settings.effective_migration_url, database_name)


async def _reachable(url: str) -> str | None:
    """None when the server answers, otherwise why it did not."""
    engine = create_async_engine(url, poolclass=None)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        await engine.dispose()
    return None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def settings() -> AsyncIterator[Settings]:
    """Create and migrate the test database, or skip the package.

    The database is dropped and recreated per session rather than reused, so a
    migration edited mid-development cannot leave a half-applied schema behind
    to confuse the next run.

    `loop_scope="session"` because this fixture outlives any one test. It is
    safe here only because what it yields is plain data: the engines it opens
    are disposed before it returns, so no connection escapes onto a loop that
    is about to be closed.
    """
    configured = _base_settings()
    # `postgres` is the maintenance database: CREATE DATABASE cannot run from
    # inside the database being created.
    maintenance_url = _owner_url_for(configured, "postgres")

    if (problem := await _reachable(maintenance_url)) is not None:
        message = f"PostgreSQL is not reachable ({problem}). Run `npm run infra:up`."
        if os.environ.get("AGENTWORLD_REQUIRE_INTEGRATION") == "1":
            pytest.fail(f"{message} AGENTWORLD_REQUIRE_INTEGRATION=1 forbids skipping.")
        pytest.skip(message, allow_module_level=True)

    engine = create_async_engine(maintenance_url, poolclass=None)
    async with engine.connect() as conn:
        # CREATE/DROP DATABASE cannot run inside a transaction block.
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DATABASE}"'))
    await engine.dispose()

    # A subprocess, not `alembic.command`: `migrations/env.py` ends in
    # `asyncio.run(...)`, which cannot be called from inside the event loop
    # this fixture is running on. Running the real command also means the
    # tests exercise the path an operator uses.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            f"url={configured.effective_migration_url}",
            "upgrade",
            "head",
        ],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")

    yield configured

    engine = create_async_engine(maintenance_url, poolclass=None)
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)'))
    await engine.dispose()


@pytest_asyncio.fixture
async def owner_database(settings: Settings) -> AsyncIterator[Database]:
    """Connected as the schema owner, which here is a superuser.

    Used for seeding and truncating, and - in `test_rls.py` - to demonstrate
    that a superuser bypasses the policies, which is the whole reason the
    application connects as someone else.
    """
    database = Database(settings.effective_migration_url)
    yield database
    await database.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(owner_database: Database) -> AsyncIterator[None]:
    """Empty every table before each test.

    Truncation runs as the owner: the application role is deliberately not
    granted TRUNCATE, and on `audit_log` not even DELETE.
    """
    async with owner_database.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
    yield


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    """Connected as the application role: not a superuser, no BYPASSRLS."""
    database = Database(settings.database_url)
    yield database
    await database.dispose()


@pytest_asyncio.fixture
async def factory(database: Database) -> UnitOfWorkFactory:
    return UnitOfWorkFactory(database)


# ------------------------------------------------------------- fake identity ---


@pytest.fixture
def auth() -> FakeAuthProvider:
    return FakeAuthProvider()


@pytest.fixture
def app(settings: Settings, auth: FakeAuthProvider) -> FastAPI:
    application = create_app(settings)
    # A dependency override rather than writing to `app.state`: the lifespan
    # owns that attribute and would overwrite anything set before it runs.
    application.dependency_overrides[get_auth_provider] = lambda: auth
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------- test actors ---


@pytest.fixture
def actor(auth: FakeAuthProvider) -> Actor:
    return Actor(auth, "owner@example.com")


@pytest.fixture
def make_actor(auth: FakeAuthProvider) -> Any:
    counter = iter(range(1, 1000))

    def build(email: str | None = None) -> Actor:
        return Actor(auth, email or f"user{next(counter)}@example.com")

    return build

"""Alembic environment.

Two choices worth knowing about:

**The URL comes from application settings, not alembic.ini.** One source of
truth for "which database", so `alembic upgrade head` cannot be pointed at
something other than what the app will then talk to. `-x url=...` overrides it
for the throwaway databases the integration tests create.

**It runs async.** The app's driver is asyncpg; running migrations through a
second, synchronous driver would mean installing psycopg as well and having
two code paths that can disagree about TLS, search_path or type handling.
"""

from __future__ import annotations

import asyncio
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config, create_async_engine

from agentworld.repositories.models import Base
from agentworld.settings import Settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """The owner's URL, not the application's.

    Migrations need DDL rights; the application role has none and should keep
    none. `-x url=...` overrides both, which is how the integration tests
    point Alembic at a throwaway database.
    """
    override = context.get_x_argument(as_dictionary=True).get("url")
    return override or Settings().effective_migration_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Native enum types are created by 0002 and owned by it; autogenerate
        # must not propose creating or dropping them as a side effect of a
        # table change.
        include_object=_include_object,
        compare_type=True,
        compare_server_default=True,
        # One transaction per migration, not one for the whole chain.
        #
        # Required, not a preference: PostgreSQL refuses to *use* an enum
        # value in the same transaction that added it, so migration 0006's
        # `ALTER TYPE agent_status ADD VALUE 'paused'` and migration 0007's
        # index predicate referencing 'paused' cannot share one. With the
        # default they do, and the upgrade fails with
        # `UnsafeNewEnumValueUsageError`. Any later migration that adds an
        # enum member and then uses it would hit the same wall.
        #
        # The trade is real and worth stating: a failure part-way through the
        # chain now leaves the earlier migrations applied, rather than rolling
        # the whole upgrade back. That is the conventional choice for a
        # forward-only chain, and the schema drift test is what catches a
        # database left in a half-migrated state.
        transaction_per_migration=True,
    )


#: Monthly partitions are named `<table>_y2026m11`. Phase 2 creates them from
#: a database function rather than from migrations, so autogenerate has to
#: ignore them or every run would propose dropping the lot.
_PARTITION_SUFFIX = re.compile(r"_y\d{4}m\d{2}$")


def _include_object(obj: object, name: str | None, type_: str, *_args: object) -> bool:
    is_partition = (
        type_ == "table" and name is not None and _PARTITION_SUFFIX.search(name) is not None
    )
    return not is_partition


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = _database_url()
    if config.get_section(config.config_ini_section):
        engine = create_async_engine(url, poolclass=None)
    else:  # pragma: no cover - defensive; alembic always provides the section
        engine = async_engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.")

    async with engine.connect() as connection:
        await connection.run_sync(_run_with_connection)
    await engine.dispose()


def _run_with_connection(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

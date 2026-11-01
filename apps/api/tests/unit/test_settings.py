"""Settings, in particular the refusal to boot misconfigured.

Every test here constructs `Settings` with `_env_file=None` and an
environment stripped of `AGENTWORLD`-relevant variables, through the
`isolated_settings` fixture. That is not ceremony: these tests assert what
happens when a value is *missing*, and without isolation they assert it
against whatever `.env` the developer happens to have on disk. The
fail-fast test below did exactly that until a local `.env` with a
development pepper in it turned a genuine assertion into a passing one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from agentworld.settings import Settings

#: Variables that would otherwise leak a developer's or CI's configuration
#: into a test about missing configuration.
_LEAKY_VARS = (
    "ENVIRONMENT",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "REDIS_URL",
    "EVENT_BUS",
    "SUPABASE_URL",
    "SUPABASE_JWKS_URL",
    "SUPABASE_JWT_AUDIENCE",
    "API_KEY_PEPPER",
    "CORS_ORIGINS",
)

SettingsFactory = Callable[..., Settings]


@pytest.fixture
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[SettingsFactory]:
    """Build `Settings` from arguments alone - no `.env`, no environment."""
    for name in _LEAKY_VARS:
        monkeypatch.delenv(name, raising=False)

    def build(**overrides: Any) -> Settings:
        return Settings(_env_file=None, **overrides)

    yield build


def test_the_fixture_really_is_isolated(isolated_settings: SettingsFactory) -> None:
    """Guard on the guard.

    If `_env_file=None` ever stopped being honoured, every "missing value"
    test below would quietly start passing for the wrong reason. This asserts
    the unset defaults are actually unset.
    """
    s = isolated_settings()
    assert s.api_key_pepper == ""
    assert s.supabase_url == ""
    assert s.supabase_jwks_url == ""


def test_local_defaults_work_without_secrets(isolated_settings: SettingsFactory) -> None:
    s = isolated_settings(environment="local")
    assert not s.is_deployed
    assert s.event_bus == "inprocess"


def test_cors_origins_parse_into_a_list(isolated_settings: SettingsFactory) -> None:
    s = isolated_settings(cors_origins="http://a.test, http://b.test ,")
    assert s.cors_origin_list == ["http://a.test", "http://b.test"]


def test_production_refuses_to_boot_without_secrets(
    isolated_settings: SettingsFactory,
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        isolated_settings(environment="production", event_bus="redis")
    message = str(excinfo.value)
    assert "API_KEY_PEPPER" in message
    assert "SUPABASE_URL" in message
    assert "SUPABASE_JWKS_URL" in message


def test_production_refuses_the_inprocess_bus(isolated_settings: SettingsFactory) -> None:
    # The in-process bus only reaches sockets on its own worker, so it is
    # silently wrong behind more than one replica. Fail loudly instead.
    with pytest.raises(ValidationError, match="EVENT_BUS"):
        isolated_settings(
            environment="production",
            event_bus="inprocess",
            api_key_pepper="x",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/jwks",
        )


def test_deployed_settings_accept_a_complete_configuration(
    isolated_settings: SettingsFactory,
) -> None:
    s = isolated_settings(
        environment="production",
        event_bus="redis",
        api_key_pepper="pepper",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/jwks",
    )
    assert s.is_deployed


def test_the_migration_url_falls_back_to_the_application_url(
    isolated_settings: SettingsFactory,
) -> None:
    """A single-role deployment is valid: FORCE ROW LEVEL SECURITY covers an
    owner, just not a superuser."""
    s = isolated_settings(database_url="postgresql+asyncpg://one:role@host/db")
    assert s.effective_migration_url == "postgresql+asyncpg://one:role@host/db"


def test_a_separate_migration_url_is_preferred_when_given(
    isolated_settings: SettingsFactory,
) -> None:
    s = isolated_settings(
        database_url="postgresql+asyncpg://app:p@host/db",
        migration_database_url="postgresql+asyncpg://owner:p@host/db",
    )
    assert s.effective_migration_url == "postgresql+asyncpg://owner:p@host/db"


def test_settings_are_immutable(isolated_settings: SettingsFactory) -> None:
    s = isolated_settings()
    with pytest.raises(ValidationError):
        s.environment = "production"  # type: ignore[misc]


@pytest.mark.parametrize("value", [0, 4, 3601])
def test_heartbeat_ttl_is_bounded(isolated_settings: SettingsFactory, value: int) -> None:
    with pytest.raises(ValidationError):
        isolated_settings(default_heartbeat_ttl_seconds=value)

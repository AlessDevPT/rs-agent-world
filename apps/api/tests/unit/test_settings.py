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

import base64
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from agentworld.infra import secrets
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
    "CREDENTIAL_KEKS",
    "CREDENTIAL_ACTIVE_KEK_ID",
    "CORS_ORIGINS",
)

#: A key that is not the development one, for the deployed-configuration
#: tests. 32 bytes, because `Settings` checks the length at boot.
REAL_KEK = base64.b64encode(bytes(range(32))).decode()

#: What a complete deployed configuration looks like. Spelled out once: every
#: test that asserts a *missing* value leaves exactly one of these out, and
#: building it by hand in each would eventually mean a test passing because
#: it forgot two things rather than one.
DEPLOYED = {
    "environment": "production",
    "event_bus": "redis",
    "api_key_pepper": "pepper",
    "supabase_url": "https://x.supabase.co",
    "supabase_jwks_url": "https://x.supabase.co/jwks",
    "credential_keks": json.dumps({"k2026a": REAL_KEK}),
    "credential_active_kek_id": "k2026a",
}

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
    assert s.credential_keks == ""
    assert s.credential_active_kek_id == ""


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
    assert "CREDENTIAL_KEKS" in message
    assert "CREDENTIAL_ACTIVE_KEK_ID" in message


def test_production_refuses_the_inprocess_bus(isolated_settings: SettingsFactory) -> None:
    # The in-process bus only reaches sockets on its own worker, so it is
    # silently wrong behind more than one replica. Fail loudly instead.
    with pytest.raises(ValidationError, match="EVENT_BUS"):
        isolated_settings(**{**DEPLOYED, "event_bus": "inprocess"})


def test_deployed_settings_accept_a_complete_configuration(
    isolated_settings: SettingsFactory,
) -> None:
    s = isolated_settings(**DEPLOYED)
    assert s.is_deployed
    assert s.credential_keyring.active_id == "k2026a"


# ------------------------------------------------------- the credential keyring ---


def test_local_falls_back_to_the_development_keyring(
    isolated_settings: SettingsFactory,
) -> None:
    """A local run needs no key material configured, and gets a fixed key.

    Fixed rather than per-process, so a credential written before a restart
    is readable after it. Nothing deployed can reach this: the two tests
    below are what stop it.
    """
    s = isolated_settings(environment="local")
    assert s.credential_keyring.active_id == secrets.DEVELOPMENT_KEY_ID
    assert s.credential_keyring.holds_the_development_key


@pytest.mark.parametrize("omitted", ["credential_keks", "credential_active_kek_id"])
def test_production_refuses_a_half_configured_keyring(
    isolated_settings: SettingsFactory, omitted: str
) -> None:
    """Half of the pair is a misconfiguration, not a default to fill in."""
    with pytest.raises(ValidationError, match="CREDENTIAL"):
        isolated_settings(**{**DEPLOYED, omitted: ""})


def test_production_refuses_the_development_key_pasted_in(
    isolated_settings: SettingsFactory,
) -> None:
    """Worse than leaving it unset, because it looks configured.

    The check is on the key material, not on the id, so dressing it up as
    `k2026a` does not get it past.
    """
    disguised = base64.b64encode(secrets.development().active).decode()
    with pytest.raises(ValidationError, match="development key"):
        isolated_settings(**{**DEPLOYED, "credential_keks": json.dumps({"k2026a": disguised})})


@pytest.mark.parametrize(
    "keks",
    ["{not json", '{"k2026a": "!!!"}', '{"k2026a": "' + base64.b64encode(b"tiny").decode() + '"}'],
)
def test_a_malformed_keyring_fails_at_boot_even_locally(
    isolated_settings: SettingsFactory, keks: str
) -> None:
    """Not a deployed-only check.

    A present-but-broken `CREDENTIAL_KEKS` is wrong wherever it appears, and
    finding out at the first credential write means a process that started
    healthy and cannot store a secret.
    """
    with pytest.raises(ValidationError):
        isolated_settings(
            environment="local", credential_keks=keks, credential_active_kek_id="k2026a"
        )


def test_a_boot_failure_does_not_print_the_key_material(
    isolated_settings: SettingsFactory,
) -> None:
    """The one error message nobody can suppress must not carry a key.

    pydantic renders the input that failed validation by default, and the
    input to this model is every environment variable the API reads -
    `CREDENTIAL_KEKS` among them. A boot failure is the message most likely
    to end up in a ticket or a log aggregator, so `hide_input_in_errors`
    turns it off. This test is what keeps it off.
    """
    with pytest.raises(ValidationError) as excinfo:
        isolated_settings(**{**DEPLOYED, "event_bus": "inprocess"})
    message = str(excinfo.value)

    # `input_value` is the rendering, and asserting its absence is what
    # makes this test non-vacuous. Greping for the whole key is not enough:
    # pydantic elides the *middle* of a long input and keeps both ends, so a
    # 44-character base64 key can be half-printed while a search for all of
    # it finds nothing. Half a key is still half a key.
    assert "input_value" not in message
    assert REAL_KEK[:16] not in message
    assert REAL_KEK[-16:] not in message
    assert "api_key_pepper" not in message
    # Still useful: it says which setting is wrong.
    assert "EVENT_BUS" in message


def test_an_active_id_outside_the_map_fails_at_boot(isolated_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="not one of the keys given"):
        isolated_settings(
            credential_keks=json.dumps({"k2026a": REAL_KEK}),
            credential_active_kek_id="k2026b",
        )


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

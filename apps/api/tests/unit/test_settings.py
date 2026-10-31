from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentworld.settings import Settings


def test_local_defaults_work_without_secrets() -> None:
    s = Settings(environment="local")
    assert not s.is_deployed
    assert s.event_bus == "inprocess"


def test_cors_origins_parse_into_a_list() -> None:
    s = Settings(cors_origins="http://a.test, http://b.test ,")
    assert s.cors_origin_list == ["http://a.test", "http://b.test"]


def test_production_refuses_to_boot_without_secrets() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(environment="production", event_bus="redis")
    message = str(excinfo.value)
    assert "API_KEY_PEPPER" in message
    assert "SUPABASE_URL" in message


def test_production_refuses_the_inprocess_bus() -> None:
    # The in-process bus only reaches sockets on its own worker, so it is
    # silently wrong behind more than one replica. Fail loudly instead.
    with pytest.raises(ValidationError, match="EVENT_BUS"):
        Settings(
            environment="production",
            event_bus="inprocess",
            api_key_pepper="x",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/jwks",
        )


def test_deployed_settings_accept_a_complete_configuration() -> None:
    s = Settings(
        environment="production",
        event_bus="redis",
        api_key_pepper="pepper",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/jwks",
    )
    assert s.is_deployed


def test_settings_are_immutable() -> None:
    s = Settings()
    with pytest.raises(ValidationError):
        s.environment = "production"  # type: ignore[misc]


@pytest.mark.parametrize("value", [0, 4, 3601])
def test_heartbeat_ttl_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(default_heartbeat_ttl_seconds=value)

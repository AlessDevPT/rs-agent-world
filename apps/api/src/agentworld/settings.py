"""Application configuration.

Every environment variable the API reads is declared exactly once, here.
Secrets have no working defaults: outside `local` the settings object refuses
to construct without them, so a misconfigured deployment fails at boot rather
than at the first request that needs the missing value.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentworld.infra import secrets

Environment = Literal["local", "test", "staging", "production"]
EventBusKind = Literal["inprocess", "redis"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # Without this, a pydantic validation error renders the input that
        # failed - and the input to this model includes `CREDENTIAL_KEKS`
        # and `API_KEY_PEPPER`. A boot failure is exactly the message most
        # likely to be copied into a terminal, a ticket or a log aggregator,
        # so the one error nobody can suppress must not carry key material.
        # The field names are still named, which is all an operator needs.
        hide_input_in_errors=True,
    )

    environment: Environment = "local"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    #: The application's own connection. This role must NOT be a superuser
    #: and must NOT hold BYPASSRLS, or migration 0004's row-level security
    #: policies are silently inert. `test_rls.py` asserts that at runtime.
    database_url: str = "postgresql+asyncpg://agentworld_app:app@localhost:5432/agentworld"
    #: The schema owner, used only by `alembic upgrade`. Empty means "the same
    #: role as the application", which is a valid single-role deployment:
    #: FORCE ROW LEVEL SECURITY covers an owner, just not a superuser.
    migration_database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"

    event_bus: EventBusKind = "inprocess"

    supabase_url: str = ""
    supabase_jwks_url: str = ""
    supabase_jwt_audience: str = "authenticated"

    api_key_pepper: str = ""

    #: The key-encryption keys for `credentials`, as a JSON object of
    #: `{"<key id>": "<base64 of 32 bytes>"}`, and the id of the one that
    #: wraps new data keys. Both empty means the development keyring, which
    #: `_require_secrets_when_deployed` permits only outside staging and
    #: production. Parsed at boot by the same validator, so a malformed map
    #: stops the process rather than the first credential write.
    credential_keks: str = ""
    credential_active_kek_id: str = ""

    default_heartbeat_ttl_seconds: int = Field(default=60, ge=5, le=3600)
    ws_replay_window_events: int = Field(default=10_000, ge=100)
    ingest_max_batch_events: int = Field(default=500, ge=1, le=5_000)
    ingest_max_future_skew_seconds: int = Field(default=300, ge=0)

    #: How often the reaper sweeps for expired leases. A quarter of the
    #: 120-second lease, so a crashed worker's dispatch moves within about a
    #: quarter of the window rather than at the end of it. An interval longer
    #: than the lease leaves a gap in which a row is expired and nothing has
    #: noticed - not corruption, since every guard is on the row rather than
    #: on the schedule, but delay nobody chose.
    reaper_interval_seconds: int = Field(default=30, ge=1, le=3600)
    #: Expired leases taken back per workspace per sweep. Promoted from the
    #: repository's own default so a backlog can be drained faster without a
    #: code change.
    reaper_batch_limit: int = Field(default=100, ge=1, le=10_000)
    #: Consecutive failed sweeps before the loop exits non-zero. A reaper
    #: spinning against a database that is refusing it is a reaper nobody
    #: notices; exiting hands the problem to the supervisor, where it is
    #: visible.
    reaper_max_consecutive_errors: int = Field(default=5, ge=1, le=100)

    cors_origins: str = "http://localhost:3000"

    otel_exporter_otlp_endpoint: str = ""

    @property
    def effective_migration_url(self) -> str:
        return self.migration_database_url or self.database_url

    @property
    def is_deployed(self) -> bool:
        return self.environment in ("staging", "production")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def credential_keyring(self) -> secrets.Keyring:
        """The KEKs this process can unwrap with.

        Built on each access rather than cached: it is a few microseconds of
        base64, `Settings` is frozen so the inputs cannot change under it,
        and caching a keyring on a settings object would put key material in
        a longer-lived place than it needs to be. The validator below has
        already proved this cannot raise.
        """
        return secrets.load(self.credential_keks, self.credential_active_kek_id)

    @model_validator(mode="after")
    def _require_secrets_when_deployed(self) -> Settings:
        # Parsed in every environment, deployed or not: a `CREDENTIAL_KEKS`
        # that is present and malformed is a misconfiguration wherever it
        # appears, and discovering it at the first credential write means a
        # process that started healthy and cannot store a secret.
        keyring = secrets.load(self.credential_keks, self.credential_active_kek_id)

        if not self.is_deployed:
            return self
        missing = [
            name
            for name, value in (
                ("API_KEY_PEPPER", self.api_key_pepper),
                ("SUPABASE_URL", self.supabase_url),
                ("SUPABASE_JWKS_URL", self.supabase_jwks_url),
                ("CREDENTIAL_KEKS", self.credential_keks),
                ("CREDENTIAL_ACTIVE_KEK_ID", self.credential_active_kek_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"{self.environment} requires these to be set: {', '.join(missing)}")
        if keyring.holds_the_development_key:
            # Unreachable through `missing` above, which only sees an empty
            # string. This catches a deployment that pasted the development
            # key in on purpose, which is worse than leaving the variables
            # unset because it looks configured.
            raise ValueError(
                "CREDENTIAL_KEKS contains the development key; "
                f"{self.environment} needs real key material"
            )
        if self.event_bus != "redis":
            # More than one API worker is the norm once deployed, and the
            # in-process bus only reaches sockets held by its own worker.
            raise ValueError("EVENT_BUS must be 'redis' outside local/test")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

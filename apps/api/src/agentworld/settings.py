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

Environment = Literal["local", "test", "staging", "production"]
EventBusKind = Literal["inprocess", "redis"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
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

    default_heartbeat_ttl_seconds: int = Field(default=60, ge=5, le=3600)
    ws_replay_window_events: int = Field(default=10_000, ge=100)
    ingest_max_batch_events: int = Field(default=500, ge=1, le=5_000)
    ingest_max_future_skew_seconds: int = Field(default=300, ge=0)

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

    @model_validator(mode="after")
    def _require_secrets_when_deployed(self) -> Settings:
        if not self.is_deployed:
            return self
        missing = [
            name
            for name, value in (
                ("API_KEY_PEPPER", self.api_key_pepper),
                ("SUPABASE_URL", self.supabase_url),
                ("SUPABASE_JWKS_URL", self.supabase_jwks_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"{self.environment} requires these to be set: {', '.join(missing)}")
        if self.event_bus != "redis":
            # More than one API worker is the norm once deployed, and the
            # in-process bus only reaches sockets held by its own worker.
            raise ValueError("EVENT_BUS must be 'redis' outside local/test")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

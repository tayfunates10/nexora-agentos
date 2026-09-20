import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXORA_", extra="ignore")
    database_url: SecretStr = SecretStr("postgresql://localhost/nexora")
    # Set only on the migration process in production. These are capability role
    # names, not credentials; the workload-specific login URLs stay in Secrets.
    database_api_role: str | None = None
    database_worker_role: str | None = None
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    dependency_timeout_seconds: float = 2.0
    # The issuer/audience are operator trust anchors. A static PEM remains an optional
    # pin; otherwise the API discovers and rotates bounded RS256 JWKS keys.
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_public_key: str | None = None
    auth_jwks_url: str | None = None
    auth_jwks_cache_seconds: int = Field(default=300, ge=30, le=86_400)
    auth_jwks_min_refresh_seconds: int = Field(default=10, ge=1, le=300)
    auth_jwks_timeout_seconds: float = Field(default=3.0, gt=0.0, le=15.0)
    # Shared authenticated-request limiter. Zero disables it for isolated library/test use;
    # repository deployment configs enable it explicitly.
    api_rate_limit_requests: int = Field(default=0, ge=0, le=100_000)
    api_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    # Telemetry is operator-owned. Without an exporter endpoint nothing leaves the
    # process, and without a metrics token the scrape endpoint stays disabled.
    service_name: str = Field(default="nexora-api", min_length=1, max_length=64)
    service_version: str = Field(default="0.1.0", min_length=1, max_length=32)
    otel_exporter_endpoint: str | None = None
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    otel_export_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    metrics_token: SecretStr | None = None
    log_level: str = "INFO"
    # Worker process settings. Model profiles come from an operator-mounted file and
    # provider credentials from the environment; a run can influence neither.
    worker_runtime_config: str | None = None
    worker_lease_seconds: int = Field(default=30, ge=3, le=300)
    worker_idle_sleep_seconds: float = Field(default=0.5, gt=0.0, le=5.0)
    worker_shutdown_grace_seconds: float = Field(default=25.0, ge=1.0, le=300.0)
    worker_admin_port: int = Field(default=8001, ge=1, le=65535)
    openai_api_key: SecretStr | None = None

    @field_validator("database_api_role", "database_worker_role", mode="before")
    @classmethod
    def _database_role_name(cls, value):
        if value in (None, ""):
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
            raise ValueError("database runtime role must be a simple PostgreSQL identifier")
        return value

    @field_validator("auth_jwks_url", mode="before")
    @classmethod
    def _jwks_url(cls, value):
        if value in (None, ""):
            return None
        if not isinstance(value, str) or len(value) > 500 or not value.startswith("https://"):
            raise ValueError("auth_jwks_url must be an HTTPS URL")
        return value

    @field_validator("otel_exporter_endpoint")
    @classmethod
    def _collector_endpoint(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not value.startswith(("http://", "https://")) or len(value) > 500:
            raise ValueError("otel_exporter_endpoint must be an http(s) collector URL")
        return value

    @field_validator("metrics_token")
    @classmethod
    def _scrape_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        if len(value.get_secret_value()) < 32:
            raise ValueError("metrics_token must be at least 32 characters")
        return value

    @field_validator("log_level")
    @classmethod
    def _level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(LOG_LEVELS)}")
        return level

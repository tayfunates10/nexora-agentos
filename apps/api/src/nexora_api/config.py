from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXORA_", extra="ignore")
    database_url: SecretStr = SecretStr("postgresql://localhost/nexora")
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    dependency_timeout_seconds: float = 2.0
    # A deployment-managed RSA public key: tokens cannot choose a key URL.
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_public_key: str | None = None
    # Telemetry is operator-owned. Without an exporter endpoint nothing leaves the
    # process, and without a metrics token the scrape endpoint stays disabled.
    service_name: str = Field(default="nexora-api", min_length=1, max_length=64)
    service_version: str = Field(default="0.1.0", min_length=1, max_length=32)
    otel_exporter_endpoint: str | None = None
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    otel_export_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    metrics_token: SecretStr | None = None
    log_level: str = "INFO"

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

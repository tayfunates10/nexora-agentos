import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from nexora_api.secret_vault import SecretVault, VaultUnavailable, build_vault, parse_master_keys

LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SUBJECT_RE = re.compile(r"^[^\s,]{1,255}$")


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
    # Integration vault. Master keys are supplied by the operator's secret store as a JSON
    # object of base64 256-bit keys; several may be present so a rotation can still read
    # credentials sealed under the previous one. Without them the platform refuses to
    # store tenant credentials rather than keeping them readable.
    secret_vault_keys: SecretStr | None = None
    secret_vault_active_key: str | None = None
    # Platform administrators run the standard agent catalog. Membership is deployment
    # configuration: no API grants it, and no tenant role implies it.
    platform_admin_subjects: str = ""
    # OAuth client registrations per connector, as a JSON object keyed by integration id.
    # Client secrets belong to the operator and never reach the database or a tenant.
    integration_oauth_clients: SecretStr | None = None
    integration_request_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    # Optional isolated Playwright service. Standard agents only receive the browser tool
    # when both values are configured; the browser origin itself is frozen in the run snapshot.
    browser_runtime_url: str | None = None
    browser_runtime_token: SecretStr | None = None
    # The runtime contract standard agents declare a minimum against. It moves with the
    # platform, independently of any agent version.
    agent_runtime_version: str = "1.0.0"

    @field_validator("secret_vault_keys")
    @classmethod
    def _vault_keys(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        try:
            parse_master_keys(value.get_secret_value())
        except VaultUnavailable as error:
            # Reported as configuration invalid, the same way every other bad setting is.
            raise ValueError(str(error)) from None
        return value

    @field_validator("secret_vault_active_key")
    @classmethod
    def _vault_active_key(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
            raise ValueError("secret_vault_active_key must be a short key identifier")
        return value

    @field_validator("browser_runtime_url")
    @classmethod
    def _browser_runtime_url(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not value.startswith(("http://", "https://")) or len(value) > 500:
            raise ValueError("browser_runtime_url must be an http(s) URL")
        return value.rstrip("/")

    @field_validator("browser_runtime_token")
    @classmethod
    def _browser_runtime_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        if len(value.get_secret_value()) < 32:
            raise ValueError("browser_runtime_token must be at least 32 characters")
        return value

    @field_validator("agent_runtime_version")
    @classmethod
    def _runtime_version(cls, value: str) -> str:
        if not SEMVER.fullmatch(value):
            raise ValueError("agent_runtime_version must be a semantic version")
        return value

    @field_validator("platform_admin_subjects")
    @classmethod
    def _platform_admins(cls, value: str) -> str:
        for subject in filter(None, (part.strip() for part in value.split(","))):
            if not SUBJECT_RE.fullmatch(subject):
                raise ValueError("platform_admin_subjects must be comma-separated subjects")
        return value

    @property
    def platform_admins(self) -> frozenset[str]:
        """Verified token subjects allowed to publish and roll out standard agents."""
        return frozenset(
            subject.strip()
            for subject in self.platform_admin_subjects.split(",")
            if subject.strip()
        )

    def build_secret_vault(self) -> SecretVault:
        """The process-wide vault. Unconfigured deployments get one that refuses work."""
        keys = self.secret_vault_keys.get_secret_value() if self.secret_vault_keys else None
        if keys and not self.secret_vault_active_key:
            raise VaultUnavailable("secret_vault_active_key must be set alongside vault keys")
        return build_vault(keys, self.secret_vault_active_key)

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

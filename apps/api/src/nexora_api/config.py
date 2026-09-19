from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXORA_", extra="ignore")
    database_url: SecretStr = SecretStr("postgresql://localhost/nexora")
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    dependency_timeout_seconds: float = 2.0
    # A deployment-managed RSA public key: tokens cannot choose a key URL.
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_public_key: str | None = None

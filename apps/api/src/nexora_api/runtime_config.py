"""Operator-managed worker runtime configuration.

A run never chooses its model, provider, credentials or tool allowlist. Operators
declare model candidates and execution profiles in a file mounted next to the worker;
credentials stay in the process environment and never appear in this file. The loader
fails closed: an unreadable, malformed or internally inconsistent configuration stops
the worker from starting rather than silently granting execution.
"""

import ipaddress
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from nexora_api.executor import ExecutionProfile
from nexora_api.model_routing import ModelCandidate, ModelCapability

PROFILE_NAME = r"^[A-Za-z0-9._-]{1,64}$"
PROVIDER_NAME = r"^[a-z][a-z0-9_.-]{1,63}$"
TOOL_NAME = r"^[a-z][a-z0-9_.-]{1,63}$"
MAX_CONFIG_BYTES = 256_000


class RuntimeConfigError(ValueError):
    """Configuration a worker must refuse to start with."""


class ModelCandidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(pattern=PROVIDER_NAME)
    model: str = Field(min_length=1, max_length=128)
    capabilities: list[ModelCapability] = Field(min_length=1, max_length=8)
    quality_tier: int = Field(default=1, ge=1, le=5)
    estimated_cost_per_million_tokens: int = Field(default=0, ge=0, le=1_000_000)

    def candidate(self) -> ModelCandidate:
        return ModelCandidate(
            provider=self.provider,
            model=self.model,
            capabilities=frozenset(self.capabilities),
            quality_tier=self.quality_tier,
            estimated_cost_per_million_tokens=self.estimated_cost_per_million_tokens,
        )


class ExecutionProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # An empty allowlist would mean "nothing may execute"; that is a misconfiguration,
    # not a deployment, so it is rejected instead of started.
    allowed_workspaces: list[UUID] = Field(min_length=1, max_length=1000)
    allowed_providers: list[str] = Field(min_length=1, max_length=8)
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)
    max_steps: int = Field(default=8, ge=1, le=32)
    max_output_tokens: int = Field(default=2048, ge=1, le=16384)
    max_total_tokens: int = Field(default=32000, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=60, ge=1, le=120)
    max_context_chars: int = Field(default=100_000, ge=1000, le=200_000)

    @model_validator(mode="after")
    def _bounded_names(self):
        for provider in self.allowed_providers:
            if not re.fullmatch(PROVIDER_NAME, provider):
                raise ValueError(f"invalid provider name: {provider}")
        for tool in self.allowed_tools:
            if not re.fullmatch(TOOL_NAME, tool):
                raise ValueError(f"invalid tool name: {tool}")
        return self

    def profile(self) -> ExecutionProfile:
        return ExecutionProfile(
            allowed_workspaces=frozenset(self.allowed_workspaces),
            allowed_providers=frozenset(self.allowed_providers),
            allowed_tools=frozenset(self.allowed_tools),
            max_steps=self.max_steps,
            max_output_tokens=self.max_output_tokens,
            max_total_tokens=self.max_total_tokens,
            timeout_seconds=self.timeout_seconds,
            max_context_chars=self.max_context_chars,
        )


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["openai"] = "openai"
    model: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(default=1536, ge=1, le=4096)
    limit: int = Field(default=8, ge=1, le=50)
    batch_size: int = Field(default=128, ge=1, le=256)
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    transport: Literal["streamable_http"] = "streamable_http"
    url: str = Field(min_length=1, max_length=1000)
    bearer_token_env: str | None = Field(
        default=None,
        pattern=r"^NEXORA_MCP_[A-Z0-9_]{1,100}$",
    )
    timeout_seconds: float = Field(default=15.0, ge=0.1, le=120)
    max_response_bytes: int = Field(default=131072, ge=1024, le=1048576)

    @field_validator("url")
    @classmethod
    def _secure_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError("MCP URL must use HTTPS port 443 without credentials/query")
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or not hostname:
            raise ValueError("MCP hostname is not allowed")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return value
        if not address.is_global:
            raise ValueError("MCP literal IP must be globally routable")
        return value


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_candidates: list[ModelCandidateConfig] = Field(min_length=1, max_length=32)
    profiles: dict[str, ExecutionProfileConfig] = Field(min_length=1, max_length=16)
    retrieval: RetrievalConfig | None = None
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def _consistent(self):
        seen: set[tuple[str, str]] = set()
        for candidate in self.model_candidates:
            key = (candidate.provider, candidate.model)
            if key in seen:
                raise ValueError(
                    f"duplicate model candidate: {candidate.provider}/{candidate.model}"
                )
            seen.add(key)

        for server_key in self.mcp_servers:
            if not re.fullmatch(TOOL_NAME, server_key):
                raise ValueError(f"invalid MCP server key: {server_key}")

        configured = {candidate.provider for candidate in self.model_candidates}
        for name, profile in self.profiles.items():
            if not re.fullmatch(PROFILE_NAME, name):
                raise ValueError(f"invalid profile name: {name}")
            # A profile that can never route is a deployment error, not a runtime one.
            unknown = set(profile.allowed_providers) - configured
            if unknown:
                raise ValueError(
                    f"profile {name} allows providers without a model candidate: "
                    + ",".join(sorted(unknown))
                )
        return self

    @property
    def providers(self) -> frozenset[str]:
        return frozenset(candidate.provider for candidate in self.model_candidates)

    def candidates(self) -> list[ModelCandidate]:
        return [candidate.candidate() for candidate in self.model_candidates]

    def execution_profiles(self) -> dict[str, ExecutionProfile]:
        return {name: profile.profile() for name, profile in self.profiles.items()}

    def model_capabilities(self, provider: str) -> dict[str, frozenset[ModelCapability]]:
        """Capabilities an adapter may serve, derived from the same declared candidates."""
        return {
            candidate.model: frozenset(candidate.capabilities)
            for candidate in self.model_candidates
            if candidate.provider == provider
        }


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    """Read and validate an operator configuration file, or refuse to start."""
    location = Path(path)
    try:
        raw = location.read_bytes()
    except OSError as exc:
        raise RuntimeConfigError(f"runtime config is unreadable: {location}") from exc
    if not raw:
        raise RuntimeConfigError(f"runtime config is empty: {location}")
    if len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeConfigError(f"runtime config exceeds {MAX_CONFIG_BYTES} bytes: {location}")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeConfigError(f"runtime config is not valid JSON: {location}") from exc
    if not isinstance(document, dict):
        raise RuntimeConfigError(f"runtime config must be a JSON object: {location}")
    try:
        return RuntimeConfig.model_validate(document)
    except ValidationError as exc:
        raise RuntimeConfigError(
            f"runtime config is invalid: {exc.error_count()} problem(s)"
        ) from exc

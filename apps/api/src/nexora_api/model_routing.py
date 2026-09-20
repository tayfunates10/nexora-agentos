from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ModelCapability(StrEnum):
    TEXT = "text"
    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"


class ProviderHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ProviderError(RuntimeError):
    """Stable provider failure that is safe to expose to runtime policy."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    provider: str
    model: str
    capabilities: frozenset[ModelCapability]
    quality_tier: int = 1
    estimated_cost_per_million_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    required_capabilities: frozenset[ModelCapability]
    allowed_providers: frozenset[str]
    max_quality_tier: int | None = None
    max_cost_per_million_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    candidate: ModelCandidate
    reason: str


class ProviderAdapter(Protocol):
    """Provider boundary. Vendor SDK objects must not escape this interface."""

    async def generate(self, *, model: str, messages: list[dict[str, str]]) -> dict: ...


class ModelRouter:
    def __init__(
        self,
        candidates: list[ModelCandidate],
        provider_health: dict[str, ProviderHealth] | None = None,
    ):
        if not candidates:
            raise ValueError("at least one model candidate is required")
        self._candidates = tuple(candidates)
        self._provider_health = dict(provider_health or {})

    def provider_health(self, provider: str) -> ProviderHealth:
        return self._provider_health.get(provider, ProviderHealth.HEALTHY)

    def set_provider_health(self, provider: str, health: ProviderHealth) -> None:
        self._provider_health[provider] = health

    def route(self, request: RoutingRequest) -> RoutingDecision:
        policy_eligible = [
            candidate
            for candidate in self._candidates
            if candidate.provider in request.allowed_providers
            and request.required_capabilities.issubset(candidate.capabilities)
            and (
                request.max_quality_tier is None
                or candidate.quality_tier <= request.max_quality_tier
            )
            and (
                request.max_cost_per_million_tokens is None
                or candidate.estimated_cost_per_million_tokens
                <= request.max_cost_per_million_tokens
            )
        ]
        if not policy_eligible:
            raise ProviderError("no_compatible_model")

        eligible = [
            candidate
            for candidate in policy_eligible
            if self.provider_health(candidate.provider) != ProviderHealth.UNHEALTHY
        ]
        if not eligible:
            raise ProviderError("no_healthy_provider", retryable=True)

        # Prefer healthy providers over degraded providers. Within the same health
        # class prefer the highest permitted quality, then lower estimated cost
        # and stable names. Capability and workspace-policy constraints are never
        # relaxed during failover.
        selected = sorted(
            eligible,
            key=lambda item: (
                self.provider_health(item.provider) != ProviderHealth.HEALTHY,
                -item.quality_tier,
                item.estimated_cost_per_million_tokens,
                item.provider,
                item.model,
            ),
        )[0]
        health = self.provider_health(selected.provider)
        reason = (
            "capabilities_policy_health_quality_cost"
            if health == ProviderHealth.HEALTHY
            else "capabilities_policy_degraded_fallback_quality_cost"
        )
        return RoutingDecision(candidate=selected, reason=reason)

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ModelCapability(StrEnum):
    TEXT = "text"
    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"


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
    def __init__(self, candidates: list[ModelCandidate]):
        if not candidates:
            raise ValueError("at least one model candidate is required")
        self._candidates = tuple(candidates)

    def route(self, request: RoutingRequest) -> RoutingDecision:
        eligible = [
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
        if not eligible:
            raise ProviderError("no_compatible_model")

        # Prefer the highest permitted quality, then lower estimated cost and stable names.
        selected = sorted(
            eligible,
            key=lambda item: (
                -item.quality_tier,
                item.estimated_cost_per_million_tokens,
                item.provider,
                item.model,
            ),
        )[0]
        return RoutingDecision(
            candidate=selected,
            reason="capabilities_policy_quality_cost",
        )

import pytest

from nexora_api.model_routing import (
    ModelCandidate,
    ModelCapability,
    ModelRouter,
    ProviderError,
    RoutingRequest,
)


def candidate(provider, model, capabilities, quality=1, cost=0):
    return ModelCandidate(
        provider=provider,
        model=model,
        capabilities=frozenset(capabilities),
        quality_tier=quality,
        estimated_cost_per_million_tokens=cost,
    )


def test_router_never_silently_downgrades_required_capabilities():
    router = ModelRouter(
        [
            candidate("alpha", "text-only", [ModelCapability.TEXT], quality=3),
            candidate(
                "beta",
                "tool-model",
                [ModelCapability.TEXT, ModelCapability.TOOLS],
                quality=2,
            ),
        ]
    )

    decision = router.route(
        RoutingRequest(
            required_capabilities=frozenset([ModelCapability.TEXT, ModelCapability.TOOLS]),
            allowed_providers=frozenset(["alpha", "beta"]),
        )
    )

    assert decision.candidate.provider == "beta"
    assert decision.candidate.model == "tool-model"


def test_router_enforces_provider_allowlist_and_budget():
    router = ModelRouter(
        [
            candidate("alpha", "expensive", [ModelCapability.TEXT], quality=3, cost=40),
            candidate("beta", "affordable", [ModelCapability.TEXT], quality=2, cost=10),
        ]
    )

    decision = router.route(
        RoutingRequest(
            required_capabilities=frozenset([ModelCapability.TEXT]),
            allowed_providers=frozenset(["beta"]),
            max_cost_per_million_tokens=20,
        )
    )

    assert decision.candidate.provider == "beta"


def test_router_fails_closed_when_no_model_satisfies_policy():
    router = ModelRouter([candidate("alpha", "text-only", [ModelCapability.TEXT], quality=3)])

    with pytest.raises(ProviderError) as error:
        router.route(
            RoutingRequest(
                required_capabilities=frozenset([ModelCapability.STRUCTURED_OUTPUT]),
                allowed_providers=frozenset(["alpha"]),
            )
        )

    assert error.value.code == "no_compatible_model"
    assert error.value.retryable is False


def test_router_prefers_quality_then_cost_deterministically():
    router = ModelRouter(
        [
            candidate("beta", "b", [ModelCapability.TEXT], quality=2, cost=20),
            candidate("alpha", "a", [ModelCapability.TEXT], quality=2, cost=10),
        ]
    )

    decision = router.route(
        RoutingRequest(
            required_capabilities=frozenset([ModelCapability.TEXT]),
            allowed_providers=frozenset(["alpha", "beta"]),
        )
    )

    assert decision.candidate.provider == "alpha"
    assert decision.reason == "capabilities_policy_quality_cost"

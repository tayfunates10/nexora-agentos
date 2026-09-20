import json
from uuid import uuid4

import pytest

from nexora_api import metrics
from nexora_api.runtime_config import RuntimeConfig, RuntimeConfigError, load_runtime_config
from nexora_api.spend import (
    DEFAULT_ALERT_THRESHOLDS,
    MAX_SOURCE_KEY_LENGTH,
    BudgetEnforcement,
    BudgetInput,
    EmbeddingSpend,
    ModelPrice,
    SpendCategory,
    SpendPolicy,
    SpendPricingError,
    adhoc_retrieval_source_key,
    agent_step_source_key,
    budget_decision,
    crossed_thresholds,
    judge_case_source_key,
    knowledge_source_key,
    normalize_thresholds,
    run_retrieval_source_key,
)


def price(input_rate=3_000_000, output_rate=15_000_000):
    return ModelPrice(
        input_micros_per_million_tokens=input_rate,
        output_micros_per_million_tokens=output_rate,
    )


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "expected"),
    [
        (0, 0, 0),
        (1_000_000, 0, 3_000_000),
        (0, 1_000_000, 15_000_000),
        (1000, 500, 10_500),
        # A fraction of a price unit is charged, never silently discarded.
        (1, 0, 3),
        (1, 1, 18),
    ],
)
def test_cost_is_integer_and_rounds_up(input_tokens, output_tokens, expected):
    assert price().cost_micros(input_tokens, output_tokens) == expected


def test_free_model_costs_nothing_but_is_still_priced():
    assert price(0, 0).cost_micros(10_000, 10_000) == 0


@pytest.mark.parametrize(
    "rates",
    [(-1, 0), (0, -1), (10**13, 0), (0, 10**13), (1.5, 0), (True, 0)],
)
def test_invalid_prices_are_rejected(rates):
    with pytest.raises(ValueError):
        ModelPrice(
            input_micros_per_million_tokens=rates[0],
            output_micros_per_million_tokens=rates[1],
        )


def test_negative_usage_is_rejected():
    with pytest.raises(ValueError):
        price().cost_micros(-1, 0)


def test_unpriced_model_fails_closed():
    policy = SpendPolicy(prices={("openai", "priced-model"): price()})
    assert policy.cost_micros("openai", "priced-model", 1000, 100) == 4_500
    with pytest.raises(SpendPricingError, match="model_price_not_configured"):
        policy.cost_micros("openai", "other-model", 1000, 100)


@pytest.mark.parametrize(
    ("limit", "enforcement", "consumed", "allowed", "reason", "remaining"),
    [
        (None, None, 5_000, True, "no_limit", None),
        (10_000, BudgetEnforcement.ENFORCE, 0, True, "within_budget", 10_000),
        (10_000, BudgetEnforcement.ENFORCE, 9_999, True, "within_budget", 1),
        (10_000, BudgetEnforcement.ENFORCE, 10_000, False, "exhausted", 0),
        (10_000, BudgetEnforcement.ENFORCE, 12_000, False, "exhausted", 0),
        (10_000, BudgetEnforcement.MONITOR, 12_000, True, "monitor", 0),
        (0, BudgetEnforcement.ENFORCE, 0, False, "exhausted", 0),
    ],
)
def test_budget_decisions(limit, enforcement, consumed, allowed, reason, remaining):
    decision = budget_decision(limit, enforcement, consumed)
    assert (decision.allowed, decision.reason) == (allowed, reason)
    assert decision.remaining_micros == remaining
    assert decision.limit_micros == limit


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([80, 100], (80, 100)),
        ([100, 80], (80, 100)),
        # A duplicate would alert twice on one crossing.
        ([80, 80, 50], (50, 80)),
        ([], ()),
    ],
)
def test_thresholds_are_normalized_before_storage(values, expected):
    assert normalize_thresholds(values) == expected


@pytest.mark.parametrize("values", [[0], [101], [-5], [1, 2, 3, 4, 5, 6]])
def test_invalid_thresholds_are_rejected(values):
    with pytest.raises(ValueError):
        normalize_thresholds(values)
    with pytest.raises(ValueError):
        BudgetInput(monthly_limit_micros=1000, alert_thresholds=values)


def test_budget_input_defaults_to_warning_and_exhaustion_alerts():
    assert tuple(BudgetInput(monthly_limit_micros=1000).alert_thresholds) == (
        DEFAULT_ALERT_THRESHOLDS
    )
    assert BudgetInput(monthly_limit_micros=1000, alert_thresholds=[]).alert_thresholds == []


@pytest.mark.parametrize(
    ("limit", "consumed", "expected"),
    [
        (10_000, 0, ()),
        (10_000, 7_999, ()),
        # Exactly at the threshold counts as reached; a micro below does not.
        (10_000, 8_000, (80,)),
        (10_000, 9_999, (80,)),
        (10_000, 10_000, (80, 100)),
        (10_000, 25_000, (80, 100)),
        (None, 10_000, ()),
        # A zero limit is fully consumed from the first micro onwards.
        (0, 0, (80, 100)),
    ],
)
def test_crossed_thresholds_use_integer_comparison(limit, consumed, expected):
    assert crossed_thresholds([80, 100], limit, consumed) == expected


def test_crossing_is_exact_where_a_float_comparison_would_alert_early():
    # consumed * 100 is one micro short of 81% of this limit, but
    # consumed / limit * 100 >= 81 is True in float64.
    limit, consumed = 252_345_555_427_421, 204_399_899_896_211
    assert consumed / limit * 100 >= 81
    assert consumed * 100 == 81 * limit - 1
    assert crossed_thresholds([81], limit, consumed) == ()
    assert crossed_thresholds([81], limit, consumed + 1) == (81,)


def test_negative_consumption_is_rejected():
    with pytest.raises(ValueError):
        budget_decision(10, BudgetEnforcement.ENFORCE, -1)


def test_source_keys_are_deterministic_and_bounded():
    run_id = uuid4()
    case_id = uuid4()
    assert agent_step_source_key(run_id, 3) == agent_step_source_key(run_id, 3)
    assert agent_step_source_key(run_id, 3) != agent_step_source_key(run_id, 4)
    assert judge_case_source_key(run_id, case_id) != agent_step_source_key(run_id, 0)
    keys = (
        agent_step_source_key(run_id, 31),
        judge_case_source_key(run_id, case_id),
        run_retrieval_source_key(run_id),
        adhoc_retrieval_source_key(run_id),
        knowledge_source_key(case_id),
    )
    # A retrieval must never collide with the model steps of the same run.
    assert len(set(keys)) == len(keys)
    for key in keys:
        assert 1 <= len(key) <= MAX_SOURCE_KEY_LENGTH


def test_embedding_spend_charges_input_tokens_only():
    spend = EmbeddingSpend(
        provider="openai",
        model="embed-test",
        price=ModelPrice(
            input_micros_per_million_tokens=20_000,
            output_micros_per_million_tokens=0,
        ),
    )
    assert spend.cost_micros(1_000_000) == 20_000
    # Rounding up keeps a small embedding from costing a recorded zero.
    assert spend.cost_micros(1) == 1
    assert spend.cost_micros(0) == 0


def runtime_document(retrieval=None, **candidate):
    document = {
        "model_candidates": [
            {
                "provider": "openai",
                "model": "priced-model",
                "capabilities": ["text", "structured_output"],
                **candidate,
            }
        ],
        "profiles": {
            "default": {
                "allowed_workspaces": [str(uuid4())],
                "allowed_providers": ["openai"],
            }
        },
    }
    if retrieval is not None:
        document["retrieval"] = {"provider": "openai", "model": "embed-test", **retrieval}
    return document


PRICED = {
    "input_micros_per_million_tokens": 3_000_000,
    "output_micros_per_million_tokens": 15_000_000,
}


def test_embedding_price_is_required_exactly_when_models_are_priced():
    # Unpriced deployment: retrieval stays unpriced too.
    config = RuntimeConfig.model_validate(runtime_document(retrieval={}))
    assert config.embedding_spend() is None

    priced = RuntimeConfig.model_validate(
        runtime_document(retrieval={"input_micros_per_million_tokens": 20_000}, **PRICED)
    )
    spend = priced.embedding_spend()
    assert spend is not None
    assert (spend.provider, spend.model) == ("openai", "embed-test")
    assert spend.cost_micros(500_000) == 10_000

    # Embeddings are provider egress; leaving them unpriced would exempt them.
    with pytest.raises(ValueError, match="retrieval embeddings must be priced"):
        RuntimeConfig.model_validate(runtime_document(retrieval={}, **PRICED))
    with pytest.raises(ValueError, match="retrieval embeddings must be priced"):
        RuntimeConfig.model_validate(
            runtime_document(retrieval={"input_micros_per_million_tokens": 20_000})
        )


def test_a_deployment_without_retrieval_needs_no_embedding_price():
    assert RuntimeConfig.model_validate(runtime_document(**PRICED)).embedding_spend() is None


def test_runtime_config_without_prices_disables_accounting():
    config = RuntimeConfig.model_validate(runtime_document())
    assert config.spend_policy() is None


def test_runtime_config_prices_build_a_policy():
    config = RuntimeConfig.model_validate(
        runtime_document(
            input_micros_per_million_tokens=3_000_000,
            output_micros_per_million_tokens=15_000_000,
        )
    )
    policy = config.spend_policy()
    assert policy is not None
    assert policy.cost_micros("openai", "priced-model", 1000, 500) == 10_500


def test_half_declared_price_is_rejected():
    with pytest.raises(ValueError, match="both input and output rates"):
        RuntimeConfig.model_validate(runtime_document(input_micros_per_million_tokens=3_000_000))


def test_partially_priced_candidates_are_rejected(tmp_path):
    document = runtime_document(
        input_micros_per_million_tokens=3_000_000,
        output_micros_per_million_tokens=15_000_000,
    )
    document["model_candidates"].append(
        {
            "provider": "openai",
            "model": "unpriced-model",
            "capabilities": ["text"],
        }
    )
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(document))
    with pytest.raises(RuntimeConfigError, match="invalid"):
        load_runtime_config(path)
    with pytest.raises(ValueError, match="every candidate or for none"):
        RuntimeConfig.model_validate(document)


def sample(name, **labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def test_spend_metrics_stay_bounded():
    before = sample("nexora_model_cost_micros_total", provider="openai", category="agent_run")
    metrics.observe_spend("openai", SpendCategory.AGENT_RUN, 250)
    metrics.observe_spend("openai", SpendCategory.AGENT_RUN, 0)
    assert (
        sample("nexora_model_cost_micros_total", provider="openai", category="agent_run")
        == before + 250
    )

    denied_before = sample("nexora_spend_denials_total", category="evaluation_judge")
    metrics.observe_spend_denied(SpendCategory.EVALUATION_JUDGE)
    assert sample("nexora_spend_denials_total", category="evaluation_judge") == denied_before + 1

    # Unknown providers and categories must not create unbounded label series.
    metrics.observe_spend("Provider Name With Spaces", "surprise_category", 5)
    assert sample("nexora_model_cost_micros_total", provider="other", category="other") == 5

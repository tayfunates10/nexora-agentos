import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_health import StubProbe

from nexora_api import metrics
from nexora_api.config import Settings
from nexora_api.main import create_app

TOKEN = "scrape-token-that-is-long-enough-01"


def sample(name, **labels):
    return metrics.REGISTRY.get_sample_value(name, labels or None) or 0.0


@pytest.fixture
def client():
    settings = Settings(metrics_token=TOKEN)
    with TestClient(create_app(settings=settings, probe=StubProbe())) as test_client:
        yield test_client


def test_scrape_requires_the_operator_token(client):
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"authorization": "Bearer wrong"}).status_code == 401

    response = client.get("/metrics", headers={"authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "nexora_http_requests_total" in response.text


def test_scrape_endpoint_is_absent_without_a_token():
    with TestClient(create_app(settings=Settings(), probe=StubProbe())) as unconfigured:
        assert unconfigured.get("/metrics").status_code == 404


def test_scrape_token_must_not_be_weak():
    with pytest.raises(ValidationError):
        Settings(metrics_token="short")


def test_requests_are_counted_by_route_template(client):
    before = sample(
        "nexora_http_requests_total",
        method="GET",
        route="/api/v1/health/live",
        status="200",
    )

    client.get("/api/v1/health/live")

    assert (
        sample(
            "nexora_http_requests_total",
            method="GET",
            route="/api/v1/health/live",
            status="200",
        )
        == before + 1
    )
    assert sample(
        "nexora_http_request_duration_seconds_count",
        method="GET",
        route="/api/v1/health/live",
    )


def test_unmatched_paths_cannot_inflate_label_cardinality(client):
    before = sample("nexora_http_requests_total", method="GET", route="unmatched", status="404")

    for suffix in range(3):
        client.get(f"/does-not-exist/{suffix}")

    assert (
        sample("nexora_http_requests_total", method="GET", route="unmatched", status="404")
        == before + 3
    )


def test_scrape_traffic_is_not_self_reported(client):
    headers = {"authorization": f"Bearer {TOKEN}"}
    before = sample("nexora_http_requests_total", method="GET", route="/metrics", status="200")

    client.get("/metrics", headers=headers)

    assert sample("nexora_http_requests_total", method="GET", route="/metrics", status="200") == (
        before
    )


@pytest.mark.parametrize(
    "value,expected",
    [("openai", "openai"), ("Bad Key", "other"), ("", "unknown"), ("x" * 80, "other")],
)
def test_operator_labels_stay_bounded(value, expected):
    assert metrics.label(value) == expected


def test_unknown_outcomes_collapse_instead_of_creating_series():
    before = sample("nexora_agent_runs_total", outcome="other")

    metrics.observe_run("something-new", 0.5)

    assert sample("nexora_agent_runs_total", outcome="other") == before + 1


def test_model_usage_is_recorded_per_provider():
    before = sample("nexora_model_tokens_total", provider="test", kind="output")

    metrics.observe_model_call("test", "success", 0.2, input_tokens=10, output_tokens=4)

    assert sample("nexora_model_tokens_total", provider="test", kind="output") == before + 4
    assert sample("nexora_model_calls_total", provider="test", outcome="success")


def test_evaluation_judge_metrics_are_bounded_and_track_tokens():
    calls_before = sample(
        "nexora_evaluation_judge_calls_total",
        provider="test",
        target="candidate",
        outcome="success",
    )
    tokens_before = sample(
        "nexora_evaluation_judge_tokens_total",
        provider="test",
        target="candidate",
        kind="input",
    )
    jobs_before = sample("nexora_evaluation_judge_jobs_total", outcome="succeeded")

    metrics.observe_evaluation_judge_call(
        "test", "candidate", "success", 0.25, input_tokens=12, output_tokens=5
    )
    metrics.observe_evaluation_judge_job("succeeded")

    assert (
        sample(
            "nexora_evaluation_judge_calls_total",
            provider="test",
            target="candidate",
            outcome="success",
        )
        == calls_before + 1
    )
    assert (
        sample(
            "nexora_evaluation_judge_tokens_total",
            provider="test",
            target="candidate",
            kind="input",
        )
        == tokens_before + 12
    )
    assert sample("nexora_evaluation_judge_jobs_total", outcome="succeeded") == jobs_before + 1
    assert (
        sample(
            "nexora_evaluation_judge_call_duration_seconds_count",
            provider="test",
            target="candidate",
        )
        >= 1
    )


def test_evaluation_judge_unknown_labels_collapse():
    before = sample(
        "nexora_evaluation_judge_calls_total",
        provider="other",
        target="other",
        outcome="other",
    )

    metrics.observe_evaluation_judge_call(
        "Bad Provider", "unexpected-target", "unexpected-outcome", 0.1
    )

    assert (
        sample(
            "nexora_evaluation_judge_calls_total",
            provider="other",
            target="other",
            outcome="other",
        )
        == before + 1
    )


def test_approval_wait_is_only_observed_for_real_waits():
    before = sample("nexora_approval_wait_seconds_count", decision="approved")

    metrics.observe_approval("approved", 42.0)
    metrics.observe_approval("approved", -1.0)

    assert sample("nexora_approval_wait_seconds_count", decision="approved") == before + 1
    assert sample("nexora_approval_decisions_total", decision="approved") >= 2


def test_malformed_scrape_credentials_are_rejected_without_error(client):
    # Headers arrive as latin-1 bytes, so a non-ASCII credential must fail, not raise.
    response = client.get("/metrics", headers={"authorization": "Bearer tökén".encode("latin-1")})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_declared_objectives_are_exported_for_alert_rules(client):
    response = client.get("/metrics", headers={"authorization": f"Bearer {TOKEN}"})

    assert sample("nexora_slo_objective_ratio", slo="api_availability") == 0.999
    assert sample("nexora_slo_objective_ratio", slo="agent_run_reliability") == 0.99
    assert sample("nexora_slo_window_days", slo="api_availability") == 30
    assert 'nexora_slo_objective_ratio{slo="api_availability"}' in response.text


def test_error_budget_follows_the_objective():
    assert metrics.API_AVAILABILITY.error_budget == pytest.approx(0.001)
    assert metrics.AGENT_RUN_RELIABILITY.error_budget == pytest.approx(0.01)
    assert {objective.window_days for objective in metrics.SERVICE_LEVEL_OBJECTIVES} == {30}


@pytest.mark.parametrize(
    "name,objective,window",
    [
        ("api_availability", 1.0, 30),
        ("api_availability", 0.4, 30),
        ("api_availability", 0.99, 0),
        ("api_availability", 0.99, 365),
        ("API Availability", 0.99, 30),
    ],
)
def test_unmeasurable_objectives_are_rejected(name, objective, window):
    with pytest.raises(ValueError):
        metrics.ServiceLevelObjective(name, objective, window)

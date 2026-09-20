"""Service level objectives and Prometheus instruments for API, worker, model,
retrieval and tool activity.

Label values stay bounded on purpose. Tenant, user, run and workspace identifiers are
high-cardinality and belong on spans, never on metrics. Operator-controlled values
(provider, server_key) are length- and charset-bounded; anything else becomes "other".
"""

import hmac
import re
from dataclasses import dataclass
from typing import Literal

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

_LABEL = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_RUN_OUTCOMES = frozenset(
    {"succeeded", "failed_retryable", "failed_terminal", "waiting_for_approval", "cancelled"}
)
_CALL_OUTCOMES = frozenset(
    {"success", "error", "timeout", "cancelled", "denied", "replayed", "approval"}
)
_DECISIONS = frozenset({"approved", "rejected", "expired", "cancelled"})
_JUDGE_TARGETS = frozenset({"candidate", "baseline"})
_JUDGE_CALL_OUTCOMES = frozenset({"success", "provider_error", "timeout", "invalid_response"})
_JUDGE_JOB_OUTCOMES = frozenset({"succeeded", "failed"})

_LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_RUN_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0)
_WAIT_BUCKETS = (1.0, 5.0, 30.0, 60.0, 300.0, 900.0, 3600.0, 21600.0, 86400.0)


@dataclass(frozen=True)
class ServiceLevelObjective:
    """A user-facing target, declared before dashboards so alerts measure a stated goal."""

    name: str
    objective: float
    window_days: int

    def __post_init__(self):
        if not _LABEL.fullmatch(self.name):
            raise ValueError("slo name must be a bounded label value")
        if not 0.5 <= self.objective < 1.0:
            raise ValueError("objective must be at least 0.5 and below 1.0")
        if not 1 <= self.window_days <= 90:
            raise ValueError("window_days must be between 1 and 90")

    @property
    def error_budget(self) -> float:
        return 1.0 - self.objective


# Initial engineering targets. Production traffic and an error-budget policy must come
# before any of these becomes a contractual guarantee.
API_AVAILABILITY = ServiceLevelObjective("api_availability", 0.999, 30)
AGENT_RUN_RELIABILITY = ServiceLevelObjective("agent_run_reliability", 0.99, 30)
SERVICE_LEVEL_OBJECTIVES = (API_AVAILABILITY, AGENT_RUN_RELIABILITY)


slo_objective_ratio = Gauge(
    "nexora_slo_objective_ratio",
    "Declared service level objective as a success ratio.",
    ("slo",),
    registry=REGISTRY,
)
slo_window_days = Gauge(
    "nexora_slo_window_days",
    "Rolling evaluation window of a declared service level objective.",
    ("slo",),
    registry=REGISTRY,
)
http_requests_total = Counter(
    "nexora_http_requests_total",
    "HTTP requests served by the API.",
    ("method", "route", "status"),
    registry=REGISTRY,
)
http_request_duration_seconds = Histogram(
    "nexora_http_request_duration_seconds",
    "API request latency in seconds.",
    ("method", "route"),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
agent_runs_total = Counter(
    "nexora_agent_runs_total",
    "Agent run executions finalized by a worker.",
    ("outcome",),
    registry=REGISTRY,
)
agent_run_duration_seconds = Histogram(
    "nexora_agent_run_duration_seconds",
    "Worker execution time per claimed agent run attempt.",
    ("outcome",),
    buckets=_RUN_BUCKETS,
    registry=REGISTRY,
)
model_calls_total = Counter(
    "nexora_model_calls_total",
    "Model generation calls per provider.",
    ("provider", "outcome"),
    registry=REGISTRY,
)
model_call_duration_seconds = Histogram(
    "nexora_model_call_duration_seconds",
    "Model generation latency in seconds.",
    ("provider",),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
model_tokens_total = Counter(
    "nexora_model_tokens_total",
    "Tokens reported by providers.",
    ("provider", "kind"),
    registry=REGISTRY,
)
tool_calls_total = Counter(
    "nexora_tool_calls_total",
    "Governed tool calls by MCP server key.",
    ("server_key", "outcome"),
    registry=REGISTRY,
)
tool_call_duration_seconds = Histogram(
    "nexora_tool_call_duration_seconds",
    "Governed tool execution latency in seconds.",
    ("server_key",),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
retrieval_queries_total = Counter(
    "nexora_retrieval_queries_total",
    "Permission-filtered retrieval queries.",
    ("outcome",),
    registry=REGISTRY,
)
retrieval_duration_seconds = Histogram(
    "nexora_retrieval_duration_seconds",
    "Retrieval latency in seconds.",
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
approval_decisions_total = Counter(
    "nexora_approval_decisions_total",
    "Human approval outcomes for governed tool calls.",
    ("decision",),
    registry=REGISTRY,
)
approval_wait_seconds = Histogram(
    "nexora_approval_wait_seconds",
    "Time between an approval request and its decision.",
    ("decision",),
    buckets=_WAIT_BUCKETS,
    registry=REGISTRY,
)
queue_depth = Gauge(
    "nexora_queue_depth",
    "Pending entries in the agent run job stream.",
    registry=REGISTRY,
)
outbox_published_total = Counter(
    "nexora_outbox_published_total",
    "Outbox jobs published to the job stream.",
    registry=REGISTRY,
)
evaluation_judge_calls_total = Counter(
    "nexora_evaluation_judge_calls_total",
    "Pinned evaluation judge model calls.",
    ("provider", "target", "outcome"),
    registry=REGISTRY,
)
evaluation_judge_call_duration_seconds = Histogram(
    "nexora_evaluation_judge_call_duration_seconds",
    "Pinned evaluation judge model-call latency in seconds.",
    ("provider", "target"),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
evaluation_judge_tokens_total = Counter(
    "nexora_evaluation_judge_tokens_total",
    "Tokens reported by evaluation judge model calls.",
    ("provider", "target", "kind"),
    registry=REGISTRY,
)
evaluation_judge_jobs_total = Counter(
    "nexora_evaluation_judge_jobs_total",
    "Evaluation judge jobs reaching a terminal state.",
    ("outcome",),
    registry=REGISTRY,
)


def label(value: str | None) -> str:
    """Bound an operator-controlled label value; unknown shapes collapse to 'other'."""
    if not value:
        return "unknown"
    return value if _LABEL.fullmatch(value) else "other"


def method_label(value: str | None) -> str:
    return value if value in _HTTP_METHODS else "other"


def route_label(value: str | None) -> str:
    """Only matched route templates are recorded; raw paths would be unbounded."""
    if not value or len(value) > 200:
        return "unmatched"
    return value


def _bounded(value: str | None, allowed: frozenset[str]) -> str:
    return value if value in allowed else "other"


def observe_http(method: str, route: str | None, status: int, seconds: float) -> None:
    method_value = method_label(method)
    route_value = route_label(route)
    http_requests_total.labels(method_value, route_value, str(status)).inc()
    http_request_duration_seconds.labels(method_value, route_value).observe(seconds)


def observe_run(outcome: str, seconds: float) -> None:
    value = _bounded(outcome, _RUN_OUTCOMES)
    agent_runs_total.labels(value).inc()
    agent_run_duration_seconds.labels(value).observe(seconds)


def observe_model_call(
    provider: str, outcome: str, seconds: float, input_tokens: int = 0, output_tokens: int = 0
) -> None:
    value = label(provider)
    model_calls_total.labels(value, _bounded(outcome, _CALL_OUTCOMES)).inc()
    model_call_duration_seconds.labels(value).observe(seconds)
    if input_tokens > 0:
        model_tokens_total.labels(value, "input").inc(input_tokens)
    if output_tokens > 0:
        model_tokens_total.labels(value, "output").inc(output_tokens)


def observe_evaluation_judge_call(
    provider: str,
    target: str,
    outcome: str,
    seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    provider_value = label(provider)
    target_value = _bounded(target, _JUDGE_TARGETS)
    outcome_value = _bounded(outcome, _JUDGE_CALL_OUTCOMES)
    evaluation_judge_calls_total.labels(provider_value, target_value, outcome_value).inc()
    evaluation_judge_call_duration_seconds.labels(provider_value, target_value).observe(seconds)
    if input_tokens > 0:
        evaluation_judge_tokens_total.labels(
            provider_value, target_value, "input"
        ).inc(input_tokens)
    if output_tokens > 0:
        evaluation_judge_tokens_total.labels(
            provider_value, target_value, "output"
        ).inc(output_tokens)


def observe_evaluation_judge_job(outcome: str) -> None:
    evaluation_judge_jobs_total.labels(_bounded(outcome, _JUDGE_JOB_OUTCOMES)).inc()


def observe_tool_call(server_key: str | None, outcome: str, seconds: float | None = None) -> None:
    value = label(server_key)
    tool_calls_total.labels(value, _bounded(outcome, _CALL_OUTCOMES)).inc()
    if seconds is not None:
        tool_call_duration_seconds.labels(value).observe(seconds)


def observe_retrieval(outcome: str, seconds: float) -> None:
    retrieval_queries_total.labels(_bounded(outcome, _CALL_OUTCOMES)).inc()
    retrieval_duration_seconds.observe(seconds)


def observe_approval(decision: str, waited_seconds: float | None) -> None:
    value = _bounded(decision, _DECISIONS)
    approval_decisions_total.labels(value).inc()
    if waited_seconds is not None and waited_seconds >= 0:
        approval_wait_seconds.labels(value).observe(waited_seconds)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def authorize_scrape(presented: str, token) -> Literal["disabled", "unauthorized", "allowed"]:
    """Gate the scrape endpoint. No configured token means the endpoint does not exist.

    Credentials are compared as bytes so a malformed header fails instead of raising.
    """
    if token is None:
        return "disabled"
    expected = f"Bearer {token.get_secret_value()}".encode()
    if not hmac.compare_digest(presented.encode("utf-8", "replace"), expected):
        return "unauthorized"
    return "allowed"


def _publish_objectives() -> None:
    """Export the targets so alert rules read them instead of hardcoding a number."""
    for objective in SERVICE_LEVEL_OBJECTIVES:
        slo_objective_ratio.labels(objective.name).set(objective.objective)
        slo_window_days.labels(objective.name).set(objective.window_days)


_publish_objectives()

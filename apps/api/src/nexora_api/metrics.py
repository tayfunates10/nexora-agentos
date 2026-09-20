"""Prometheus instruments for API, worker, model, retrieval and tool activity.

Label values stay bounded on purpose. Tenant, user, run and workspace identifiers are
high-cardinality and belong on spans, never on metrics. Operator-controlled values
(provider, server_key) are length- and charset-bounded; anything else becomes "other".
"""

import re

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

_LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_RUN_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0)
_WAIT_BUCKETS = (1.0, 5.0, 30.0, 60.0, 300.0, 900.0, 3600.0, 21600.0, 86400.0)

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

"""Trace identity, sampling and exporter configuration.

Run traces are durable: the ``agent_runs.trace_id`` UUID *is* the OpenTelemetry trace
id. API, worker, model and tool spans therefore join a single trace without smuggling
trace context through the queue, and an at-least-once redelivery keeps the trace while
``nexora.attempt`` separates the attempts. Sampling is derived from the same id, so
every process reaches the same decision for a run.

Span attributes are allowlisted. Prompts, tool arguments, tool results, retrieved
context and credentials are never recorded as telemetry.
"""

import hashlib
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
)

from nexora_api.config import Settings

TRACER_NAME = "nexora.agentos"
MAX_ATTRIBUTE_CHARS = 200

# Telemetry carries identifiers, counts and outcome codes only. Anything not listed
# here is dropped before it can reach an exporter.
ALLOWED_ATTRIBUTES = frozenset(
    {
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "nexora.agent_id",
        "nexora.approval_id",
        "nexora.attempt",
        "nexora.citation_count",
        "nexora.error_code",
        "nexora.finish_reason",
        "nexora.input_tokens",
        "nexora.job_id",
        "nexora.model",
        "nexora.outcome",
        "nexora.output_tokens",
        "nexora.policy_action",
        "nexora.provider",
        "nexora.queue_depth",
        "nexora.request_id",
        "nexora.retryable",
        "nexora.routing_reason",
        "nexora.run_id",
        "nexora.server_key",
        "nexora.side_effect",
        "nexora.step",
        "nexora.tool_call_key",
        "nexora.tool_name",
        "nexora.worker_id",
        "nexora.workspace_id",
    }
)

_provider: TracerProvider | None = None
_exporting = False


def safe_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Keep allowlisted, bounded, primitive attributes and drop everything else."""
    safe: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key not in ALLOWED_ATTRIBUTES or value is None:
            continue
        if isinstance(value, bool | int | float):
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = value[:MAX_ATTRIBUTE_CHARS]
        elif isinstance(value, UUID):
            safe[key] = str(value)
    return safe


def is_sampled(trace_id: int, ratio: float) -> bool:
    """Deterministic head sampling so separate processes agree on one run's trace.

    Run trace ids are UUIDs whose version and variant bits are fixed, so the raw id
    is not uniformly distributed. Sampling therefore hashes the id instead of masking
    it, which the standard ratio sampler can assume for randomly generated trace ids.
    """
    if ratio >= 1.0:
        return True
    if ratio <= 0.0:
        return False
    digest = hashlib.sha256(trace_id.to_bytes(16, "big")).digest()[:8]
    return int.from_bytes(digest, "big") < round(ratio * (1 << 64))


def durable_trace_context(trace_id: UUID, run_id: UUID, ratio: float = 1.0) -> Context | None:
    """Build the remote parent identified by a run's durable trace id."""
    raw_trace = int.from_bytes(trace_id.bytes, "big")
    raw_span = int.from_bytes(hashlib.sha256(run_id.bytes).digest()[:8], "big")
    if raw_trace == 0 or raw_span == 0:
        return None
    flags = TraceFlags.SAMPLED if is_sampled(raw_trace, ratio) else TraceFlags.DEFAULT
    span_context = SpanContext(
        trace_id=raw_trace,
        span_id=raw_span,
        is_remote=True,
        trace_flags=TraceFlags(flags),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context))


def configure_telemetry(settings: Settings) -> TracerProvider:
    """Install the tracer provider once. No endpoint configured means no egress."""
    global _provider, _exporting
    if _provider is not None:
        return _provider
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(root=TraceIdRatioBased(settings.otel_sample_ratio)),
    )
    if settings.otel_exporter_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.otel_exporter_endpoint,
                    timeout=int(settings.otel_export_timeout_seconds),
                )
            )
        )
        _exporting = True
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def exporter_configured() -> bool:
    """True only when an operator configured a collector endpoint."""
    return _exporting


def shutdown_telemetry() -> None:
    global _provider, _exporting
    if _provider is not None:
        _provider.shutdown()
        _provider = None
        _exporting = False


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


@contextmanager
def span(
    name: str,
    context: Context | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    **attributes: Any,
):
    """Start a span with allowlisted attributes and error-aware status."""
    with tracer().start_as_current_span(
        name, context=context, kind=kind, attributes=safe_attributes(attributes)
    ) as active:
        try:
            yield active
        except Exception as exc:
            active.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def record(active_span: trace.Span, **attributes: Any) -> None:
    for key, value in safe_attributes(attributes).items():
        active_span.set_attribute(key, value)


def record_error(active_span: trace.Span, code: str) -> None:
    active_span.set_status(Status(StatusCode.ERROR, code[:MAX_ATTRIBUTE_CHARS]))
    record(active_span, **{"nexora.error_code": code})


def current_ids() -> tuple[str | None, str | None]:
    """Return (trace_id, span_id) hex for log correlation."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")

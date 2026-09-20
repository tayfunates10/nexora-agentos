import hashlib
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import StatusCode, TraceFlags
from pydantic import ValidationError

from nexora_api import telemetry
from nexora_api.config import Settings


@pytest.fixture
def exported(monkeypatch):
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "tracer", lambda: provider.get_tracer("test"))
    return exporter


def test_run_trace_id_is_the_durable_run_identifier(exported):
    trace_id, run_id = uuid4(), uuid4()

    context = telemetry.durable_trace_context(trace_id, run_id)
    with telemetry.span("agent.run", context=context, **{"nexora.run_id": run_id}):
        pass

    span = exported.get_finished_spans()[0]
    expected_parent = int.from_bytes(hashlib.sha256(run_id.bytes).digest()[:8], "big")
    assert span.context.trace_id == int.from_bytes(trace_id.bytes, "big")
    assert span.parent.trace_id == span.context.trace_id
    assert span.parent.span_id == expected_parent
    assert span.attributes["nexora.run_id"] == str(run_id)


def test_retried_attempts_stay_in_one_trace(exported):
    trace_id, run_id = uuid4(), uuid4()

    for attempt in (1, 2):
        context = telemetry.durable_trace_context(trace_id, run_id)
        with telemetry.span("agent.run", context=context, **{"nexora.attempt": attempt}):
            pass

    first, second = exported.get_finished_spans()
    assert first.context.trace_id == second.context.trace_id
    assert first.parent.span_id == second.parent.span_id
    assert first.context.span_id != second.context.span_id
    assert (first.attributes["nexora.attempt"], second.attributes["nexora.attempt"]) == (1, 2)


def test_unusable_identifiers_produce_no_parent_context():
    zero = UUID(int=0)
    assert telemetry.durable_trace_context(zero, uuid4()) is None
    assert telemetry.durable_trace_context(uuid4(), uuid4()) is not None


@pytest.mark.parametrize("ratio,expected", [(1.0, True), (0.0, False)])
def test_sampling_is_deterministic_per_trace(ratio, expected):
    trace_id = uuid4()
    raw = int.from_bytes(trace_id.bytes, "big")

    assert telemetry.is_sampled(raw, ratio) is expected
    assert telemetry.is_sampled(raw, ratio) is telemetry.is_sampled(raw, ratio)
    context = telemetry.durable_trace_context(trace_id, uuid4(), ratio)
    flags = list(context.values())[0].get_span_context().trace_flags
    assert bool(flags & TraceFlags.SAMPLED) is expected


def test_sampling_ratio_selects_a_bounded_subset():
    ids = [int.from_bytes(uuid4().bytes, "big") for _ in range(2000)]
    sampled = sum(telemetry.is_sampled(value, 0.25) for value in ids)
    assert 0 < sampled < len(ids)


def test_span_attributes_are_allowlisted_and_bounded(exported):
    with telemetry.span(
        "model.generate",
        **{
            "nexora.provider": "openai",
            "nexora.input_tokens": 12,
            "nexora.model": "m" * 500,
            "prompt": "secret user prompt",
            "nexora.unknown": "dropped",
        },
    ):
        pass

    attributes = exported.get_finished_spans()[0].attributes
    assert attributes["nexora.provider"] == "openai"
    assert attributes["nexora.input_tokens"] == 12
    assert len(attributes["nexora.model"]) == telemetry.MAX_ATTRIBUTE_CHARS
    assert "prompt" not in attributes
    assert "nexora.unknown" not in attributes


def test_failures_are_recorded_and_reraised(exported):
    with pytest.raises(ValueError):
        with telemetry.span("tool.invoke") as active:
            telemetry.record_error(active, "mcp_timeout")
            raise ValueError("boom")

    span = exported.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["nexora.error_code"] == "mcp_timeout"


def test_current_ids_are_absent_without_an_active_span():
    assert telemetry.current_ids() == (None, None)


def test_configuration_is_idempotent_and_does_not_export_by_default():
    settings = Settings()

    assert settings.otel_exporter_endpoint is None
    assert telemetry.configure_telemetry(settings) is telemetry.configure_telemetry(settings)
    assert telemetry.exporter_configured() is False


def test_collector_endpoint_must_be_an_http_url():
    with pytest.raises(ValidationError):
        Settings(otel_exporter_endpoint="collector:4318")
    assert Settings(otel_exporter_endpoint="https://collector:4318/v1/traces")

import json
import logging

from nexora_api import logs
from nexora_api.config import Settings


def record(**extra):
    logger = logging.LogRecord("nexora.test", logging.INFO, __file__, 1, "request", None, None)
    for key, value in extra.items():
        setattr(logger, key, value)
    return json.loads(logs.JsonFormatter("nexora-api").format(logger))


def test_logs_carry_correlation_context():
    payload = record(**logs.context(request_id="req-1", route="/api/v1/me", status=200))

    assert payload["request_id"] == "req-1"
    assert payload["route"] == "/api/v1/me"
    assert payload["status"] == 200
    assert payload["service"] == "nexora-api"
    assert payload["level"] == "INFO"


def test_context_drops_unknown_fields():
    assert logs.context(prompt="secret", api_key="sk-live", run_id=None) == {}
    assert "prompt" not in record(prompt="secret")


def test_context_values_are_bounded():
    bounded = logs.context(tool_name="t" * 500)

    assert len(bounded["tool_name"]) == logs.MAX_VALUE_CHARS


def test_configure_logging_installs_exactly_one_json_handler():
    logger = logs.configure_logging(Settings(log_level="warning"))
    logs.configure_logging(Settings(log_level="WARNING"))

    assert len(logger.handlers) == 1
    assert logger.level == logging.WARNING
    assert not logger.propagate

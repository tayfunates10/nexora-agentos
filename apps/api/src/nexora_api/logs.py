"""Structured JSON logging with correlation identifiers.

Only allowlisted context fields are serialized. Prompts, tool arguments, tool results,
tokens and credentials must never be passed to the logger; unknown extras are dropped
rather than emitted, so a careless call site cannot leak tenant content into log sinks.
"""

import json
import logging
import sys
from typing import Any

from nexora_api.config import Settings
from nexora_api.telemetry import current_ids

MAX_MESSAGE_CHARS = 1000
MAX_VALUE_CHARS = 200

LOG_CONTEXT_FIELDS = frozenset(
    {
        "agent_id",
        "approval_id",
        "attempt",
        "duration_ms",
        "error_code",
        "job_id",
        "method",
        "outcome",
        "provider",
        "request_id",
        "route",
        "run_id",
        "server_key",
        "status",
        "step",
        "tool_name",
        "worker_id",
        "workspace_id",
    }
)

_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)))


def context(**fields: Any) -> dict[str, Any]:
    """Build a bounded ``extra`` mapping for a log call."""
    bounded: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in LOG_CONTEXT_FIELDS or value is None or key in _RESERVED:
            continue
        bounded[key] = (
            value if isinstance(value, bool | int | float) else str(value)[:MAX_VALUE_CHARS]
        )
    return bounded


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = current_ids()
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service_name,
            "message": record.getMessage()[:MAX_MESSAGE_CHARS],
        }
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id
        for key in LOG_CONTEXT_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = (
                    value if isinstance(value, bool | int | float) else str(value)[:MAX_VALUE_CHARS]
                )
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__ if record.exc_info[0] else "error"
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(settings: Settings) -> logging.Logger:
    """Install a single JSON stdout handler for the Nexora logger tree."""
    logger = logging.getLogger("nexora")
    formatter = JsonFormatter(settings.service_name)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.setLevel(settings.log_level)
    logger.propagate = False
    return logger


def logger(name: str) -> logging.Logger:
    return logging.getLogger(f"nexora.{name}")

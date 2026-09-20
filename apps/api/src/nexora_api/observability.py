from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic


SAFE_ATTRIBUTE_KEYS = frozenset(
    {"request_id", "trace_id", "workspace_id", "run_id", "agent_id", "operation", "status"}
)


def safe_attributes(values: Mapping[str, str | None]) -> dict[str, str]:
    """Allowlist correlation metadata; never accept prompts, tokens, credentials or tool args."""
    return {key: value for key, value in values.items() if key in SAFE_ATTRIBUTE_KEYS and value}


@dataclass(frozen=True)
class SloTarget:
    name: str
    objective: float
    window_days: int


API_AVAILABILITY_SLO = SloTarget("api_availability", 0.999, 30)
AGENT_RUN_RELIABILITY_SLO = SloTarget("agent_run_reliability", 0.99, 30)


class OperationTimer:
    """Small exporter-independent latency primitive for HTTP/worker/provider instrumentation."""

    def __init__(self) -> None:
        self._started = monotonic()

    def elapsed_seconds(self) -> float:
        return max(0.0, monotonic() - self._started)

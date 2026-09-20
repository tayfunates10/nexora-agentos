import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from nexora_api.spend_alert_worker import (
    EVENT_TYPE,
    MAX_DELIVERY_ATTEMPTS,
    SpendAlertWebhook,
    alert_payload,
    delivery_retry_delay,
    signature,
)

SECRET = "a" * 32


def webhook(**overrides):
    return SpendAlertWebhook(
        **{
            "url": "https://alerts.example.test/hooks/spend",
            "signing_secret": SECRET,
            **overrides,
        }
    )


def test_operator_endpoint_accepts_only_public_https():
    endpoint = webhook(bearer_token="token", timeout_seconds=5)
    assert endpoint.url == "https://alerts.example.test/hooks/spend"
    # A globally routable literal is allowed; it is parsed, never contacted here.
    assert (
        SpendAlertWebhook(url="https://8.8.8.8/hooks", signing_secret=SECRET).timeout_seconds
        == 10.0
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://alerts.example.test/hooks",
        "https://localhost/hooks",
        "https://127.0.0.1/hooks",
        "https://10.0.0.5/hooks",
        "https://203.0.113.10/hooks",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/hooks",
        "https://user:pass@alerts.example.test/hooks",
        "https://alerts.example.test:8443/hooks",
        "https://alerts.example.test/hooks?token=secret",
        "https://alerts.example.test/hooks#fragment",
        "file:///etc/passwd",
    ],
)
def test_unsafe_endpoints_are_refused(url):
    with pytest.raises(ValueError):
        SpendAlertWebhook(url=url, signing_secret=SECRET)


@pytest.mark.parametrize(
    ("secret", "timeout"),
    [("short", 10.0), ("", 10.0), (SECRET, 0.0), (SECRET, 120.0)],
)
def test_weak_signing_or_unbounded_timeouts_are_refused(secret, timeout):
    with pytest.raises(ValueError):
        SpendAlertWebhook(
            url="https://alerts.example.test/hooks",
            signing_secret=secret,
            timeout_seconds=timeout,
        )


def alert_row(**overrides):
    return {
        "alert_id": uuid4(),
        "workspace_id": uuid4(),
        "period_start": date(2026, 9, 1),
        "threshold_percent": 80,
        "monthly_limit_micros": 100_000,
        "consumed_micros": 80_000,
        "enforcement": "enforce",
        "created_at": datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        **overrides,
    }


def test_payload_carries_amounts_and_identifiers_only():
    row = alert_row()
    payload = alert_payload(row)
    assert payload == {
        "event": EVENT_TYPE,
        "event_id": str(row["alert_id"]),
        "occurred_at": "2026-09-12T09:00:00+00:00",
        "workspace_id": str(row["workspace_id"]),
        "period_start": "2026-09-01",
        "threshold_percent": 80,
        "monthly_limit_micros": 100_000,
        "consumed_micros": 80_000,
        "enforcement": "enforce",
    }
    # Nothing a tenant wrote, and nothing that names a person or a workspace.
    serialized = json.dumps(payload)
    for leaked in ("name", "email", "subject", "issuer", "prompt", "model", "token"):
        assert leaked not in serialized


def test_signature_binds_body_and_timestamp():
    body = json.dumps(alert_payload(alert_row()), separators=(",", ":"), sort_keys=True).encode()
    signed = signature(SECRET, 1_760_000_000, body)
    assert signed.startswith("v1=")
    assert len(signed) == len("v1=") + 64
    assert signature(SECRET, 1_760_000_000, body) == signed
    # A replay at another time, a changed body or another secret all break it.
    assert signature(SECRET, 1_760_000_001, body) != signed
    assert signature(SECRET, 1_760_000_000, body + b" ") != signed
    assert signature("b" * 32, 1_760_000_000, body) != signed


def test_retry_delay_is_bounded_and_deterministic():
    alert_id = uuid4()
    delays = [delivery_retry_delay(alert_id, attempt) for attempt in range(MAX_DELIVERY_ATTEMPTS)]
    assert delays == [delivery_retry_delay(alert_id, attempt) for attempt in range(len(delays))]
    assert all(0 < delay <= 375.0 for delay in delays)
    assert delays == sorted(delays)
    assert delivery_retry_delay(uuid4(), 3) != delivery_retry_delay(alert_id, 3)

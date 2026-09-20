"""Operator-configured delivery of budget threshold alerts.

The alert row and its delivery intent are written together; this worker is the only
thing that turns that intent into an outbound request. The endpoint is operator
configuration, never tenant input: a workspace cannot name a URL, so a spend alert
cannot become a request to an address a tenant chose. Requests are signed, carry no
prompt, model, user or workspace-name data, and are retried a bounded number of times
before the notification is abandoned rather than retried forever.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import psycopg
from psycopg.rows import dict_row

from nexora_api.config import Settings
from nexora_api.logs import context as log_context
from nexora_api.logs import logger
from nexora_api.metrics import observe_spend_alert_delivery

MAX_DELIVERY_ATTEMPTS = 5
MAX_RESPONSE_BYTES = 16 * 1024
EVENT_TYPE = "spend.threshold.reached.v1"
SIGNATURE_VERSION = "v1"
delivery_log = logger("worker.spend_alerts")


def delivery_retry_delay(alert_id: UUID, attempts: int) -> float:
    base = min(300.0, float(2 ** min(attempts, 8)))
    digest = hashlib.sha256(f"{alert_id}:{attempts}".encode()).digest()
    jitter = int.from_bytes(digest[:2], "big") / 65535
    return round(base * (1 + 0.25 * jitter), 3)


class AlertDeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SpendAlertWebhook:
    """An operator-declared notification endpoint. Tenants cannot influence it."""

    url: str
    signing_secret: str
    bearer_token: str | None = None
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError(
                "alert webhook must be an HTTPS URL on port 443 without credentials/query"
            )
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost":
            raise ValueError("alert webhook hostname is not allowed")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("alert webhook literal IP must be globally routable")
        # An unsigned webhook is forgeable by anyone who learns the URL.
        if len(self.signing_secret) < 32:
            raise ValueError("alert webhook signing secret must be at least 32 characters")
        if not 0.1 <= self.timeout_seconds <= 60:
            raise ValueError("alert webhook timeout must be between 0.1 and 60 seconds")


def alert_payload(row) -> dict:
    """The event a receiver gets: amounts and identifiers, never tenant content."""
    return {
        "event": EVENT_TYPE,
        "event_id": str(row["alert_id"]),
        "occurred_at": row["created_at"].isoformat(),
        "workspace_id": str(row["workspace_id"]),
        "period_start": row["period_start"].isoformat(),
        "threshold_percent": row["threshold_percent"],
        "monthly_limit_micros": row["monthly_limit_micros"],
        "consumed_micros": row["consumed_micros"],
        "enforcement": row["enforcement"],
    }


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """Sign timestamp and body together so a captured body cannot be replayed later."""
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


class SpendAlertNotifier:
    def __init__(
        self,
        settings: Settings,
        webhook: SpendAlertWebhook,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        client: httpx.AsyncClient | None = None,
    ):
        if not 3 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 3 and 300")
        self.settings = settings
        self.webhook = webhook
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            yield connection

    async def process_once(self) -> bool:
        row = await self._claim()
        if row is None:
            return False
        try:
            await self._deliver(row)
        except AlertDeliveryError as exc:
            await self._fail(row, exc.code, retryable=exc.retryable)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(row, type(exc).__name__[:100], retryable=True)
        else:
            await self._succeed(row)
        return True

    async def _claim(self):
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT o.alert_id,o.workspace_id,o.attempts,
                          a.period_start,a.threshold_percent,a.monthly_limit_micros,
                          a.consumed_micros,a.enforcement,a.created_at
                   FROM workspace_spend_alert_outbox o
                   JOIN workspace_spend_alerts a ON a.id=o.alert_id
                   WHERE o.delivered_at IS NULL AND o.dead_lettered_at IS NULL
                     AND o.available_at <= now()
                     AND (o.lease_expires_at IS NULL OR o.lease_expires_at < now())
                   ORDER BY o.available_at,o.created_at,o.alert_id
                   FOR UPDATE OF o SKIP LOCKED
                   LIMIT 1"""
            )
            row = await result.fetchone()
            if row is None:
                return None
            await connection.execute(
                """UPDATE workspace_spend_alert_outbox
                   SET lease_owner=%s,lease_expires_at=now()+(%s * interval '1 second'),
                       updated_at=now()
                   WHERE alert_id=%s""",
                (self.worker_id, self.lease_seconds, row["alert_id"]),
            )
            return row

    async def _deliver(self, row) -> None:
        body = json.dumps(alert_payload(row), separators=(",", ":"), sort_keys=True).encode()
        timestamp = int(time.time())
        headers = {
            "content-type": "application/json",
            "nexora-event": EVENT_TYPE,
            "nexora-delivery-id": str(row["alert_id"]),
            "nexora-timestamp": str(timestamp),
            "nexora-signature": signature(self.webhook.signing_secret, timestamp, body),
        }
        if self.webhook.bearer_token:
            headers["authorization"] = "Bearer " + self.webhook.bearer_token
        try:
            async with self._client.stream(
                "POST",
                self.webhook.url,
                headers=headers,
                content=body,
                timeout=self.webhook.timeout_seconds,
                follow_redirects=False,
            ) as response:
                # The response body is never used; it is drained under a bound so a
                # hostile or broken receiver cannot hold the worker open.
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise AlertDeliveryError("alert_response_too_large", retryable=False)
                self._raise_for_status(response.status_code)
        except httpx.TimeoutException as exc:
            raise AlertDeliveryError("alert_timeout", retryable=True) from exc
        except httpx.TransportError as exc:
            raise AlertDeliveryError("alert_unavailable", retryable=True) from exc

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            # A redirect could move a signed tenant event to an unvetted host.
            raise AlertDeliveryError("alert_redirect_forbidden", retryable=False)
        if status in {408, 425, 429} or 500 <= status < 600:
            raise AlertDeliveryError("alert_unavailable", retryable=True)
        raise AlertDeliveryError("alert_rejected", retryable=False)

    async def _succeed(self, row) -> None:
        async with self.connection() as connection:
            updated = await connection.execute(
                """UPDATE workspace_spend_alert_outbox
                   SET delivered_at=now(),attempts=attempts+1,last_error=NULL,
                       lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                   WHERE alert_id=%s AND delivered_at IS NULL AND dead_lettered_at IS NULL
                     AND lease_owner=%s""",
                (row["alert_id"], self.worker_id),
            )
            # A lost lease means another worker owns this notification now.
            delivered = updated.rowcount == 1
        if delivered:
            observe_spend_alert_delivery("delivered")

    async def _fail(self, row, error_code: str, *, retryable: bool) -> None:
        attempts = row["attempts"] + 1
        abandoned = not retryable or attempts >= MAX_DELIVERY_ATTEMPTS
        async with self.connection() as connection:
            if abandoned:
                updated = await connection.execute(
                    """UPDATE workspace_spend_alert_outbox
                       SET attempts=%s,dead_lettered_at=now(),last_error=%s,
                           lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                       WHERE alert_id=%s AND delivered_at IS NULL AND dead_lettered_at IS NULL
                         AND lease_owner=%s""",
                    (attempts, error_code[:100], row["alert_id"], self.worker_id),
                )
            else:
                updated = await connection.execute(
                    """UPDATE workspace_spend_alert_outbox
                       SET attempts=%s,available_at=now()+(%s * interval '1 second'),
                           last_error=%s,lease_owner=NULL,lease_expires_at=NULL,
                           updated_at=now()
                       WHERE alert_id=%s AND delivered_at IS NULL AND dead_lettered_at IS NULL
                         AND lease_owner=%s""",
                    (
                        attempts,
                        delivery_retry_delay(row["alert_id"], attempts),
                        error_code[:100],
                        row["alert_id"],
                        self.worker_id,
                    ),
                )
            changed = updated.rowcount == 1

        # If this worker lost its lease, another worker owns the durable outcome.
        # Do not emit a retry/abandoned metric or warning for a transition we did not commit.
        if not changed:
            return

        observe_spend_alert_delivery("abandoned" if abandoned else "retry")
        # An abandoned notification is an operator problem, so it is logged once with
        # the reason. The durable alert itself stays readable in the console.
        delivery_log.warning(
            "spend alert delivery failed",
            extra=log_context(
                worker_id=self.worker_id,
                error_code=error_code,
                outcome="abandoned" if abandoned else "retry",
            ),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

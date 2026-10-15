"""Cloud transport implementations for the local outbox."""

from __future__ import annotations

import json
import ssl
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.robot_core.common import utc_now


class HttpsOutboxSender:
    """Send one versioned event over an outbound HTTPS/mTLS connection.

    The robot initiates every connection.  The cloud endpoint acknowledges the
    event by returning JSON with an optional ``acknowledgedAt`` timestamp.  An
    HTTP status outside 2xx raises so the outbox dispatcher can retry safely.
    """

    def __init__(
        self,
        endpoint: str,
        robot_id: str,
        *,
        timeout_seconds: float = 10.0,
        certificate_file: Optional[str] = None,
        private_key_file: Optional[str] = None,
        ca_file: Optional[str] = None,
    ) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("HTTPS outbox transport requires an https:// endpoint")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if bool(certificate_file) != bool(private_key_file):
            raise ValueError("certificate_file and private_key_file must be configured together")
        self.endpoint = endpoint
        self.robot_id = robot_id
        self.timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context(cafile=ca_file)
        if certificate_file and private_key_file:
            self._ssl_context.load_cert_chain(certificate_file, private_key_file)

    def __call__(self, item: Mapping[str, Any]) -> Mapping[str, str]:
        payload = {
            "contractVersion": "robot.event.v1",
            "eventId": item["outbox_id"],
            "robotId": self.robot_id,
            "eventType": item["event_type"],
            "aggregateType": item["aggregate_type"],
            "aggregateId": item["aggregate_id"],
            "idempotencyKey": item["idempotency_key"],
            "occurredAt": item["created_at"],
            "payload": item["payload"],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Idempotency-Key": str(item["idempotency_key"]),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=self._ssl_context) as response:
                raw = response.read()
        except HTTPError as exc:
            body = exc.read(512).decode("utf-8", errors="replace")
            raise RuntimeError(f"cloud returned HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"cloud connection failed: {exc.reason}") from exc

        acknowledgement: Mapping[str, Any] = {}
        if raw:
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("cloud acknowledgement was not valid JSON") from exc
            if not isinstance(value, dict):
                raise RuntimeError("cloud acknowledgement must be a JSON object")
            acknowledgement = value
        return {"acknowledged_at": str(acknowledgement.get("acknowledgedAt") or utc_now())}


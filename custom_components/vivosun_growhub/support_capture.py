"""Local opt-in support capture utilities for diagnostics exports."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .redaction import sanitize_mapping_for_debug


class SupportCaptureManager:
    """Track an opt-in, bounded support capture session locally."""

    def __init__(self) -> None:
        """Initialize capture state."""
        self._active = False
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._max_events = 0
        self._dropped_events = 0
        self._subscription_topics: list[str] = []
        self._subscription_results: dict[str, dict[str, object]] = {}
        self._model_metadata_results: list[dict[str, object]] = []
        self._devices: list[dict[str, object]] = []
        self._events: deque[dict[str, object]] = deque()

    @property
    def active(self) -> bool:
        """Return whether support capture is currently active."""
        return self._active

    def start(
        self,
        *,
        max_events: int,
        devices: list[dict[str, object]],
        subscription_topics: list[str],
    ) -> None:
        """Start a new support capture session, resetting prior buffered events."""
        self._active = True
        self._started_at = datetime.now(tz=UTC)
        self._stopped_at = None
        self._max_events = max_events
        self._dropped_events = 0
        self._subscription_topics = list(subscription_topics)
        self._subscription_results = {}
        self._model_metadata_results = []
        self._devices = [sanitize_mapping_for_debug(dict(device)) for device in devices]
        self._events = deque(maxlen=max_events)

    def stop(self) -> None:
        """Stop the current support capture session."""
        self._active = False
        self._stopped_at = datetime.now(tz=UTC)

    def record(self, kind: str, *, data: Mapping[str, object] | None = None) -> None:
        """Record a redacted event when support capture is active."""
        if not self._active:
            return
        if self._events.maxlen is not None and len(self._events) == self._events.maxlen:
            self._dropped_events += 1
        event: dict[str, object] = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "kind": kind,
        }
        if data:
            event["data"] = sanitize_mapping_for_debug(dict(data))
        self._events.append(event)

    def record_subscription_result(
        self,
        topic: str,
        *,
        status: str,
        reason: str | None = None,
    ) -> None:
        """Record the latest broker outcome for an attempted support topic filter."""
        result: dict[str, object] = {"topic": topic, "status": status}
        if reason is not None:
            result["reason"] = reason
        self._subscription_results[topic] = sanitize_mapping_for_debug(result)

    def record_model_metadata_result(self, result: Mapping[str, object]) -> None:
        """Record a support-capture model metadata result."""
        self._model_metadata_results.append(sanitize_mapping_for_debug(dict(result)))

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostics-safe snapshot of support capture state."""
        return {
            "active": self._active,
            "started_at": self._started_at.isoformat() if self._started_at is not None else None,
            "stopped_at": self._stopped_at.isoformat() if self._stopped_at is not None else None,
            "max_events": self._max_events,
            "dropped_events": self._dropped_events,
            "subscription_topics": list(self._subscription_topics),
            "subscription_results": [
                self._subscription_results[topic] for topic in sorted(self._subscription_results)
            ],
            "model_metadata_results": list(self._model_metadata_results),
            "devices": list(self._devices),
            "events": list(self._events),
        }


def summarize_support_capture_payload(payload: bytes) -> dict[str, object]:
    """Return a diagnostics-safe payload summary for support capture events."""
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {"payload_hex": payload.hex()[:512]}

    try:
        import json

        parsed = json.loads(decoded)
    except ValueError:
        return {"payload_text": decoded[:512]}

    if isinstance(parsed, Mapping):
        return {"payload": sanitize_mapping_for_debug(dict(parsed))}
    if isinstance(parsed, Sequence) and not isinstance(parsed, (str, bytes, bytearray)):
        return {"payload": sanitize_mapping_for_debug({"items": list(parsed)})}
    return {"payload_text": decoded[:512]}

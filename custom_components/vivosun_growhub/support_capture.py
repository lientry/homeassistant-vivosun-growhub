"""Local opt-in support capture utilities for diagnostics exports."""

from __future__ import annotations

import json
import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from .redaction import REDACTED, is_sensitive_capture_key, redact_topic, sanitize_mapping_for_capture

SUPPORT_CAPTURE_MAX_DEVICES = 25
SUPPORT_CAPTURE_MAX_PAYLOAD_BYTES = 65536
SUPPORT_CAPTURE_MAX_MQTT_VALUES = 1500
SUPPORT_CAPTURE_MAX_REST_VALUES = 500
SUPPORT_CAPTURE_MAX_EVENT_BYTES = 72 * 1024
SUPPORT_CAPTURE_MAX_EVENT_BUFFER_BYTES = 4 * 1024 * 1024
SUPPORT_CAPTURE_MAX_REST_SAMPLES_PER_DEVICE = 20
SUPPORT_CAPTURE_MAX_REST_SAMPLE_BYTES = 16 * 1024
SUPPORT_CAPTURE_MAX_STAGE_RESULTS = 25


class SupportCaptureManager:
    """Track an opt-in, bounded support capture session locally."""

    def __init__(self) -> None:
        """Initialize capture state."""
        self._active = False
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._max_events = 0
        self._dropped_events = 0
        self._event_buffer_bytes = 0
        self._subscription_topics: list[str] = []
        self._subscription_results: dict[str, dict[str, object]] = {}
        self._model_metadata_results: list[dict[str, object]] = []
        self._shadow_read_results: dict[str, dict[str, object]] = {}
        self._rest_telemetry_results: dict[str, dict[str, object]] = {}
        self._rest_samples: dict[str, list[dict[str, object]]] = {}
        self._stage_info_results: dict[str, dict[str, object]] = {}
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
        self._event_buffer_bytes = 0
        self._subscription_topics = [redact_topic(topic) for topic in subscription_topics]
        self._subscription_results = {}
        self._model_metadata_results = []
        self._shadow_read_results = {}
        self._rest_telemetry_results = {}
        self._rest_samples = {}
        self._stage_info_results = {}
        self._devices = [sanitize_mapping_for_capture(dict(device)) for device in devices]
        self._events = deque(maxlen=max_events)

    def stop(self) -> None:
        """Stop the current support capture session."""
        self._active = False
        self._stopped_at = datetime.now(tz=UTC)

    def record(self, kind: str, *, data: Mapping[str, object] | None = None) -> None:
        """Record a redacted event when support capture is active."""
        if not self._active:
            return
        event: dict[str, object] = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "kind": kind,
        }
        if data:
            event["data"] = sanitize_mapping_for_capture(dict(data))
        encoded_size = len(json.dumps(event, default=str, ensure_ascii=True).encode())
        if encoded_size > SUPPORT_CAPTURE_MAX_EVENT_BYTES:
            event = {"ts": event["ts"], "kind": kind, "data": {"payload_status": "event_too_large"}}
            encoded_size = len(json.dumps(event).encode())
        while self._events and (
            len(self._events) == self._max_events
            or self._event_buffer_bytes + encoded_size > SUPPORT_CAPTURE_MAX_EVENT_BUFFER_BYTES
        ):
            oldest = self._events.popleft()
            self._event_buffer_bytes -= len(json.dumps(oldest, default=str, ensure_ascii=True).encode())
            self._dropped_events += 1
        self._events.append(event)
        self._event_buffer_bytes += encoded_size

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
        self._subscription_results[topic] = sanitize_mapping_for_capture(result)

    def record_model_metadata_result(self, result: Mapping[str, object]) -> None:
        """Record a support-capture model metadata result."""
        self._model_metadata_results.append(sanitize_mapping_for_capture(dict(result)))

    def record_shadow_read_result(self, client_id: str, result: Mapping[str, object]) -> None:
        """Keep each device's latest read result outside the evicting event buffer."""
        if self._active and (
            client_id in self._shadow_read_results or len(self._shadow_read_results) < SUPPORT_CAPTURE_MAX_DEVICES
        ):
            self._shadow_read_results[client_id] = sanitize_mapping_for_capture({"client_id": client_id, **result})

    def record_rest_telemetry_result(self, device_id: str, result: Mapping[str, object]) -> None:
        """Retain the latest bounded REST result and a rolling event for each active poll."""
        if not self._active:
            return
        data = {"device_id": device_id, **result}
        if device_id in self._rest_telemetry_results or len(self._rest_telemetry_results) < SUPPORT_CAPTURE_MAX_DEVICES:
            self._rest_telemetry_results[device_id] = sanitize_mapping_for_capture(data)
        self.record("rest_point_log", data=data)

    def record_rest_samples(self, device_id: str, rows: Sequence[Mapping[str, object]]) -> None:
        """Keep distinct recent returned rows without issuing any extra cloud requests."""
        if not self._active:
            return
        if device_id not in self._rest_samples and len(self._rest_samples) >= SUPPORT_CAPTURE_MAX_DEVICES:
            return
        samples = self._rest_samples.setdefault(device_id, [])
        seen = {json.dumps(sample, sort_keys=True) for sample in samples}
        for row in rows[-SUPPORT_CAPTURE_MAX_REST_SAMPLES_PER_DEVICE:]:
            sample = summarize_rest_telemetry(row)
            if len(json.dumps(sample, ensure_ascii=True).encode()) > SUPPORT_CAPTURE_MAX_REST_SAMPLE_BYTES:
                continue
            key = json.dumps(sample, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            samples.append(sample)
            if len(samples) > SUPPORT_CAPTURE_MAX_REST_SAMPLES_PER_DEVICE:
                samples.pop(0)

    def record_stage_info_result(self, stage_id: str, result: Mapping[str, object]) -> None:
        """Retain bounded outcomes from existing stage-info reads by known stage ID."""
        if not self._active:
            return
        if (
            stage_id not in self._stage_info_results
            and len(self._stage_info_results) >= SUPPORT_CAPTURE_MAX_STAGE_RESULTS
        ):
            return
        bounded, truncated = _bounded_capture_payload(result, max_values=SUPPORT_CAPTURE_MAX_REST_VALUES)
        safe = sanitize_mapping_for_capture({"stage_id": stage_id, **cast("Mapping[str, object]", bounded)})
        if len(json.dumps(safe, ensure_ascii=True, default=str).encode()) > SUPPORT_CAPTURE_MAX_PAYLOAD_BYTES:
            safe = {"stage_id": stage_id, "status": result.get("status", "unknown")}
            truncated = True
        safe["truncated"] = truncated
        self._stage_info_results[stage_id] = safe

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostics-safe snapshot of support capture state."""
        return {
            "active": self._active,
            "started_at": self._started_at.isoformat() if self._started_at is not None else None,
            "stopped_at": self._stopped_at.isoformat() if self._stopped_at is not None else None,
            "max_events": self._max_events,
            "dropped_events": self._dropped_events,
            "event_buffer_bytes": self._event_buffer_bytes,
            "subscription_topics": list(self._subscription_topics),
            "subscription_results": [
                self._subscription_results[topic] for topic in sorted(self._subscription_results)
            ],
            "model_metadata_results": list(self._model_metadata_results),
            "shadow_read_results": list(self._shadow_read_results.values()),
            "rest_telemetry_results": list(self._rest_telemetry_results.values()),
            "rest_samples": [
                {"device_id": device_id, "samples": samples}
                for device_id, samples in self._rest_samples.items()
            ],
            "stage_info_results": list(self._stage_info_results.values()),
            "devices": list(self._devices),
            "events": list(self._events),
        }


def summarize_support_capture_payload(payload: bytes) -> dict[str, object]:
    """Keep bounded JSON evidence; omit oversized, binary and unstructured bodies."""
    summary: dict[str, object] = {"payload_bytes": len(payload)}
    if len(payload) > SUPPORT_CAPTURE_MAX_PAYLOAD_BYTES:
        return {**summary, "payload_status": "too_large"}
    try:
        parsed = json.loads(payload)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {**summary, "payload_status": "non_json"}
    if not isinstance(parsed, (dict, list)):
        return {**summary, "payload_status": "non_object"}
    bounded_payload, truncated = _bounded_capture_payload(parsed, max_values=SUPPORT_CAPTURE_MAX_MQTT_VALUES)
    safe = {"payload": bounded_payload}
    return {**summary, **sanitize_mapping_for_capture(safe), "payload_truncated": truncated}


def _bounded_capture_payload(payload: object, *, max_values: int) -> tuple[object, bool]:
    """Bound collection size, depth and values before redaction or serialization."""
    remaining = max_values
    truncated = False

    def bounded(value: object, depth: int = 0) -> object:
        nonlocal remaining, truncated
        remaining -= 1
        if remaining < 0 or depth > 8:
            truncated = True
            return "<omitted>"
        if isinstance(value, Mapping):
            if len(value) > 80:
                truncated = True
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 80 or remaining <= 0:
                    truncated = True
                    break
                result[key] = bounded(item, depth + 1)
            return result
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if len(value) > 80:
                truncated = True
            items = []
            for item in value[:80]:
                if remaining <= 0:
                    truncated = True
                    break
                items.append(bounded(item, depth + 1))
            return items
        if isinstance(value, str) and len(value) > 256:
            truncated = True
            return "<long string omitted>"
        return value

    return bounded(payload), truncated


def summarize_rest_telemetry(row: Mapping[str, object]) -> dict[str, object]:
    """Keep bounded REST values and device IDs while masking recognized private fields."""
    bounded_row, truncated = _bounded_capture_payload(row, max_values=SUPPORT_CAPTURE_MAX_REST_VALUES)
    redacted_values = 0

    def sanitized(value: object) -> object:
        nonlocal redacted_values, truncated
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._]{0,47}", key):
                    truncated = True
                    continue
                if is_sensitive_capture_key(key):
                    result[key] = REDACTED
                    redacted_values += 1
                else:
                    result[key] = sanitized(item)
            return result
        if isinstance(value, list):
            return [sanitized(item) for item in value]
        if value is None or isinstance(value, str) or type(value) is bool:
            return value
        if type(value) is int and value.bit_length() <= 63:
            return value
        if type(value) is float and math.isfinite(value):
            return value
        redacted_values += 1
        return REDACTED

    telemetry = sanitize_mapping_for_capture(cast("Mapping[str, object]", sanitized(bounded_row)))
    if len(json.dumps(telemetry, allow_nan=False).encode()) > SUPPORT_CAPTURE_MAX_PAYLOAD_BYTES:
        telemetry = {}
        truncated = True
    return {
        "telemetry": telemetry,
        "telemetry_truncated": truncated,
        "telemetry_redacted_values": redacted_values,
    }

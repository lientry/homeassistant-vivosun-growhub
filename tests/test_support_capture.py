"""Tests for support capture utilities and services."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub import async_setup, async_setup_entry, async_unload_entry
from custom_components.vivosun_growhub.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
    OPTION_SUPPORT_CAPTURE_ENABLED,
    PLATFORMS,
    SERVICE_START_SUPPORT_CAPTURE,
    SERVICE_STOP_SUPPORT_CAPTURE,
    SUPPORT_CAPTURE_DEFAULT_MAX_EVENTS,
)
from custom_components.vivosun_growhub.support_capture import (
    SUPPORT_CAPTURE_MAX_EVENT_BUFFER_BYTES,
    SUPPORT_CAPTURE_MAX_MQTT_VALUES,
    SUPPORT_CAPTURE_MAX_STAGE_RESULTS,
    SupportCaptureManager,
    summarize_rest_telemetry,
    summarize_support_capture_payload,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


class _CoordinatorStub:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.started = False
        self.stopped = False
        self.started_capture_max_events: list[int] = []
        self.stop_capture_calls = 0

    async def async_start(self) -> None:
        self.started = True

    async def async_shutdown(self) -> None:
        self.stopped = True

    async def async_start_support_capture(self, *, max_events: int) -> None:
        self.started_capture_max_events.append(max_events)

    async def async_stop_support_capture(self) -> None:
        self.stop_capture_calls += 1


def test_stage_info_collection_is_bounded_private_and_resets() -> None:
    manager = SupportCaptureManager()
    manager.record_stage_info_result("ignored", {"status": "success"})
    assert manager.snapshot()["stage_info_results"] == []

    manager.start(max_events=1, devices=[], subscription_topics=[])
    for index in range(SUPPORT_CAPTURE_MAX_STAGE_RESULTS + 2):
        manager.record_stage_info_result(f"stage-{index}", {
            "status": "success",
            "content": {
                "duration": index,
                "hwId": "sensitive",
                "accessToken": "secret",
                "nested": {"hardware_id": "private", "userPlanStageId": "stage-0"},
                "large": list(range(120)),
            },
        })

    saved = manager.snapshot()["stage_info_results"]
    assert len(saved) == SUPPORT_CAPTURE_MAX_STAGE_RESULTS
    assert saved[0]["stage_id"] == "stage-0"
    assert saved[0]["content"]["duration"] == 0
    assert saved[0]["content"]["hwId"] == "***"
    assert saved[0]["content"]["accessToken"] == "***"
    assert saved[0]["content"]["nested"] == {"hardware_id": "***", "userPlanStageId": "stage-0"}
    assert saved[0]["truncated"] is True
    assert "sensitive" not in str(saved)
    assert "private" not in str(saved)

    manager.record_stage_info_result("stage-0", {"status": "error", "error_type": "VivosunResponseError"})
    assert len(manager.snapshot()["stage_info_results"]) == SUPPORT_CAPTURE_MAX_STAGE_RESULTS
    assert manager.snapshot()["stage_info_results"][0] == {
        "stage_id": "stage-0", "status": "error", "error_type": "VivosunResponseError", "truncated": False,
    }
    manager.stop()
    manager.record_stage_info_result("stage-0", {"status": "success"})
    assert manager.snapshot()["stage_info_results"][0]["status"] == "error"
    manager.start(max_events=1, devices=[], subscription_topics=[])
    assert manager.snapshot()["stage_info_results"] == []


def test_stage_info_oversized_result_omits_content() -> None:
    manager = SupportCaptureManager()
    manager.start(max_events=2, devices=[], subscription_topics=[])
    manager.record_stage_info_result("stage-1", {
        "status": "success",
        "content": {f"field{i}": ["A" * 256] * 80 for i in range(80)},
    })
    assert manager.snapshot()["stage_info_results"] == [
        {"stage_id": "stage-1", "status": "success", "truncated": True},
    ]


def test_support_capture_manager_records_bounded_events() -> None:
    manager = SupportCaptureManager()
    manager.start(
        max_events=2,
        devices=[{"device_id": "dev-1"}],
        subscription_topics=["$aws/things/dev-1/shadow/get/rejected"],
    )
    manager.record("one", data={"value": 1})
    manager.record("two", data={"value": 2})
    manager.record("three", data={"value": 3})
    manager.stop()

    snapshot = manager.snapshot()

    assert snapshot["active"] is False
    assert snapshot["max_events"] == 2
    assert snapshot["dropped_events"] == 1
    assert snapshot["subscription_results"] == []
    assert snapshot["model_metadata_results"] == []
    events = cast("list[dict[str, object]]", snapshot["events"])
    assert [event["kind"] for event in events] == ["two", "three"]


def test_support_capture_event_buffer_has_total_byte_limit() -> None:
    manager = SupportCaptureManager()
    manager.start(max_events=5000, devices=[], subscription_topics=[])
    for index in range(600):
        manager.record("many", data={"index": index, "payload": "A" * 7000})
    snapshot = manager.snapshot()
    assert snapshot["event_buffer_bytes"] <= SUPPORT_CAPTURE_MAX_EVENT_BUFFER_BYTES
    assert snapshot["dropped_events"] > 0
    assert snapshot["events"][-1]["data"]["index"] == 599


def test_support_capture_rest_samples_are_bounded_and_deduplicated() -> None:
    manager = SupportCaptureManager()
    manager.start(max_events=10, devices=[], subscription_topics=[])
    manager.record_rest_samples("device-a", [{"time": 1, "reading": "auto"}, {"time": 2, "reading": 42}])
    manager.record_rest_samples("device-a", [{"time": 2, "reading": 42}, {"time": 3, "reading": 43}])
    samples = manager.snapshot()["rest_samples"][0]["samples"]
    assert [sample["telemetry"]["time"] for sample in samples] == [1, 2, 3]
    manager.record_rest_samples("device-a", [{"time": index} for index in range(40)])
    samples = manager.snapshot()["rest_samples"][0]["samples"]
    assert len(samples) == 20
    assert samples[0]["telemetry"]["time"] == 20
    manager.start(max_events=10, devices=[], subscription_topics=[])
    assert manager.snapshot()["rest_samples"] == []


def test_capture_keeps_read_results_after_event_eviction_and_redacts_routes() -> None:
    manager = SupportCaptureManager()
    topic = "$aws/things/vivosun-VSSBS40-account-device/shadow/get/accepted"
    manager.start(max_events=1, devices=[], subscription_topics=[topic])
    manager.record_shadow_read_result(
        "vivosun-VSSBS40-account-device",
        {
            "status": "accepted",
            "model_token": "VSSBS40",
            "payload": {"state": {"reported": {"mode": 2}}},
        },
    )
    manager.record("mqtt_publish", data={"topic": topic})
    manager.record("later")

    snapshot = manager.snapshot()
    assert snapshot["shadow_read_results"][0]["payload"]["state"]["reported"]["mode"] == 2
    assert snapshot["shadow_read_results"][0]["model_token"] == "VSSBS40"
    assert "account-device" not in str(snapshot)
    manager.start(max_events=1, devices=[], subscription_topics=[])
    assert manager.snapshot()["shadow_read_results"] == []


def test_capture_payload_limits_bytes_and_omits_unstructured_text() -> None:
    for payload in (b"password=must-not-appear", b"\xffmust-not-appear", b"x" * 65537):
        summary = summarize_support_capture_payload(payload)
        assert summary["payload_bytes"] == len(payload)
        assert "must-not-appear" not in str(summary)
        assert "payload_text" not in summary
        assert "payload_hex" not in summary


def test_capture_keeps_both_sides_of_large_shadow_transition() -> None:
    """E42A-sized update/documents must not lose late device fields."""
    reported = {
        f"accessory{i}": {
            "mode": i % 3,
            "manual": {"level": i, "target": i * 10},
            "connected": True,
        }
        for i in range(60)
    }
    previous = {"state": {"reported": reported, "desired": {"dFan": {"mode": 0}}}}
    current = json.loads(json.dumps(previous))
    current["state"]["reported"]["accessory59"]["manual"]["level"] = 99
    current["state"]["reported"]["accessory59"]["devPass"] = "private-password"
    payload = json.dumps({"previous": previous, "current": current}).encode()
    assert len(payload) < 65536

    summary = summarize_support_capture_payload(payload)

    assert summary["payload_truncated"] is False
    assert summary["payload"]["previous"]["state"]["reported"]["accessory59"]["manual"]["level"] == 59
    assert summary["payload"]["current"]["state"]["reported"]["accessory59"]["manual"]["level"] == 99
    assert summary["payload"]["current"]["state"]["reported"]["accessory59"]["devPass"] == "***"
    assert "private-password" not in json.dumps(summary)

    manager = SupportCaptureManager()
    manager.start(max_events=10, devices=[], subscription_topics=[])
    manager.record("mqtt_publish", data={"topic": "$aws/things/thing-1/shadow/update/documents", **summary})
    retained = manager.snapshot()["events"][0]["data"]
    assert retained["payload_truncated"] is False
    assert retained["payload"]["current"]["state"]["reported"]["accessory59"]["manual"]["level"] == 99


def test_rest_telemetry_preserves_strings_and_device_ids_but_masks_private_fields() -> None:
    private = {
        "accountId": 61234567, "hwId": 51234567, "auth_header": 41234567,
        "ipAddr": 31234567, "gpsLat": 21234567, "clientId": "raw-thing",
        "email": "person@example.com", "serial": "raw-serial", "cookie": "raw-cookie",
        "headers": {"opaque": "raw-header"}, "apiKey": "raw-api-key",
    }
    row = {
        "humid": 54, "futureSensor": 42.5, "probe": private,
        "channel.deviceId": 11234567, "samples": [1, False, None, "auto", private],
        "deviceId": "new-device", "scene_id": 81234567, "newSampleID": 71234567,
        "program": {"id": "program-1", "mode": "dry", "target": "55.0"},
        "userPlanStageId": "stage-1",
        "nanReading": float("nan"), "hugeNumber": 10 ** 5000,
        "location": {"latitude": 12.3, "longitude": 45.6},
    }

    summary = summarize_rest_telemetry(row)

    telemetry = summary["telemetry"]
    assert telemetry["humid"] == 54
    assert telemetry["futureSensor"] == 42.5
    for key in private:
        if key == "clientId":
            assert telemetry["probe"][key] != private[key]
        else:
            assert telemetry["probe"][key] == "***"
    assert telemetry["channel.deviceId"] == 11234567
    assert telemetry["deviceId"] == "new-device"
    assert telemetry["scene_id"] == 81234567
    assert telemetry["newSampleID"] == 71234567
    assert telemetry["program"] == row["program"]
    assert telemetry["userPlanStageId"] == "stage-1"
    assert telemetry["samples"][:4] == [1, False, None, "auto"]
    assert telemetry["samples"][4] == telemetry["probe"]
    assert telemetry["nanReading"] == "***"
    assert telemetry["hugeNumber"] == "***"
    assert telemetry["location"] == "***"
    assert summary["telemetry_redacted_values"] > 0
    encoded = json.dumps(summary, allow_nan=False)
    assert not any(str(value) in encoded for value in private.values() if not isinstance(value, dict))
    assert row["probe"] == private


def test_rest_telemetry_bounds_collections_depth_and_export_size() -> None:
    row = {"sensors": {f"reading{i}": i for i in range(81)}, "samples": list(range(81)),
           "deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}},
           "user@example.com": 42, "x" * 49: 43}
    summary = summarize_rest_telemetry(row)
    assert summary["telemetry_truncated"] is True
    assert len(summary["telemetry"]["sensors"]) == 80
    assert len(summary["telemetry"]["samples"]) == 80
    assert "user@example.com" not in str(summary)
    assert "x" * 49 not in str(summary)
    assert len(json.dumps(summary).encode()) < 65536


def test_capture_value_budget_stops_walking_large_nested_collections() -> None:
    row = {f"group{i}": {f"reading{j}": j for j in range(80)} for i in range(80)}
    rest = summarize_rest_telemetry(row)
    mqtt = summarize_support_capture_payload(json.dumps(row, separators=(",", ":")).encode())
    assert rest["telemetry_truncated"] is True
    assert len(rest["telemetry"]) < 80
    assert sum(len(group) for group in rest["telemetry"].values()) < 500
    assert mqtt["payload_status"] == "too_large"

    smaller = {f"group{i}": list(range(80)) for i in range(25)}
    mqtt = summarize_support_capture_payload(json.dumps(smaller).encode())
    assert mqtt["payload_truncated"] is True
    assert sum(len(group) for group in mqtt["payload"].values()) < SUPPORT_CAPTURE_MAX_MQTT_VALUES


def test_rest_telemetry_strings_obey_existing_length_and_serialized_size_bounds() -> None:
    summary = summarize_rest_telemetry({"mode": "auto", "description": "x" * 257})
    assert summary["telemetry"]["mode"] == "auto"
    assert summary["telemetry"]["description"] == "<long string omitted>"
    assert summary["telemetry_truncated"] is True

    row = {f"group{i}": ["界" * 256] * 80 for i in range(6)}
    summary = summarize_rest_telemetry(row)
    assert summary["telemetry"] == {}
    assert summary["telemetry_truncated"] is True
    assert len(json.dumps(summary).encode()) < 65536


def test_rest_telemetry_latest_results_survive_eviction_and_reset_on_restart() -> None:
    manager = SupportCaptureManager()
    manager.start(max_events=1, devices=[], subscription_topics=[])
    for index in range(26):
        manager.record_rest_telemetry_result(f"device-{index}", summarize_rest_telemetry({"reading": index}))
    manager.record_rest_telemetry_result("device-0", summarize_rest_telemetry({"reading": 100}))
    manager.record("later")
    manager.stop()
    manager.record_rest_telemetry_result("device-0", summarize_rest_telemetry({"reading": 200}))
    results = manager.snapshot()["rest_telemetry_results"]
    assert len(results) == 25
    assert results[0]["telemetry"]["reading"] == 100
    assert results[0]["device_id"] == "device-0"
    manager.start(max_events=1, devices=[], subscription_topics=[])
    assert manager.snapshot()["rest_telemetry_results"] == []


async def test_support_capture_services_start_and_stop_single_entry(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)
    forward_mock = AsyncMock(return_value=True)
    unload_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward_mock)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_mock)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test@example.com",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
    )

    assert await async_setup(hass, {})
    assert await async_setup_entry(hass, entry)

    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator = cast("_CoordinatorStub", runtime.coordinator)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_START_SUPPORT_CAPTURE,
        {"max_events": 123},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_STOP_SUPPORT_CAPTURE,
        {},
        blocking=True,
    )

    assert coordinator.started_capture_max_events == [123]
    assert coordinator.stop_capture_calls == 1
    assert forward_mock.await_args.args == (entry, PLATFORMS)

    assert await async_unload_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, SERVICE_START_SUPPORT_CAPTURE) is False
    assert hass.services.has_service(DOMAIN, SERVICE_STOP_SUPPORT_CAPTURE) is False


async def test_setup_entry_registers_support_capture_services_without_async_setup(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test@example.com",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
    )

    assert await async_setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, SERVICE_START_SUPPORT_CAPTURE) is True
    assert hass.services.has_service(DOMAIN, SERVICE_STOP_SUPPORT_CAPTURE) is True


async def test_setup_entry_starts_support_capture_when_option_enabled(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test@example.com",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
        options={OPTION_SUPPORT_CAPTURE_ENABLED: True},
    )

    assert await async_setup_entry(hass, entry)

    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator = cast("_CoordinatorStub", runtime.coordinator)
    assert coordinator.started_capture_max_events == [SUPPORT_CAPTURE_DEFAULT_MAX_EVENTS]

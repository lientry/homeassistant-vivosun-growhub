"""Confirmed spectrum state from real, sanitized MQTT shadow ingress."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.vivosun_growhub.const import SENSOR_UNAVAILABLE_SENTINEL
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.models import DeviceInfo
from custom_components.vivosun_growhub.select import VivosunSpectrumSelect

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _live_spectrum(hass: HomeAssistant) -> tuple[VivosunCoordinator, VivosunSpectrumSelect]:
    coordinator = VivosunCoordinator(hass, object(), email="offline@example.com", password="unused")
    coordinator._devices = [DeviceInfo("box", "thing", "topic", "VGrow", True, 1, "controller")]
    coordinator._build_topic_maps()
    coordinator._mqtt_client = SimpleNamespace(is_connected=True)
    coordinator.async_publish_shadow_update = AsyncMock()
    coordinator.data = coordinator._build_state_snapshot()
    return coordinator, VivosunSpectrumSelect(coordinator, "box")


async def _report(coordinator: VivosunCoordinator, suffix: str, version: int, light: dict[str, object]) -> None:
    state = {"reported": {"light": light}}
    document: dict[str, object] = {"state": state, "version": version}
    if suffix == "update/documents":
        document = {"current": {"state": state, "version": version}}
    await coordinator._handle_mqtt_publish(
        f"$aws/things/thing/shadow/{suffix}", json.dumps(document).encode(), 1
    )


@pytest.mark.parametrize("field,invalid", [
    ("mode", None), ("mode", "invalid"), ("mode", False),
    ("inPlan", None), ("inPlan", "invalid"), ("inPlan", SENSOR_UNAVAILABLE_SENTINEL),
])
async def test_explicit_unknown_flag_revokes_reported_manual_permission(
    hass: HomeAssistant, field: str, invalid: object,
) -> None:
    coordinator, spectrum = _live_spectrum(hass)
    await _report(coordinator, "get/accepted", 10, {"mode": 0, "inPlan": 0, "spec": 50})
    await spectrum.async_select_option("Seedling")
    assert spectrum.current_option == "Vegetative"
    await _report(coordinator, "update/accepted", 11, {field: invalid})
    with pytest.raises(ServiceValidationError, match="manual mode"):
        await spectrum.async_select_option("Bloom")
    assert coordinator.async_publish_shadow_update.await_count == 1


@pytest.mark.parametrize("invalid", [None, "invalid", False, SENSOR_UNAVAILABLE_SENTINEL])
async def test_explicit_unknown_spectrum_clears_confirmed_option(hass: HomeAssistant, invalid: object) -> None:
    coordinator, spectrum = _live_spectrum(hass)
    await _report(coordinator, "get/accepted", 10, {"mode": 0, "inPlan": 0, "spec": 50})
    await _report(coordinator, "update/accepted", 11, {"spec": invalid})
    assert spectrum.current_option is None
    # Unknown spectrum does not by itself unprove separately reported manual/no-plan flags.
    await spectrum.async_select_option("Seedling")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"light": {"manu": {"spec": 20}}}}}, device_id="box", qos=1
    )


async def test_older_reports_cannot_reopen_manual_writes_or_revert_spectrum(hass: HomeAssistant) -> None:
    coordinator, spectrum = _live_spectrum(hass)
    await _report(coordinator, "get/accepted", 10, {"mode": 0, "inPlan": 0, "spec": 50})
    await _report(coordinator, "update/documents", 14, {"mode": 1, "inPlan": 1, "spec": 100})
    assert spectrum.current_option == "Bloom"
    for suffix in ("get/accepted", "update/documents", "update/accepted"):
        await _report(coordinator, suffix, 13, {"mode": 0, "inPlan": 0, "spec": 20})
        assert spectrum.current_option == "Bloom"
        with pytest.raises(ServiceValidationError, match="manual mode"):
            await spectrum.async_select_option("Seedling")
    coordinator.async_publish_shadow_update.assert_not_awaited()

    # Partial genuine reports at a single revision must independently update their fields.
    await _report(coordinator, "update/documents", 15, {"mode": 0})
    with pytest.raises(ServiceValidationError):
        await spectrum.async_select_option("Seedling")
    await _report(coordinator, "update/accepted", 15, {"inPlan": 0})
    await spectrum.async_select_option("Seedling")
    assert spectrum.current_option == "Bloom"
    coordinator.async_publish_shadow_update.assert_awaited_once()


async def test_missing_or_stale_field_metadata_does_not_promote_old_get(hass: HomeAssistant) -> None:
    coordinator, spectrum = _live_spectrum(hass)
    await _report(coordinator, "get/accepted", 10, {"mode": 0, "inPlan": 0, "spec": 50})
    await _report(coordinator, "update/accepted", 12, {"mode": 1, "inPlan": 1})
    # A desired-only version bump can be followed by a GET containing an older field timestamp.
    document = {
        "version": 13,
        "state": {"reported": {"light": {"mode": 0, "inPlan": 0, "spec": 50}}},
        "metadata": {"reported": {"light": {
            "mode": {"timestamp": 90}, "inPlan": {"timestamp": 90}, "spec": {"timestamp": 90},
        }}},
    }
    # Establish newer actual timestamps before the stale GET arrives.
    await coordinator._handle_mqtt_publish(
        "$aws/things/thing/shadow/update/accepted",
        json.dumps({
            "version": 12, "state": {"reported": {"light": {"mode": 1, "inPlan": 1, "spec": 100}}},
            "metadata": {"reported": {"light": {
                "mode": {"timestamp": 100}, "inPlan": {"timestamp": 100}, "spec": {"timestamp": 100},
            }}},
        }).encode(), 1,
    )
    await coordinator._handle_mqtt_publish(
        "$aws/things/thing/shadow/get/accepted", json.dumps(document).encode(), 1
    )
    assert spectrum.current_option == "Bloom"
    with pytest.raises(ServiceValidationError):
        await spectrum.async_select_option("Seedling")
    coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_spectrum_write_fails_if_device_disconnected(hass: HomeAssistant) -> None:
    coordinator, spectrum = _live_spectrum(hass)
    await _report(coordinator, "get/accepted", 10, {"mode": 0, "inPlan": 0, "spec": 50})
    coordinator._mqtt_client.is_connected = False
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await spectrum.async_select_option("Bloom")
    coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_partial_manual_spectrum_report_can_confirm_and_clear_a_preset(hass: HomeAssistant) -> None:
    coordinator, spectrum = _live_spectrum(hass)
    await _report(coordinator, "get/accepted", 10, {"mode": 0, "inPlan": False, "lv": 0})
    assert spectrum.current_option is None
    await _report(coordinator, "update/documents", 11, {"manu": {"spec": 50}})
    assert spectrum.current_option == "Vegetative"
    await spectrum.async_select_option("Bloom")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"light": {"manu": {"spec": 100}}}}}, device_id="box", qos=1
    )
    # A sparse report preserves reported manual/no-plan facts and level 0.
    await _report(coordinator, "update/accepted", 12, {"manu": {"spec": None}})
    assert spectrum.current_option is None
    assert coordinator.data["shadows"]["box"]["light"]["level"] == 0
    await _report(coordinator, "get/accepted", 13, {"manu": {"spec": 20}})
    assert spectrum.current_option == "Seedling"


async def test_unknown_spectrum_preset_rejected_without_publish(hass: HomeAssistant) -> None:
    coordinator, spectrum = _live_spectrum(hass)
    await _report(coordinator, "get/accepted", 10, {"mode": 0, "inPlan": 0, "spec": 50})
    with pytest.raises(ServiceValidationError, match="Unknown spectrum preset"):
        await spectrum.async_select_option("Custom")
    coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_desired_ack_and_partial_reports_do_not_confirm_spectrum(hass: HomeAssistant) -> None:
    coordinator = VivosunCoordinator(hass, object(), email="offline@example.com", password="unused")
    coordinator._devices = [
        DeviceInfo("box-1", "thing-1", "topic/1", "VGrow", True, 1, "controller")
    ]
    coordinator._build_topic_maps()
    coordinator.data = coordinator._build_state_snapshot()
    spectrum = VivosunSpectrumSelect(coordinator, "box-1")

    async def receive(suffix: str, body: dict[str, object]) -> None:
        await coordinator._handle_mqtt_publish(
            f"$aws/things/thing-1/shadow/{suffix}", json.dumps(body).encode(), 1
        )

    await receive("update/accepted", {"state": {"desired": {"light": {"manu": {"spec": 50}}}}})
    assert spectrum.current_option is None

    await receive("get/accepted", {"state": {"reported": {"light": {
        "spec": 100, "mode": 0, "inPlan": 0, "lv": 60,
    }}}})
    assert spectrum.current_option == "Bloom"

    await receive("update/accepted", {"state": {"desired": {"light": {"manu": {"spec": 20}}}}})
    assert spectrum.current_option == "Bloom"
    await receive("update/accepted", {"state": {"reported": {"light": {"lv": 65}}}})
    assert spectrum.current_option == "Bloom"
    await receive("update/accepted", {"state": {"reported": {"light": {"spec": 77}}}})
    assert spectrum.current_option is None
    await receive("update/accepted", {"state": {"reported": {"light": {"spec": 20}}}})
    assert spectrum.current_option == "Seedling"


async def test_desired_only_manual_flags_do_not_enable_commands(hass: HomeAssistant) -> None:
    coordinator = VivosunCoordinator(hass, object(), email="offline@example.com", password="unused")
    coordinator._devices = [DeviceInfo("box", "thing", "topic", "VGrow", True, 1, "controller")]
    coordinator._build_topic_maps()
    coordinator._mqtt_client = SimpleNamespace(is_connected=True)
    coordinator.async_publish_shadow_update = AsyncMock()
    coordinator.data = coordinator._build_state_snapshot()
    await coordinator._handle_mqtt_publish(
        "$aws/things/thing/shadow/get/accepted",
        json.dumps({"state": {"desired": {"light": {"spec": 50, "mode": 0, "inPlan": 0}}}}).encode(),
        1,
    )
    spectrum = VivosunSpectrumSelect(coordinator, "box")
    assert spectrum.current_option is None
    try:
        await spectrum.async_select_option("Vegetative")
    except ServiceValidationError:
        pass
    else:
        raise AssertionError("No reported manual/no-plan proof")
    coordinator.async_publish_shadow_update.assert_not_awaited()

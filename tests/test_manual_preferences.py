"""Synthetic manual-selection persistence and cloud acceptance regressions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from homeassistant.helpers.storage import Store

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.fan import VivosunCirculationFanEntity, VivosunDuctFanEntity
from custom_components.vivosun_growhub.light import VivosunLightEntity
from custom_components.vivosun_growhub.manual_preferences import ManualPreferences
from custom_components.vivosun_growhub.models import DeviceInfo

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _Mqtt:
    is_connected = True

    def __init__(self) -> None:
        self.published: list[dict[str, object]] = []

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        self.published.append(json.loads(payload))

    async def disconnect(self) -> None:
        self.is_connected = False


async def _setup(hass: HomeAssistant) -> tuple[VivosunCoordinator, _Mqtt, ManualPreferences]:
    coordinator = VivosunCoordinator(hass, object(), email="none@example.invalid", password="unused")
    coordinator._devices = [DeviceInfo("device-1", "thing-1", "channel-1", "GrowHub", True, 1, "controller")]
    coordinator._build_topic_maps()
    mqtt = _Mqtt()
    coordinator._mqtt_client = cast("object", mqtt)
    prefs = ManualPreferences(hass, "manual-tests", True)
    await prefs.async_load()
    coordinator.manual_preferences = prefs
    coordinator.data = coordinator._build_state_snapshot()
    return coordinator, mqtt, prefs


async def _ingress(coordinator: VivosunCoordinator, suffix: str, payload: dict[str, object]) -> None:
    await coordinator._handle_mqtt_publish(
        f"$aws/things/thing-1/shadow/{suffix}", json.dumps(payload).encode(), 1,
    )


async def test_store_v1_migrates_to_tagged_v2_and_rejects_invalid(hass: HomeAssistant) -> None:
    key = f"{DOMAIN}_manual_settings_legacy"
    await Store[dict[str, object]](hass, 1, key).async_save({"levels": {
        "device-1": {"light": 65, "cFan": 70, "dFan": True},
    }})
    prefs = ManualPreferences(hass, "legacy", True)
    await prefs.async_load()
    assert prefs.selection("device-1", "light") == 65
    assert prefs.selection("device-1", "cFan") == 70
    assert prefs.selection("device-1", "dFan") is None
    assert await Store[dict[str, object]](hass, 2, key).async_load() == {"settings": {
        "device-1": {
            "light": {"kind": "level", "value": 65},
            "cFan": {"kind": "level", "value": 70},
        },
    }}


async def test_explicit_selection_needs_matching_ack_and_no_startup_replay(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    try:
        await coordinator.async_publish_selected_fan_level(
            device_id="device-1", channel="cFan", shadow_level=200,
        )
        command = mqtt.published[-1]
        assert prefs.selection("device-1", "cFan") is None
        await _ingress(coordinator, "update/accepted", {
            "clientToken": command["clientToken"],
            "state": {"desired": {"cFan": {"manu": {"lv": 70}}}},
        })
        assert prefs.selection("device-1", "cFan") is None
        await _ingress(coordinator, "update/accepted", {
            "clientToken": command["clientToken"],
            "state": {"desired": {"cFan": {"manu": {"lv": 200}}}},
        })
        assert prefs.selection("device-1", "cFan") == "natural_wind"
        restored = ManualPreferences(hass, "manual-tests", True)
        await restored.async_load()
        assert restored.selection("device-1", "cFan") == "natural_wind"
        assert len(mqtt.published) == 1
    finally:
        await coordinator._disconnect_mqtt_client()


async def test_only_reported_manual_without_plan_allows_restore(hass: HomeAssistant) -> None:
    coordinator, mqtt, _prefs = await _setup(hass)
    try:
        await _ingress(coordinator, "get/accepted", {
            "version": 5, "state": {"desired": {"light": {"mode": 0, "inPlan": 0}}},
        })
        assert not coordinator.manual_restore_allowed("device-1", "light")
        await _ingress(coordinator, "get/accepted", {
            "version": 6, "state": {"reported": {"light": {"mode": 0, "inPlan": 0}}},
        })
        assert coordinator.manual_restore_allowed("device-1", "light")
        await _ingress(coordinator, "update/accepted", {
            "version": 7, "state": {"reported": {"light": {"mode": 1}}},
        })
        assert not coordinator.manual_restore_allowed("device-1", "light")
        await _ingress(coordinator, "get/accepted", {
            "version": 6, "state": {"reported": {"light": {"mode": 0}}},
        })
        assert not coordinator.manual_restore_allowed("device-1", "light")
        assert mqtt.published == []
    finally:
        await coordinator._disconnect_mqtt_client()


async def test_bare_on_restores_only_with_reported_manual_mode(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    try:
        await prefs.async_remember("device-1", "light", 65)
        await prefs.async_remember("device-1", "dFan", 60)
        await prefs.async_remember("device-1", "cFan", "natural_wind")
        entities = (
            (VivosunLightEntity(coordinator, "device-1"), "light", 65),
            (VivosunDuctFanEntity(coordinator, "device-1"), "dFan", 70),
            (VivosunCirculationFanEntity(coordinator, "device-1"), "cFan", 200),
        )
        await _ingress(coordinator, "get/accepted", {
            "version": 10, "state": {"reported": {
                channel: {"mode": 0, "inPlan": 0, "lv": 0} for _, channel, _ in entities
            }},
        })
        for entity, channel, raw_level in entities:
            await entity.async_turn_on()
            desired = mqtt.published[-1]["state"]["desired"]  # type: ignore[index]
            assert desired[channel]["manu"]["lv"] == raw_level
        assert len(mqtt.published) == 3
    finally:
        await coordinator._disconnect_mqtt_client()


async def test_failed_publish_does_not_train_memory(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    try:
        await coordinator.async_publish_selected_fan_level(
            device_id="device-1", channel="dFan", shadow_level=70,
        )
        selected = mqtt.published[-1]
        await _ingress(coordinator, "update/rejected", {
            "clientToken": selected["clientToken"], "code": 409,
        })
        await _ingress(coordinator, "update/accepted", {
            "clientToken": selected["clientToken"],
            "state": {"desired": {"dFan": {"manu": {"lv": 70}}}},
        })
        assert prefs.selection("device-1", "dFan") is None
    finally:
        await coordinator._disconnect_mqtt_client()

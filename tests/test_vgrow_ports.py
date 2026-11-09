"""VGrow USB power using sanitized #43 shadow capture shapes."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.switch import VivosunPortSwitch, async_setup_entry


async def _setup_port_reports(hass: object, *devices: DeviceInfo) -> tuple[VivosunCoordinator, list[object]]:
    coordinator = VivosunCoordinator(hass, object(), email="synthetic@example.test", password="unused")
    coordinator._devices = list(devices)
    coordinator._build_topic_maps()
    coordinator._mqtt_client = MagicMock(is_connected=True)
    coordinator.async_publish_shadow_update = AsyncMock()
    coordinator.data = coordinator._build_state_snapshot()
    entry = MockConfigEntry(domain=DOMAIN, title="Port devices", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry_id=entry.entry_id, coordinator=coordinator)
    entities: list[object] = []
    await async_setup_entry(hass, entry, entities.extend)
    return coordinator, entities


async def _deliver_port_report(coordinator: VivosunCoordinator, device: DeviceInfo, state: dict[str, object]) -> None:
    await coordinator._handle_mqtt_publish(
        f"$aws/things/{device.client_id}/shadow/update/accepted", json.dumps({"state": state}).encode(), 1
    )


async def test_vgrow_port_registration_preserves_model_and_port_boundaries(hass: object) -> None:
    def device(model: str, kind: str = "controller") -> DeviceInfo:
        return DeviceInfo(model.lower(), f"vivosun-{model}-account", f"topic/{model}", model, True, 0, kind)

    vgrow, lookalike = device("VSCTLY18"), device("VSCTLY19")
    a22, seed = device("VSPLUGA22", "a22"), device("VSSBS40", "vseed")
    coordinator, entities = await _setup_port_reports(hass, vgrow, lookalike, a22, seed)
    assert {entity.unique_id for entity in entities} == {
        *(f"vivosun_growhub_{vgrow.device_id}_{port}" for port in ("usb1", "usb2")),
        *(f"vivosun_growhub_{a22.device_id}_{port}" for port in ("usb1", "usb2", "plug1", "plug2")),
        f"vivosun_growhub_{seed.device_id}_seed_quick_irrigation",
        *(
            f"vivosun_growhub_{seed.device_id}_seed_stage{phase}_{key}"
            for phase in (1, 2, 3)
            for key in ("lgtEn", "pumpEn")
        ),
    }
    with pytest.raises(ValueError, match="Unsupported"):
        VivosunPortSwitch(coordinator, vgrow.device_id, "plug1", "VSCTLY18")
    with pytest.raises(ValueError, match="Unsupported"):
        VivosunPortSwitch(coordinator, lookalike.device_id, "usb1", "VSCTLY19")


async def test_vgrow_port_refuses_unsafe_manual_and_unknown_outputs(hass: object) -> None:
    device = DeviceInfo("vgrow-guard", "vivosun-VSCTLY18-guard", "topic/guard", "VGrow", True, 0, "controller")
    coordinator, entities = await _setup_port_reports(hass, device)
    usb1 = next(entity for entity in entities if entity.unique_id.endswith("_usb1"))
    await _deliver_port_report(coordinator, device, {"reported": {
        "usb1": {"lv": 100, "dev": True, "mode": 0, "inPlan": 0}, "connected": True
    }})
    assert usb1.is_on is True
    assert usb1.extra_state_attributes["accessory_code"] is None

    for change in ({"mode": 1}, {"mode": True}, {"mode": 0, "inPlan": 1}, {"inPlan": True}):
        await _deliver_port_report(coordinator, device, {"reported": {"usb1": change}})
        assert usb1.is_on is True and usb1.available
        with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
            await usb1.async_turn_off()
    coordinator.async_publish_shadow_update.assert_not_awaited()

    await _deliver_port_report(coordinator, device, {"reported": {"usb1": {"mode": 0, "inPlan": 0}}})
    await _deliver_port_report(coordinator, device, {"reported": {"connected": False}})
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await usb1.async_turn_off()
    await _deliver_port_report(coordinator, device, {"reported": {"connected": True}})
    coordinator._mqtt_client.is_connected = False
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await usb1.async_turn_off()
    coordinator._mqtt_client.is_connected = True

    for level in (50, True, "100"):
        await _deliver_port_report(coordinator, device, {"reported": {"usb1": {"lv": level}}})
        assert usb1.is_on is None and not usb1.available
        with pytest.raises(HomeAssistantError, match="no reported output level"):
            await usb1.async_turn_on()
    coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_vgrow_usb_capture_ingress_to_switches(hass: object) -> None:
    device = DeviceInfo("vgrow-1", "vivosun-VSCTLY18-account", "topic/vgrow", "VGrow", True, 42, "controller")
    coordinator = object.__new__(VivosunCoordinator)
    coordinator._device_listeners = []
    coordinator._garden_inventory = None
    coordinator._garden_listeners = []
    coordinator._devices = [device]
    coordinator._client_id_to_device_id = {device.client_id: device.device_id}
    coordinator._topic_prefix_to_device_id = {device.topic_prefix: device.device_id}
    coordinator._shadow_states = {}
    coordinator._pending_fields = {}
    coordinator._reported_fields = {}
    coordinator._has_reported_fields = set()
    coordinator._locally_commanded_fields = set()
    coordinator._sensor_states = {}
    coordinator._plan_stage_cache = {}
    coordinator._tokens = None
    coordinator._support_capture = type("Capture", (), {"record": lambda *a, **kw: None})()
    coordinator.async_update_listeners = lambda: None
    coordinator._refresh_plan_stages = AsyncMock()
    coordinator.async_publish_shadow_update = AsyncMock()
    coordinator._mqtt_client = type("ConnectedMqtt", (), {"is_connected": True})()
    coordinator.data = coordinator._build_state_snapshot()
    entry = MockConfigEntry(domain=DOMAIN, title="VGrow", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry_id=entry.entry_id, coordinator=coordinator)
    switches = []
    await async_setup_entry(hass, entry, switches.extend)
    assert {switch.unique_id for switch in switches} == {
        "vivosun_growhub_vgrow-1_usb1", "vivosun_growhub_vgrow-1_usb2"
    }
    usb1, usb2 = sorted(switches, key=lambda switch: switch.unique_id)
    assert usb1.available is False

    topic = f"$aws/things/{device.client_id}/shadow/update/accepted"

    async def deliver(state: dict[str, object], *, documents: bool = False) -> None:
        path = topic.replace("/accepted", "/documents") if documents else topic
        data = {"current": {"state": state}} if documents else {"state": state}
        await coordinator._handle_mqtt_publish(path, json.dumps(data).encode(), 1)

    await deliver({"desired": {"usb1": {"manu": {"lv": 100}}}})
    assert usb1.is_on is None
    await deliver({"reported": {
        "usb1": {"dev": 512, "mode": 0, "inPlan": 0, "lv": 0, "manu": {"lv": 0}},
        "usb2": {"dev": 502, "mode": 0, "inPlan": 0, "lv": 100, "manu": {"lv": 100}},
    }})
    assert (usb1.is_on, usb2.is_on) == (False, True)
    assert usb1.available and usb2.available
    assert usb1.extra_state_attributes == {"accessory_code": 512, "mode_code": 0, "in_plan": 0}
    await usb1.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"usb1": {"manu": {"lv": 100}}}}}, device_id="vgrow-1", qos=1
    )
    coordinator.async_publish_shadow_update.reset_mock()
    await usb2.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"usb2": {"manu": {"lv": 0}}}}}, device_id="vgrow-1", qos=1
    )
    coordinator.async_publish_shadow_update.reset_mock()

    await deliver({"reported": {"usb1": {"dev": 507, "lv": 100}, "usb2": {"dev": 503}}}, documents=True)
    assert (usb1.is_on, usb2.is_on) == (True, True)
    assert usb1.extra_state_attributes["accessory_code"] == 507
    assert usb2.extra_state_attributes["accessory_code"] == 503
    assert usb1.unique_id == "vivosun_growhub_vgrow-1_usb1"
    await deliver({"desired": {"usb1": {"mode": 0, "manu": {"lv": 0}}}})
    assert usb1.is_on is True

    for change in ({"mode": 1}, {"inPlan": 1}, {"inPlan": 0, "mode": 1}, {"mode": 0, "inPlan": 1}):
        await deliver({"reported": {"usb1": change}})
        with pytest.raises(ServiceValidationError):
            await usb1.async_turn_off()
    await deliver({"reported": {"usb1": {"mode": 0, "inPlan": 0, "lv": 25}}})
    assert usb1.is_on is None and not usb1.available
    with pytest.raises(HomeAssistantError):
        await usb1.async_turn_on()
    coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_vgrow_usb_exact_model_only(hass: object) -> None:
    device = DeviceInfo("other-1", "vivosun-VSCTLY19-account", "topic/other", "Other", True, 42, "controller")
    coordinator = object.__new__(VivosunCoordinator)
    coordinator._device_listeners = []
    coordinator._devices = [device]
    coordinator.data = {}
    entry = MockConfigEntry(domain=DOMAIN, title="Other", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry_id=entry.entry_id, coordinator=coordinator)
    entities = []
    await async_setup_entry(hass, entry, entities.extend)
    assert entities == []

"""A22 reported port behavior from sanitized, capture-shaped MQTT traffic."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.model_metadata import support_capture_model_metadata
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData, infer_device_type
from custom_components.vivosun_growhub.ports import build_manual_port_payload, reported_port
from custom_components.vivosun_growhub.sensor import async_setup_entry as setup_sensors
from custom_components.vivosun_growhub.shadow import parse_shadow_document
from custom_components.vivosun_growhub.switch import async_setup_entry


def test_port_parser_and_builder() -> None:
    assert build_manual_port_payload("plug1", True) == {"state": {"desired": {"plug1": {"manu": {"lv": 100}}}}}
    assert build_manual_port_payload("usb2", False) == {"state": {"desired": {"usb2": {"manu": {"lv": 0}}}}}
    with pytest.raises(ValueError):
        build_manual_port_payload("cFan", True)
    planned_off = reported_port({"mode": 0, "inPlan": 1, "lv": 0, "manu": {"lv": 0}, "dev": 501})
    assert (planned_off.is_on, planned_off.can_write, planned_off.accessory_code) == (False, False, 501)

    for raw, expected_on, expected_writable in (
        ({"mode": 1, "inPlan": 1, "lv": 100, "manu": {"lv": 0}}, True, False),
        ({"mode": 0, "inPlan": 0, "lv": 100, "manu": {"lv": 0}}, True, True),
        ({"mode": 0, "inPlan": 0, "lv": 0, "manu": {"lv": 100}}, False, True),
        ({"mode": 0, "lv": 100}, True, False),
        ({"inPlan": 0, "lv": 0}, False, False),
        ({"mode": 0, "inPlan": 0, "manu": {"lv": 100}}, None, True),
        ({"mode": 0, "inPlan": 0, "lv": 50}, None, True),
        ({"mode": 0, "inPlan": 0, "lv": True}, None, True),
        ({"mode": 0, "inPlan": 0, "lv": "100"}, None, True),
        ({"mode": 1, "inPlan": 0, "lv": 100}, True, False),
        ({"mode": 0, "inPlan": 1, "lv": 100}, True, False),
    ):
        report = reported_port(raw)
        assert (report.is_on, report.can_write) == (expected_on, expected_writable)

    assert reported_port(None).is_on is None
    assert reported_port({"mode": True, "inPlan": False, "lv": 0, "dev": True}).can_write is False
    assert reported_port({"dev": True}).accessory_code is None


async def test_a22_actual_ingress_and_physical_port_entities(hass: object) -> None:
    from custom_components.vivosun_growhub.coordinator import VivosunCoordinator

    device = DeviceInfo("a22-1", "vivosun-VSPLUGA22-account-a22", "topic/a22", "GrowHub A22", True, 42, "a22")
    assert infer_device_type("GrowHub A22", device.client_id) == "a22"
    coordinator = object.__new__(VivosunCoordinator)
    coordinator._devices = [device]
    coordinator._client_id_to_device_id = {device.client_id: device.device_id}
    coordinator._topic_prefix_to_device_id = {device.topic_prefix: device.device_id}
    coordinator._shadow_states = {}
    coordinator._pending_fields = {}
    coordinator._sensor_states = {}
    coordinator._plan_stage_cache = {}
    coordinator._tokens = None
    coordinator._support_capture = type("Capture", (), {"record": lambda *a, **kw: None})()
    coordinator.async_update_listeners = lambda: None
    coordinator._refresh_plan_stages = AsyncMock()
    coordinator.async_publish_shadow_update = AsyncMock()
    coordinator._mqtt_client = type("ConnectedMqtt", (), {"is_connected": True})()
    coordinator.data = coordinator._build_state_snapshot()
    entry = MockConfigEntry(domain=DOMAIN, title="a22", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry_id=entry.entry_id, coordinator=coordinator)
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert {entity.unique_id for entity in added} == {
        f"vivosun_growhub_a22-1_{key}" for key in ("usb1", "usb2", "plug1", "plug2")
    }
    plug = next(item for item in added if item.unique_id.endswith("_plug1"))
    sensors = []
    await setup_sensors(hass, entry, sensors.extend)
    assert {sensor.unique_id for sensor in sensors} == {
        "vivosun_growhub_a22-1_pTemp",
        "vivosun_growhub_a22-1_pHumi",
        "vivosun_growhub_a22-1_pVpd",
    }
    assert all(sensor.native_value is None for sensor in sensors)
    coordinator._merge_sensor_state(device.device_id, {"pTemp": 2250, "pHumi": 5840, "pVpd": 123})
    coordinator.data = coordinator._build_state_snapshot()
    assert {sensor.name: sensor.native_value for sensor in sensors} == {
        "Probe Temperature": 22.5,
        "Probe Humidity": 58.4,
        "Probe VPD": 1.23,
    }
    topic = f"$aws/things/{device.client_id}/shadow/update/accepted"

    async def deliver(document: dict[str, object], incoming_topic: str = topic) -> None:
        await coordinator._handle_mqtt_publish(incoming_topic, json.dumps(document).encode(), 1)

    usb1 = next(item for item in added if item.unique_id.endswith("_usb1"))
    usb2 = next(item for item in added if item.unique_id.endswith("_usb2"))
    assert plug.is_on is None
    assert plug.available is False
    assert usb2.is_on is None
    assert usb2.available is False
    await deliver({"state": {"desired": {"plug1": {"mode": 0, "manu": {"lv": 100}}}}})
    assert plug.is_on is None
    assert plug.available is False

    # An OFF outlet under an active plan still has a known physical output state.
    # USB1 reports its output, but no inPlan status yet.
    await deliver({"state": {"reported": {
        "plug1": {"mode": 0, "inPlan": 1, "lv": 0, "manu": {"lv": 0}, "dev": 501},
        "usb1": {"mode": 0, "lv": 100, "manu": {"lv": 0}, "dev": 0},
    }}})
    assert plug.is_on is False
    assert plug.available is True
    assert plug.extra_state_attributes == {"accessory_code": 501, "mode_code": 0, "in_plan": 1}
    assert usb1.is_on is True
    assert usb1.available is True
    with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
        await plug.async_turn_on()
    with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
        await usb1.async_turn_off()
    coordinator.async_publish_shadow_update.assert_not_awaited()

    # Later partial reports can update root lv without changing stored manu.lv.
    await deliver({"current": {"state": {"reported": {"plug1": {"lv": 100}}}}},
                  f"$aws/things/{device.client_id}/shadow/update/documents")
    assert plug.is_on is True
    assert plug.available is True
    assert plug.extra_state_attributes["accessory_code"] == 501
    assert plug.extra_state_attributes["in_plan"] == 1
    assert coordinator.data["shadows"]["a22-1"]["ports"]["plug1"]["manu"]["lv"] == 0

    await deliver({"state": {"reported": {"plug1": {"dev": 0}}}})
    assert plug.is_on is True
    assert plug.extra_state_attributes == {"accessory_code": 0, "mode_code": 0, "in_plan": 1}
    assert plug.unique_id == "vivosun_growhub_a22-1_plug1"
    await deliver({"state": {"desired": {"plug1": {"manu": {"lv": 0}}}}})
    assert plug.is_on is True
    assert plug.extra_state_attributes["accessory_code"] == 0
    with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
        await plug.async_turn_off()
    coordinator.async_publish_shadow_update.assert_not_awaited()

    await deliver({"state": {"reported": {"plug1": {"mode": 1, "lv": 100, "manu": {"lv": 0}}}}})
    assert plug.is_on is True
    assert plug.available is True
    assert plug.extra_state_attributes["mode_code"] == 1
    with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
        await plug.async_turn_off()
    coordinator.async_publish_shadow_update.assert_not_awaited()

    await deliver({"state": {"reported": {"plug1": {"mode": 0, "inPlan": 0}}}})
    await plug.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_manual_port_payload("plug1", False), device_id="a22-1", qos=1
    )
    coordinator.async_publish_shadow_update.reset_mock()
    await deliver({"state": {"reported": {"usb1": {"inPlan": 0}}}})
    await usb1.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_manual_port_payload("usb1", False), device_id="a22-1", qos=1
    )
    await deliver({"state": {"reported": {"plug1": {"lv": 0}}}})
    assert plug.is_on is False
    assert plug.available is True
    assert plug.extra_state_attributes == {"accessory_code": 0, "mode_code": 0, "in_plan": 0}

    await deliver({"state": {"reported": {"plug1": {"mode": 1, "inPlan": 1, "lv": 100}}}})
    assert plug.is_on is True
    assert plug.available is True
    with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
        await plug.async_turn_off()

    await deliver({"state": {"reported": {"connected": False}}})
    assert plug.is_on is True
    assert plug.available is False
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await plug.async_turn_off()
    await deliver({"state": {"reported": {"connected": True}}})
    assert plug.available is True
    coordinator._mqtt_client.is_connected = False
    assert plug.available is False
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await plug.async_turn_off()
    coordinator._mqtt_client.is_connected = True

    await deliver({"state": {"reported": {"plug1": {"lv": 50}}}})
    assert plug.is_on is None
    assert plug.available is False
    with pytest.raises(HomeAssistantError, match="no reported output level"):
        await plug.async_turn_on()
    await deliver({"state": {"reported": {"plug1": {"lv": 100}}}})
    assert plug.is_on is True
    assert plug.available is True

    await deliver({"state": {"reported": {"plug1": {"lv": 0, "manu": {"lv": 0}}}}}, "not/our/topic")
    assert plug.is_on is True
    await deliver({"state": {"reported": {"plug1": {"lv": 0}}}},
                  f"$aws/things/{device.client_id}/shadow/update/rejected")
    assert plug.is_on is True


def test_shadow_port_requires_real_report() -> None:
    assert "ports" not in parse_shadow_document({"state": {"desired": {"usb1": {"lv": 100}}}})
    parsed = parse_shadow_document({"state": {"reported": {"usb1": {"mode": 0, "lv": 100}}}})
    assert parsed["ports"]["usb1"]["lv"] == 100


def test_a22_metadata_is_model_specific() -> None:
    metadata = support_capture_model_metadata("vivosun-VSPLUGA22-acct-id")
    assert metadata["device_type"] == "a22"
    assert metadata["data_upload_groups"][0]["data_keys"] == ["pTemp", "pHumi", "pVpd"]
    assert infer_device_type("Unknown device", "vivosun-VSCTLE42A-acct-id") == "controller"

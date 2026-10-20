"""VGrow USB power from sanitized MQTT shadow reports."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.switch import VivosunPortSwitch, async_setup_entry


def _device(model: str, *, device_type: str = "controller") -> DeviceInfo:
    return DeviceInfo(model.lower(), f"vivosun-{model}-account", f"topic/{model}", model, True, 0, device_type)


async def _setup(hass: object, *devices: DeviceInfo) -> tuple[VivosunCoordinator, list[object]]:
    coordinator = VivosunCoordinator(hass, object(), email="synthetic@example.test", password="unused")  # type: ignore[arg-type]
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


async def _deliver(
    coordinator: VivosunCoordinator, device: DeviceInfo, state: dict[str, object], *, documents: bool = False
) -> None:
    suffix = "update/documents" if documents else "update/accepted"
    payload = {"current": {"state": state}} if documents else {"state": state}
    await coordinator._handle_mqtt_publish(
        f"$aws/things/{device.client_id}/shadow/{suffix}", json.dumps(payload).encode(), 1
    )


async def test_vgrow_usb_registration_has_exact_model_and_port_boundaries(hass: object) -> None:
    vgrow, lookalike = _device("VSCTLY18"), _device("VSCTLY19")
    a22, seed = _device("VSPLUGA22", device_type="a22"), _device("VSSBS40", device_type="vseed")
    coordinator, entities = await _setup(hass, vgrow, lookalike, a22, seed)
    ids = {entity.unique_id for entity in entities}
    assert ids == {
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


async def test_vgrow_usb_reported_power_is_not_stored_manual_level(hass: object) -> None:
    device = _device("VSCTLY18")
    coordinator, entities = await _setup(hass, device)
    usb1, usb2 = sorted(entities, key=lambda entity: entity.unique_id)
    assert usb1.unique_id == f"vivosun_growhub_{device.device_id}_usb1"
    assert usb2.unique_id == f"vivosun_growhub_{device.device_id}_usb2"
    assert usb1.is_on is None and usb1.available is False

    await _deliver(coordinator, device, {"desired": {"usb1": {"lv": 100, "manu": {"lv": 100}}}})
    assert usb1.is_on is None and usb1.available is False

    await _deliver(
        coordinator,
        device,
        {
            "reported": {
                "usb1": {"mode": 0, "inPlan": 0, "lv": 0, "manu": {"lv": 100}, "dev": 512},
                "usb2": {"mode": 0, "inPlan": 0, "lv": 100, "manu": {"lv": 0}, "dev": 502},
            }
        },
    )
    assert (usb1.is_on, usb2.is_on) == (False, True)
    assert usb1.available and usb2.available
    assert usb1.extra_state_attributes == {"accessory_code": 512, "mode_code": 0, "in_plan": 0}
    await usb1.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"usb1": {"manu": {"lv": 100}}}}}, device_id=device.device_id, qos=1
    )
    coordinator.async_publish_shadow_update.reset_mock()
    await usb2.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"usb2": {"manu": {"lv": 0}}}}}, device_id=device.device_id, qos=1
    )
    coordinator.async_publish_shadow_update.reset_mock()

    await _deliver(
        coordinator,
        device,
        {
            "reported": {
                "usb1": {"lv": 100, "dev": 507},
                "usb2": {"dev": 503},
            }
        },
        documents=True,
    )
    assert (usb1.is_on, usb2.is_on) == (True, True)
    assert (usb1.extra_state_attributes["accessory_code"], usb2.extra_state_attributes["accessory_code"]) == (
        507,
        503,
    )
    assert usb1.unique_id == f"vivosun_growhub_{device.device_id}_usb1"
    await _deliver(coordinator, device, {"desired": {"usb1": {"lv": 0, "manu": {"lv": 0}}}})
    assert usb1.is_on is True


async def test_vgrow_usb_manual_guard_preserves_reported_state(hass: object) -> None:
    device = _device("VSCTLY18")
    coordinator, entities = await _setup(hass, device)
    usb1 = next(entity for entity in entities if entity.unique_id.endswith("_usb1"))
    await _deliver(coordinator, device, {"reported": {"usb1": {"lv": 100, "dev": True}}})
    assert usb1.is_on is True
    assert usb1.extra_state_attributes["accessory_code"] is None
    with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
        await usb1.async_turn_off()

    await _deliver(coordinator, device, {"reported": {"usb1": {"mode": 0}}})
    with pytest.raises(ServiceValidationError):
        await usb1.async_turn_off()
    await _deliver(coordinator, device, {"reported": {"usb1": {"inPlan": 0}}})

    for change in ({"mode": 1}, {"mode": True}, {"mode": 0, "inPlan": 1}, {"inPlan": True}):
        await _deliver(coordinator, device, {"reported": {"usb1": change}})
        assert usb1.is_on is True and usb1.available
        with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
            await usb1.async_turn_off()
    coordinator.async_publish_shadow_update.assert_not_awaited()

    await _deliver(coordinator, device, {"reported": {"usb1": {"mode": 0, "inPlan": 0}}})
    await _deliver(coordinator, device, {"reported": {"connected": False}})
    assert usb1.available is False
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await usb1.async_turn_off()
    await _deliver(coordinator, device, {"reported": {"connected": True}})
    coordinator._mqtt_client.is_connected = False
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await usb1.async_turn_off()
    coordinator._mqtt_client.is_connected = True

    for lv in (50, True, "100"):
        await _deliver(coordinator, device, {"reported": {"usb1": {"lv": lv}}})
        assert usb1.is_on is None and usb1.available is False
        with pytest.raises(HomeAssistantError, match="no reported output level"):
            await usb1.async_turn_on()
    coordinator.async_publish_shadow_update.assert_not_awaited()

    await _deliver(coordinator, device, {"reported": {"usb1": {"lv": 0}}})
    await usb1.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"usb1": {"manu": {"lv": 100}}}}}, device_id=device.device_id, qos=1
    )

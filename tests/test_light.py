"""Tests for Vivosun light platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from homeassistant.components.light import ATTR_BRIGHTNESS
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.light import VivosunLightEntity, async_setup_entry
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.shadow import build_light_level_payload, build_light_spectrum_payload

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_DEV_ID = "dev-1"
_DEV_ID_2 = "dev-2"


def _controller_device(device_id: str) -> DeviceInfo:
    return DeviceInfo(
        device_id=device_id,
        client_id=f"vivosun-VSCTLE42A-acc-{device_id}",
        topic_prefix=f"prefix/{device_id}",
        name=f"GrowHub {device_id}",
        online=True,
        scene_id=66078,
        device_type="controller",
    )


class _StubCoordinator:
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self.data: dict[str, object] = {}
        self._devices = devices or [_controller_device(_DEV_ID)]
        self.is_mqtt_connected = True
        self.async_publish_shadow_update = AsyncMock()
        self.async_publish_light_level = AsyncMock()
        self.manual_memory_enabled = lambda device_id, channel: False
        self.manual_restore_allowed = lambda device_id, channel: False

    @property
    def device(self) -> DeviceInfo:
        return self._devices[0]

    @property
    def devices(self) -> list[DeviceInfo]:
        return self._devices

    def get_device(self, device_id: str) -> DeviceInfo | None:
        for device in self._devices:
            if device_id == device.device_id:
                return device
        return None


def _make_light(coordinator: _StubCoordinator) -> VivosunLightEntity:
    return VivosunLightEntity(cast("object", coordinator), _DEV_ID)


async def test_light_setup_creates_one_entity(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunLightEntity] = []

    def _add(entities: list[VivosunLightEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 1
    assert added[0].unique_id == f"vivosun_growhub_{_DEV_ID}_light"


async def test_light_setup_creates_entity_for_each_controller(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator([_controller_device(_DEV_ID), _controller_device(_DEV_ID_2)])
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunLightEntity] = []

    def _add(entities: list[VivosunLightEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 2
    assert {entity.unique_id for entity in added} == {
        f"vivosun_growhub_{_DEV_ID}_light",
        f"vivosun_growhub_{_DEV_ID_2}_light",
    }


async def test_light_state_mapping_and_availability() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadows": {
            _DEV_ID: {
                "light": {"level": 50, "mode": 0, "spectrum": 20},
                "connection": {"connected": True},
            }
        }
    }
    entity = _make_light(coordinator)

    assert entity.is_on is True
    assert entity.brightness == 128
    assert entity.extra_state_attributes == {"mode": 0, "spectrum": 20}
    assert entity.available is True
    assert entity.device_info["model"] == "VSCTLE42A"

    coordinator.is_mqtt_connected = False
    assert entity.available is False


async def test_light_turn_on_and_off_publishes_shadow_payloads() -> None:
    coordinator = _StubCoordinator()
    entity = _make_light(coordinator)

    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 255})
    coordinator.async_publish_light_level.assert_awaited_once_with(
        100, device_id=_DEV_ID
    )

    coordinator.async_publish_light_level.reset_mock()
    coordinator.data = {"shadows": {_DEV_ID: {"light": {"level": 0}}}}
    await entity.async_turn_on()
    coordinator.async_publish_light_level.assert_awaited_once_with(
        25, device_id=_DEV_ID
    )

    coordinator.async_publish_light_level.reset_mock()
    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 0, "spectrum": 40})
    coordinator.async_publish_light_level.assert_awaited_once_with(0, device_id=_DEV_ID)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_light_spectrum_payload(40), device_id=_DEV_ID,
    )

    coordinator.async_publish_light_level.reset_mock()
    await entity.async_turn_off()
    coordinator.async_publish_light_level.assert_awaited_once_with(
        0, device_id=_DEV_ID
    )


async def test_light_turn_on_clamps_nonzero_levels_to_device_minimum() -> None:
    coordinator = _StubCoordinator()
    entity = _make_light(coordinator)

    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 10})
    coordinator.async_publish_light_level.assert_awaited_once_with(
        4, device_id=_DEV_ID
    )
    assert build_light_level_payload(4) == build_light_level_payload(25)


async def test_light_brightness_boundaries() -> None:
    coordinator = _StubCoordinator()
    entity = _make_light(coordinator)

    coordinator.data = {"shadows": {_DEV_ID: {"light": {"level": 0}}}}
    assert entity.brightness == 0

    coordinator.data = {"shadows": {_DEV_ID: {"light": {"level": 1}}}}
    assert entity.brightness == 3

    coordinator.data = {"shadows": {_DEV_ID: {"light": {"level": 50}}}}
    assert entity.brightness == 128

    coordinator.data = {"shadows": {_DEV_ID: {"light": {"level": 100}}}}
    assert entity.brightness == 255

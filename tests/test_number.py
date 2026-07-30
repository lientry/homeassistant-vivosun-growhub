"""Tests for the Vivosun number platform (humidifier mist level)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.number import (
    VivosunHumidifierMistLevel,
    async_setup_entry,
)
from custom_components.vivosun_growhub.shadow import build_hmdf_level_payload

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity

    from custom_components.vivosun_growhub.coordinator import VivosunCoordinator

_HUMIDIFIER_DEV_ID = "humidifier-1"


def _humidifier_device() -> DeviceInfo:
    return DeviceInfo(
        device_id=_HUMIDIFIER_DEV_ID,
        client_id="vivosun-VSHMDH19-acc-humidifier-1",
        topic_prefix="prefix/humidifier",
        name="AeroStream H19",
        online=True,
        scene_id=66078,
        device_type="humidifier",
    )


class _StubCoordinator:
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self.data: dict[str, object] = {}
        self._devices = devices or [_humidifier_device()]
        self.is_mqtt_connected = True
        self.async_publish_shadow_update = AsyncMock()

    @property
    def devices(self) -> list[DeviceInfo]:
        return self._devices

    def get_device(self, device_id: str) -> DeviceInfo | None:
        for device in self._devices:
            if device_id == device.device_id:
                return device
        return None


def _make_entity(coordinator: _StubCoordinator) -> VivosunHumidifierMistLevel:
    return VivosunHumidifierMistLevel(cast("VivosunCoordinator", coordinator), _HUMIDIFIER_DEV_ID)


async def test_number_setup_creates_one_entity_per_humidifier(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("VivosunCoordinator", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[Entity] = []

    def _add(entities: Iterable[Entity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 1
    assert isinstance(added[0], VivosunHumidifierMistLevel)


async def test_mist_level_native_value_prefers_manual_setpoint() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadows": {
            _HUMIDIFIER_DEV_ID: {
                "hmdf": {"manual_level": 70, "level": 0},
                "connection": {"connected": True},
            }
        }
    }
    entity = _make_entity(coordinator)

    # Prefers the stored manual setpoint (manu.lv 70 -> app step 3).
    assert entity.native_value == 3.0
    assert entity.available is True


async def test_mist_level_native_value_falls_back_to_active_level() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_HUMIDIFIER_DEV_ID: {"hmdf": {"level": 35}}}}
    entity = _make_entity(coordinator)

    # manu.lv 35 -> app step 1
    assert entity.native_value == 1.0


async def test_mist_level_native_value_none_when_absent() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_HUMIDIFIER_DEV_ID: {"hmdf": {}}}}
    entity = _make_entity(coordinator)

    assert entity.native_value is None


async def test_mist_level_set_publishes_expected_payload() -> None:
    coordinator = _StubCoordinator()
    entity = _make_entity(coordinator)

    await entity.async_set_native_value(3)
    # App step 3 maps to device manu.lv 70.
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_hmdf_level_payload(70), device_id=_HUMIDIFIER_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_native_value(0)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_hmdf_level_payload(0), device_id=_HUMIDIFIER_DEV_ID
    )

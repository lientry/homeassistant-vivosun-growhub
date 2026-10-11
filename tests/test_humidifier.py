"""Tests for Vivosun humidifier platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN, MODE_AUTO, MODE_MANUAL
from custom_components.vivosun_growhub.humidifier import (
    VivosunDehumidifierEntity,
    VivosunHumidifierEntity,
    async_setup_entry,
)
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.shadow import (
    build_dhmdf_on_payload,
    build_dhmdf_target_payload,
    build_hmdf_mode_payload,
    build_hmdf_on_payload,
    build_hmdf_target_payload,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity

    from custom_components.vivosun_growhub.coordinator import VivosunCoordinator

_HUMIDIFIER_DEV_ID = "humidifier-1"
_DEHUMIDIFIER_DEV_ID = "dehumidifier-1"


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


def _dehumidifier_device() -> DeviceInfo:
    return DeviceInfo(
        device_id=_DEHUMIDIFIER_DEV_ID,
        client_id="vivosun-VSDRYD12-acc-dehumidifier-1",
        topic_prefix="prefix/dehumidifier",
        name="AeroDrain D12",
        online=True,
        scene_id=66079,
        device_type="dehumidifier",
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


def _make_entity(coordinator: _StubCoordinator) -> VivosunHumidifierEntity:
    return VivosunHumidifierEntity(cast("VivosunCoordinator", coordinator), _HUMIDIFIER_DEV_ID)


def _make_dehumidifier_entity(coordinator: _StubCoordinator) -> VivosunDehumidifierEntity:
    return VivosunDehumidifierEntity(cast("VivosunCoordinator", coordinator), _DEHUMIDIFIER_DEV_ID)


async def test_humidifier_setup_creates_one_entity(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("VivosunCoordinator", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunHumidifierEntity] = []

    def _add(new_entities: Iterable[Entity], update_before_add: bool = False) -> None:
        _ = update_before_add
        added.extend(cast("Iterable[VivosunHumidifierEntity]", new_entities))

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 1
    assert added[0].unique_id == f"vivosun_growhub_{_HUMIDIFIER_DEV_ID}_humidifier"


async def test_dehumidifier_setup_creates_one_entity(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator([_dehumidifier_device()])
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("VivosunCoordinator", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[Entity] = []

    def _add(new_entities: Iterable[Entity], update_before_add: bool = False) -> None:
        _ = update_before_add
        added.extend(new_entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 1
    assert added[0].unique_id == f"vivosun_growhub_{_DEHUMIDIFIER_DEV_ID}_dehumidifier"


async def test_humidifier_state_mapping_and_availability() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadows": {
            _HUMIDIFIER_DEV_ID: {
                "hmdf": {
                    "on": True,
                    "target_humidity": 5500,
                    "mode": MODE_AUTO,
                    "level": 3,
                    "water_warning": False,
                },
                "connection": {"connected": True},
            }
        },
        "sensors": {_HUMIDIFIER_DEV_ID: {"pHumi": 4875}},
    }
    entity = _make_entity(coordinator)

    assert entity.is_on is True
    assert entity.target_humidity == 55.0
    assert entity.current_humidity == 48.75
    assert entity.mode == "auto"
    assert entity.extra_state_attributes == {"level": 3, "water_warning": False}
    assert entity.available is True
    assert entity.device_info.get("model") == "VSHMDH19"

    coordinator.is_mqtt_connected = False
    assert entity.available is False


async def test_humidifier_commands_publish_expected_shadow_payloads() -> None:
    coordinator = _StubCoordinator()
    entity = _make_entity(coordinator)

    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_hmdf_on_payload(True), device_id=_HUMIDIFIER_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_hmdf_on_payload(False), device_id=_HUMIDIFIER_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_humidity(55)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_hmdf_target_payload(5500), device_id=_HUMIDIFIER_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_mode("auto")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_hmdf_mode_payload(MODE_AUTO), device_id=_HUMIDIFIER_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_mode("manual")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_hmdf_mode_payload(MODE_MANUAL), device_id=_HUMIDIFIER_DEV_ID
    )


async def test_dehumidifier_state_mapping_and_availability() -> None:
    coordinator = _StubCoordinator([_dehumidifier_device()])
    coordinator.data = {
        "shadows": {
            _DEHUMIDIFIER_DEV_ID: {
                "dhmdf": {
                    "on": True,
                    "target_humidity": 6000,
                    "pause": 0,
                },
                "connection": {"connected": True},
            }
        },
        "sensors": {_DEHUMIDIFIER_DEV_ID: {"pHumi": 5125}},
    }
    entity = _make_dehumidifier_entity(coordinator)

    assert entity.is_on is True
    assert entity.target_humidity == 60.0
    assert entity.current_humidity == 51.25
    assert entity.available is True
    assert entity.device_info.get("model") == "VSDRYD12"

    coordinator.is_mqtt_connected = False
    assert entity.available is False


async def test_dehumidifier_commands_publish_expected_shadow_payloads() -> None:
    coordinator = _StubCoordinator([_dehumidifier_device()])
    entity = _make_dehumidifier_entity(coordinator)

    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dhmdf_on_payload(True), device_id=_DEHUMIDIFIER_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dhmdf_on_payload(False), device_id=_DEHUMIDIFIER_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_humidity(60)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dhmdf_target_payload(6000), device_id=_DEHUMIDIFIER_DEV_ID
    )


async def test_humidifier_invalid_mode_does_not_publish() -> None:
    coordinator = _StubCoordinator()
    entity = _make_entity(coordinator)

    await entity.async_set_mode("invalid")

    coordinator.async_publish_shadow_update.assert_not_awaited()

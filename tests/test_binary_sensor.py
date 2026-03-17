"""Tests for Vivosun binary sensor platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.binary_sensor import VivosunConnectionBinarySensorEntity, async_setup_entry
from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_DEV_ID = "dev-1"


class _StubCoordinator:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self._device = DeviceInfo(
            device_id=_DEV_ID,
            client_id="vivosun-VSCTLE42A-acc-dev-1",
            topic_prefix="prefix",
            name="GrowHub",
            online=True,
            scene_id=66078,
            device_type="controller",
        )
        self.is_mqtt_connected = True

    @property
    def device(self) -> DeviceInfo:
        return self._device

    @property
    def devices(self) -> list[DeviceInfo]:
        return [self._device]

    def get_device(self, device_id: str) -> DeviceInfo | None:
        if device_id == self._device.device_id:
            return self._device
        return None


def _make_entity(coordinator: _StubCoordinator) -> VivosunConnectionBinarySensorEntity:
    return VivosunConnectionBinarySensorEntity(cast("object", coordinator), _DEV_ID)


async def test_binary_sensor_setup_creates_one_entity(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunConnectionBinarySensorEntity] = []

    def _add(entities: list[VivosunConnectionBinarySensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 1
    assert added[0].unique_id == f"vivosun_growhub_{_DEV_ID}_connected"


async def test_binary_sensor_connected_mapping_and_device_class() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_DEV_ID: {"connection": {"connected": True}}}}
    entity = _make_entity(coordinator)

    assert entity.device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert entity.is_on is True
    assert entity.available is True

    coordinator.data = {"shadows": {_DEV_ID: {"connection": {"connected": False}}}}
    assert entity.is_on is False
    assert entity.available is True


async def test_binary_sensor_missing_connected_maps_unknown_and_availability_behavior() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_DEV_ID: {"connection": {}}}}
    entity = _make_entity(coordinator)

    assert entity.is_on is None
    assert entity.available is True

    coordinator.is_mqtt_connected = False
    assert entity.available is False

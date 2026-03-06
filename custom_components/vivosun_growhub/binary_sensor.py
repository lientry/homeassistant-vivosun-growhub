"""Binary sensor platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun binary sensor entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    async_add_entities([VivosunConnectionBinarySensorEntity(coordinator)])


class VivosunConnectionBinarySensorEntity(CoordinatorEntity[VivosunCoordinator], BinarySensorEntity):  # type: ignore[misc]
    """Representation of GrowHub cloud connected state."""

    _attr_has_entity_name = True
    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: VivosunCoordinator) -> None:
        """Initialize the connectivity entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"vivosun_growhub_{coordinator.device.device_id}_connected"

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return is_entity_available(self.coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        """Return shared device info for this entity."""
        return build_device_info(self.coordinator)

    @property
    def is_on(self) -> bool | None:
        """Return True when GrowHub reports connected, False when disconnected."""
        data = self.coordinator.data
        if not isinstance(data, Mapping):
            return None

        shadow = data.get("shadow")
        if not isinstance(shadow, Mapping):
            return None

        connection = shadow.get("connection")
        if not isinstance(connection, Mapping):
            return None

        connected = connection.get("connected")
        if isinstance(connected, bool):
            return connected
        return None

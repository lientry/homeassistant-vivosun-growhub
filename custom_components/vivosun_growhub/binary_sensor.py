"""Binary sensor platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, shadow_slice

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

    entities = [
        VivosunConnectionBinarySensorEntity(coordinator, device.device_id)
        for device in coordinator.devices
    ]
    async_add_entities(entities)


class VivosunConnectionBinarySensorEntity(CoordinatorEntity[VivosunCoordinator], BinarySensorEntity):  # type: ignore[misc]
    """Representation of a Vivosun device cloud connected state."""

    _attr_has_entity_name = True
    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        """Initialize the connectivity entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_connected"

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return bool(self.coordinator.is_mqtt_connected)

    @property
    def device_info(self) -> DeviceInfo:
        """Return shared device info for this entity."""
        return build_device_info(self.coordinator, self._device_id)

    @property
    def is_on(self) -> bool | None:
        """Return True when the device reports connected, False when disconnected."""
        connection = shadow_slice(self.coordinator, self._device_id, "connection")
        if not connection:
            return None
        connected = connection.get("connected")
        if isinstance(connected, bool):
            return connected
        return None

"""Native A22 Cycle start time in the device's wallclock."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .a22 import a22_offset_to_time, a22_time_to_offset
from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .discovery import watch_devices
from .entity_helpers import build_device_info, is_entity_available
from .models import client_model_token
from .ports import A22_PORTS

if TYPE_CHECKING:
    from datetime import time

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Create one native start time per A22 physical port."""
    runtime = cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])
    if runtime.coordinator is None:
        return
    coordinator = runtime.coordinator
    watch_devices(entry, coordinator, async_add_entities, lambda device: [
        VivosunA22CycleStart(coordinator, device.device_id, port)
        for port in A22_PORTS
    ] if client_model_token(device.client_id) == "VSPLUGA22" else [])


class VivosunA22CycleStart(CoordinatorEntity[VivosunCoordinator], TimeEntity):  # type: ignore[misc]
    """Edit the firmware's seconds-of-day directly; never schedule HA actions."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: VivosunCoordinator, device_id: str, port: str) -> None:
        super().__init__(coordinator)
        if port not in A22_PORTS:
            raise ValueError("Unknown A22 port")
        self._device_id = device_id
        self._port = port
        label = port.upper() if port.startswith("usb") else f"Outlet {port[-1]}"
        self._attr_name = f"{label} Cycle Start"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_{port}_cycle_start"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def native_value(self) -> time | None:
        start = self.coordinator.a22_settings(self._device_id, self._port).cycle.start
        if start is None:
            return None
        try:
            return a22_offset_to_time(start)
        except ValueError:
            return None

    async def async_set_value(self, value: time) -> None:
        await self.coordinator.async_set_a22_setting(
            self._device_id, self._port, "cycle", "tOffset", a22_time_to_offset(value),
        )

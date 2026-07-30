"""Number platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, shadow_slice
from .shadow import build_hmdf_level_payload, hmdf_lv_to_step, hmdf_step_to_lv

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun number entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    entities: list[Entity] = [
        VivosunHumidifierMistLevel(coordinator, device.device_id)
        for device in coordinator.devices
        if device.device_type == "humidifier"
    ]
    if entities:
        async_add_entities(entities)


class VivosunHumidifierMistLevel(CoordinatorEntity[VivosunCoordinator], NumberEntity):  # type: ignore[misc]
    """Manual mist-level control for a Vivosun AeroStream humidifier.

    Exposed as the app's OFF/1-5 mist-level steps, mapped to the device's raw
    `manu.lv` (0-100) via HMDF_LEVEL_MAP so HA and the app agree level-for-level.
    On these units the on/off flag is inert in manual mode, so the level is the
    effective run/stop control: 0 stops misting, 1-5 run it at that intensity.
    """

    _attr_has_entity_name = True
    _attr_name = "Mist Level"
    _attr_icon = "mdi:air-humidifier"
    _attr_native_min_value = 0
    _attr_native_max_value = 5
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        """Initialize the mist-level number entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_hmdf_mist_level"

    @property
    def native_value(self) -> float | None:
        """Return the current mist level as an app step (0-5)."""
        hmdf = shadow_slice(self.coordinator, self._device_id, "hmdf")
        value = hmdf.get("manual_level")
        if not isinstance(value, int) or isinstance(value, bool):
            value = hmdf.get("level")
        if isinstance(value, int) and not isinstance(value, bool):
            step = hmdf_lv_to_step(value)
            return None if step is None else float(step)
        return None

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        return build_device_info(self.coordinator, self._device_id)

    async def async_set_native_value(self, value: float) -> None:
        """Set the mist level from an app step (0-5); 0 stops misting."""
        await self.coordinator.async_publish_shadow_update(
            build_hmdf_level_payload(hmdf_step_to_lv(int(value))), device_id=self._device_id
        )

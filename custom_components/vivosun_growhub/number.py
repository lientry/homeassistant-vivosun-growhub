"""Stored VSeed S40 growth-stage settings on Home Assistant number entities."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available
from .vseed import SETTING_LIMITS, STAGES, command, reported_seed

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData

_SETTINGS = {
    "tTemp": ("Root Temperature", 100, UnitOfTemperature.CELSIUS),
    "tHumi": ("Dome Humidity", 100, PERCENTAGE),
    "lgtLv": ("Light Intensity", 1, PERCENTAGE),
    "lgtOnDur": ("Light Duration", 1, "s"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Expose explicitly mapped stored stage targets for S40 devices only."""
    runtime = cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])
    if runtime.coordinator is None:
        return
    async_add_entities([
        VivosunSeedSettingNumber(runtime.coordinator, device.device_id, phase, key)
        for device in runtime.coordinator.devices
        if device.device_type == "vseed"
        for phase in (1, 2, 3)
        for key in _SETTINGS
    ])


class VivosunSeedSettingNumber(CoordinatorEntity[VivosunCoordinator], NumberEntity):  # type: ignore[misc]
    """Edit one stored stage recipe value without implying live actuator state."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VivosunCoordinator, device_id: str, phase: int, key: str) -> None:
        super().__init__(coordinator)
        if phase not in (1, 2, 3) or key not in _SETTINGS:
            raise ValueError("Unsupported VSeed stage setting")
        self._device_id = device_id
        self._phase = phase
        self._key = key
        name, scale, unit = _SETTINGS[key]
        low, high, step = SETTING_LIMITS[key]
        self._scale = scale
        self._attr_name = f"{STAGES[phase]} {name}"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_seed_stage{phase}_{key}"
        self._attr_native_min_value = low / scale
        self._attr_native_max_value = high / scale
        self._attr_native_step = step / scale
        self._attr_native_unit_of_measurement = unit
        self._attr_mode = NumberMode.BOX

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def native_value(self) -> float | None:
        stored = reported_seed(self.coordinator, self._device_id).get(f"stage{self._phase}")
        value = stored.get(self._key) if isinstance(stored, dict) else None
        return value / self._scale if type(value) is int else None

    async def async_set_native_value(self, value: float) -> None:
        """Publish the exact stored recipe field; reported echo owns the UI state."""
        scaled = value * self._scale
        if not scaled.is_integer():
            raise ValueError("VSeed setting precision is invalid")
        await self.coordinator.async_publish_shadow_update(
            command((f"stage{self._phase}", self._key), int(scaled)),
            device_id=self._device_id,
            qos=1,
        )

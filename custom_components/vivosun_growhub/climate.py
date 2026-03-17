"""Climate platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_TEMP_UNIT, DOMAIN, MODE_AUTO, MODE_MANUAL, TEMP_SCALE_FACTOR
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, sensor_slice, shadow_slice
from .shadow import build_heat_mode_payload, build_heat_on_payload, build_heat_target_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData

_OPTIONS_TEMP_UNIT = "temp_unit"
_UNIT_FAHRENHEIT = "fahrenheit"
_HEAT_PRESETS = ("manual", "auto")
_TURN_ON_FEATURE = getattr(ClimateEntityFeature, "TURN_ON", ClimateEntityFeature(0))
_TURN_OFF_FEATURE = getattr(ClimateEntityFeature, "TURN_OFF", ClimateEntityFeature(0))
_EXPLICIT_TURN_FEATURES = _TURN_ON_FEATURE | _TURN_OFF_FEATURE


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun climate entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    heaters = [d for d in coordinator.devices if d.device_type == "heater"]
    if heaters:
        async_add_entities(
            [VivosunHeaterClimateEntity(coordinator, entry, d.device_id) for d in heaters]
        )


class VivosunHeaterClimateEntity(CoordinatorEntity[VivosunCoordinator], ClimateEntity):  # type: ignore[misc]
    """Representation of a Vivosun AeroFlux heater."""

    _attr_has_entity_name = True
    _attr_name = "Heater"
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        _EXPLICIT_TURN_FEATURES
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
    )
    _attr_preset_modes: ClassVar[list[str]] = list(_HEAT_PRESETS)
    _attr_min_temp = 0
    _attr_max_temp = 40
    _attr_target_temperature_step = 1
    _enable_turn_on_off_backwards_compatibility = ClimateEntityFeature(0) == _EXPLICIT_TURN_FEATURES

    def __init__(
        self,
        coordinator: VivosunCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the heater climate entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry = entry
        self._attr_unique_id = f"vivosun_growhub_{device_id}_climate"

    @property
    def temperature_unit(self) -> str:
        """Return the temperature unit for this entity."""
        if self._temp_unit_config() == _UNIT_FAHRENHEIT:
            return str(UnitOfTemperature.FAHRENHEIT)
        return str(UnitOfTemperature.CELSIUS)

    @property
    def min_temp(self) -> float:
        """Return the configured minimum target temperature."""
        if self._temp_unit_config() == _UNIT_FAHRENHEIT:
            return 32.0
        return float(self._attr_min_temp)

    @property
    def max_temp(self) -> float:
        """Return the configured maximum target temperature."""
        if self._temp_unit_config() == _UNIT_FAHRENHEIT:
            return 104.0
        return float(self._attr_max_temp)

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        heat = self._heat_state()
        on = heat.get("on")
        if isinstance(on, bool) and on:
            return HVACMode.HEAT
        return HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        heat = self._heat_state()
        state = heat.get("state")
        if isinstance(state, int) and state == 1:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        heat = self._heat_state()
        target = heat.get("target_temp")
        if isinstance(target, int):
            value = target / TEMP_SCALE_FACTOR
            if self._temp_unit_config() == _UNIT_FAHRENHEIT:
                return (value * 9 / 5) + 32
            return value
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return current temperature from probe sensor."""
        sensors = sensor_slice(self.coordinator, self._device_id)
        raw = sensors.get("pTemp")
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            value = raw / TEMP_SCALE_FACTOR
            if self._temp_unit_config() == _UNIT_FAHRENHEIT:
                return (value * 9 / 5) + 32
            return value
        return None

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        heat = self._heat_state()
        mode = heat.get("mode")
        if isinstance(mode, int):
            return "auto" if mode == MODE_AUTO else "manual"
        return None

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        return build_device_info(self.coordinator, self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return additional heater state attributes."""
        heat = self._heat_state()
        attrs: dict[str, object] = {}
        level = heat.get("level")
        if isinstance(level, int):
            attrs["level"] = level
        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode (HEAT or OFF)."""
        on = hvac_mode == HVACMode.HEAT
        await self.coordinator.async_publish_shadow_update(
            build_heat_on_payload(on), device_id=self._device_id
        )

    async def async_turn_on(self) -> None:
        """Turn the heater on."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Turn the heater off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: object) -> None:
        """Set the target temperature."""
        temperature = kwargs.get("temperature")
        if not isinstance(temperature, (int, float)):
            return
        # Convert from display unit back to Celsius for raw storage
        if self._temp_unit_config() == _UNIT_FAHRENHEIT:
            temperature = (temperature - 32) * 5 / 9
        target_raw = int(temperature * TEMP_SCALE_FACTOR)
        await self.coordinator.async_publish_shadow_update(
            build_heat_target_payload(target_raw), device_id=self._device_id
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the preset mode (manual or auto)."""
        if preset_mode == "auto":
            await self.coordinator.async_publish_shadow_update(
                build_heat_mode_payload(MODE_AUTO), device_id=self._device_id
            )
        elif preset_mode == "manual":
            await self.coordinator.async_publish_shadow_update(
                build_heat_mode_payload(MODE_MANUAL), device_id=self._device_id
            )

    def _heat_state(self) -> Mapping[str, object]:
        return shadow_slice(self.coordinator, self._device_id, "heat")

    def _temp_unit_config(self) -> str:
        configured_unit = self._entry.options.get(_OPTIONS_TEMP_UNIT, DEFAULT_TEMP_UNIT)
        if isinstance(configured_unit, str):
            return configured_unit
        return DEFAULT_TEMP_UNIT

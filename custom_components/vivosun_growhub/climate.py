"""Climate platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_TEMP_UNIT, DOMAIN, MODE_AUTO, MODE_MANUAL, TEMP_SCALE_FACTOR
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, sensor_slice, shadow_slice
from .shadow import (
    build_aircd_func_payload,
    build_aircd_on_payload,
    build_aircd_target_temp_payload,
    build_aircd_wind_payload,
    build_heat_mode_payload,
    build_heat_on_payload,
    build_heat_target_payload,
)

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

    entities: list[ClimateEntity] = []
    for device in coordinator.devices:
        if device.device_type == "heater":
            entities.append(VivosunHeaterClimateEntity(coordinator, entry, device.device_id))
        elif device.device_type == "air_conditioner":
            entities.append(VivosunAirConditionerClimateEntity(coordinator, entry, device.device_id))
    if entities:
        async_add_entities(entities)


class VivosunHeaterClimateEntity(CoordinatorEntity[VivosunCoordinator], ClimateEntity):  # type: ignore[misc]
    """Representation of a Vivosun AeroFlux heater."""

    _attr_has_entity_name = True
    _attr_name = "Heater"
    _attr_supported_features = (
        _EXPLICIT_TURN_FEATURES
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
    )
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
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        self._attr_preset_modes = list(_HEAT_PRESETS)

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


_AIRCON_FUNC_TO_HVAC: dict[int, HVACMode] = {
    1: HVACMode.COOL,
    2: HVACMode.HEAT,
    3: HVACMode.DRY,
    4: HVACMode.FAN_ONLY,
}
_HVAC_TO_AIRCON_FUNC: dict[HVACMode, int] = {mode: func for func, mode in _AIRCON_FUNC_TO_HVAC.items()}
_AIRCON_FAN_MODES: dict[str, int] = {"low": 25, "medium": 50, "high": 100}


class VivosunAirConditionerClimateEntity(CoordinatorEntity[VivosunCoordinator], ClimateEntity):  # type: ignore[misc]
    """Representation of a Vivosun AeroLush air conditioner (e.g. C08 / VSACA08)."""

    _attr_has_entity_name = True
    _attr_name = "Air Conditioner"
    _attr_supported_features = (
        _EXPLICIT_TURN_FEATURES
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
    )
    _attr_min_temp = 16
    _attr_max_temp = 32
    _attr_target_temperature_step = 1
    _attr_fan_modes: ClassVar[list[str]] = list(_AIRCON_FAN_MODES)
    _enable_turn_on_off_backwards_compatibility = ClimateEntityFeature(0) == _EXPLICIT_TURN_FEATURES

    def __init__(
        self,
        coordinator: VivosunCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the air conditioner climate entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry = entry
        self._attr_unique_id = f"vivosun_growhub_{device_id}_climate"
        self._attr_hvac_modes = [HVACMode.OFF, *_HVAC_TO_AIRCON_FUNC]

    @property
    def temperature_unit(self) -> str:
        """Return the temperature unit for this entity."""
        if self._temp_unit_config() == _UNIT_FAHRENHEIT:
            return str(UnitOfTemperature.FAHRENHEIT)
        return str(UnitOfTemperature.CELSIUS)

    @property
    def min_temp(self) -> float:
        """Return the minimum target temperature, honoring the device tMin."""
        minimum = float(self._attr_min_temp)
        reported_min = self._aircd_state().get("min_target_temp")
        if isinstance(reported_min, int):
            minimum = reported_min / TEMP_SCALE_FACTOR
        if self._temp_unit_config() == _UNIT_FAHRENHEIT:
            return (minimum * 9 / 5) + 32
        return minimum

    @property
    def max_temp(self) -> float:
        """Return the maximum target temperature."""
        if self._temp_unit_config() == _UNIT_FAHRENHEIT:
            return (float(self._attr_max_temp) * 9 / 5) + 32
        return float(self._attr_max_temp)

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode from aircd state/func."""
        aircd = self._aircd_state()
        state = aircd.get("state")
        if not isinstance(state, int) or state != 1:
            return HVACMode.OFF
        func = aircd.get("func")
        if isinstance(func, int) and func in _AIRCON_FUNC_TO_HVAC:
            return _AIRCON_FUNC_TO_HVAC[func]
        return HVACMode.COOL

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        mode = self.hvac_mode
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        pause = self._aircd_state().get("pause")
        if isinstance(pause, int) and pause == 1:
            return HVACAction.IDLE
        if mode == HVACMode.COOL:
            return HVACAction.COOLING
        if mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if mode == HVACMode.DRY:
            return HVACAction.DRYING
        return HVACAction.FAN

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        target = self._aircd_state().get("target_temp")
        if isinstance(target, int):
            value = target / TEMP_SCALE_FACTOR
            if self._temp_unit_config() == _UNIT_FAHRENHEIT:
                return (value * 9 / 5) + 32
            return value
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return current temperature from the probe sensor."""
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
    def current_humidity(self) -> float | None:
        """Return current humidity from the probe sensor."""
        sensors = sensor_slice(self.coordinator, self._device_id)
        raw = sensors.get("pHumi")
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw / TEMP_SCALE_FACTOR
        return None

    @property
    def fan_mode(self) -> str | None:
        """Return the closest named fan mode for the reported wind level."""
        level = self._aircd_state().get("wind_level")
        if not isinstance(level, int):
            return None
        return min(_AIRCON_FAN_MODES, key=lambda name: abs(_AIRCON_FAN_MODES[name] - level))

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
        """Return additional raw aircd attributes useful for debugging."""
        aircd = self._aircd_state()
        attrs: dict[str, object] = {}
        for source_key, attr_name in (
            ("func", "function"),
            ("wind_level", "wind_level"),
            ("target_humidity", "target_humidity_raw"),
        ):
            value = aircd.get(source_key)
            if isinstance(value, int):
                attrs[attr_name] = value
        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode (off, cool, heat, dry, fan_only)."""
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_publish_shadow_update(
                build_aircd_on_payload(False), device_id=self._device_id
            )
            return
        func = _HVAC_TO_AIRCON_FUNC.get(hvac_mode)
        if func is None:
            return
        await self.coordinator.async_publish_shadow_update(
            build_aircd_func_payload(func), device_id=self._device_id
        )

    async def async_turn_on(self) -> None:
        """Turn the air conditioner on."""
        await self.coordinator.async_publish_shadow_update(
            build_aircd_on_payload(True), device_id=self._device_id
        )

    async def async_turn_off(self) -> None:
        """Turn the air conditioner off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: object) -> None:
        """Set the target temperature."""
        temperature = kwargs.get("temperature")
        if not isinstance(temperature, (int, float)):
            return
        if self._temp_unit_config() == _UNIT_FAHRENHEIT:
            temperature = (temperature - 32) * 5 / 9
        target_raw = round(temperature * TEMP_SCALE_FACTOR)
        await self.coordinator.async_publish_shadow_update(
            build_aircd_target_temp_payload(target_raw), device_id=self._device_id
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode (low/medium/high wind level)."""
        level = _AIRCON_FAN_MODES.get(fan_mode)
        if level is None:
            return
        await self.coordinator.async_publish_shadow_update(
            build_aircd_wind_payload(level), device_id=self._device_id
        )

    def _aircd_state(self) -> Mapping[str, object]:
        return shadow_slice(self.coordinator, self._device_id, "aircd")

    def _temp_unit_config(self) -> str:
        configured_unit = self._entry.options.get(_OPTIONS_TEMP_UNIT, DEFAULT_TEMP_UNIT)
        if isinstance(configured_unit, str):
            return configured_unit
        return DEFAULT_TEMP_UNIT

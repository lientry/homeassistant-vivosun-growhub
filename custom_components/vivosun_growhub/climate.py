"""Climate platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODE_AUTO, MODE_MANUAL, SENSOR_UNAVAILABLE_SENTINEL, TEMP_SCALE_FACTOR
from .coordinator import VivosunCoordinator
from .discovery import watch_devices
from .entity_helpers import build_device_info, is_entity_available, sensor_slice, shadow_slice
from .shadow import (
    build_aircd_func_payload,
    build_aircd_on_payload,
    build_aircd_target_temp_payload,
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

    from .models import DeviceInfo as VivosunDeviceInfo
    from .models import RuntimeData

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

    def factory(device: VivosunDeviceInfo) -> list[ClimateEntity]:
        if device.device_type == "heater":
            return [VivosunHeaterClimateEntity(coordinator, entry, device.device_id)]
        if device.device_type == "air_conditioner":
            return [VivosunAirConditionerClimateEntity(coordinator, entry, device.device_id)]
        return []

    watch_devices(entry, coordinator, async_add_entities, factory)


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
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
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
        self._attr_unique_id = f"vivosun_growhub_{device_id}_climate"
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        self._attr_preset_modes = list(_HEAT_PRESETS)

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
    def native_target_temperature(self) -> float | None:
        """Return the device target in Celsius."""
        heat = self._heat_state()
        target = heat.get("target_temp")
        if type(target) is int and target != SENSOR_UNAVAILABLE_SENTINEL:
            return target / TEMP_SCALE_FACTOR
        return None

    @property
    def native_current_temperature(self) -> float | None:
        """Return the probe temperature in Celsius."""
        sensors = sensor_slice(self.coordinator, self._device_id)
        raw = sensors.get("pTemp")
        if type(raw) is int and raw != SENSOR_UNAVAILABLE_SENTINEL:
            return raw / TEMP_SCALE_FACTOR
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
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            return
        # Home Assistant converts service input to the native unit before this call.
        target_raw = round(temperature * TEMP_SCALE_FACTOR)
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

class VivosunAirConditionerClimateEntity(CoordinatorEntity[VivosunCoordinator], ClimateEntity):  # type: ignore[misc]
    """AeroLush C08 cooling controls. Other functions need device verification."""

    _attr_has_entity_name = True
    _attr_name = "Air Conditioner"
    _attr_supported_features = _EXPLICIT_TURN_FEATURES | ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_min_temp = 16
    _attr_max_temp = 32
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _enable_turn_on_off_backwards_compatibility = ClimateEntityFeature(0) == _EXPLICIT_TURN_FEATURES

    def __init__(self, coordinator: VivosunCoordinator, entry: ConfigEntry, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_climate"
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL]

    @property
    def min_temp(self) -> float:
        minimum = self._aircd_state().get("min_target_temp")
        if type(minimum) is int and 16 * TEMP_SCALE_FACTOR <= minimum <= 32 * TEMP_SCALE_FACTOR:
            return minimum / TEMP_SCALE_FACTOR
        return float(self._attr_min_temp)

    @property
    def hvac_mode(self) -> HVACMode | None:
        state = self._aircd_state()
        if state.get("state") == 0:
            return HVACMode.OFF
        if state.get("state") == 1 and state.get("func") == 1:
            return HVACMode.COOL
        return None

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.hvac_mode
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        if mode == HVACMode.COOL:
            return HVACAction.IDLE if self._aircd_state().get("pause") == 1 else HVACAction.COOLING
        return None

    @property
    def native_target_temperature(self) -> float | None:
        raw = self._aircd_state().get("target_temp")
        return raw / TEMP_SCALE_FACTOR if type(raw) is int and raw != SENSOR_UNAVAILABLE_SENTINEL else None

    @property
    def native_current_temperature(self) -> float | None:
        raw = sensor_slice(self.coordinator, self._device_id).get("pTemp")
        if type(raw) is int and raw != SENSOR_UNAVAILABLE_SENTINEL:
            return raw / TEMP_SCALE_FACTOR
        return None

    @property
    def current_humidity(self) -> float | None:
        raw = sensor_slice(self.coordinator, self._device_id).get("pHumi")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw / TEMP_SCALE_FACTOR
        return None

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._aircd_state()
        return {
            key: value
            for key, value in (("function", state.get("func")), ("wind_level", state.get("wind_level")))
            if isinstance(value, int)
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            payload = build_aircd_on_payload(False)
        elif hvac_mode == HVACMode.COOL:
            payload = build_aircd_func_payload(1)
        else:
            return
        await self.coordinator.async_publish_shadow_update(payload, device_id=self._device_id)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.COOL)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: object) -> None:
        temp = kwargs.get("temperature")
        if isinstance(temp, bool) or not isinstance(temp, (int, float)):
            return
        await self.coordinator.async_publish_shadow_update(
            build_aircd_target_temp_payload(round(temp * TEMP_SCALE_FACTOR)),
            device_id=self._device_id,
        )

    def _aircd_state(self) -> Mapping[str, object]:
        return shadow_slice(self.coordinator, self._device_id, "aircd")

"""Fan platform for the Vivosun GrowHub integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

import voluptuous as vol
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.helpers.entity_platform import async_get_current_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available
from .shadow import (
    build_cfan_level_payload,
    build_cfan_night_mode_payload,
    build_cfan_oscillate_payload,
    build_dfan_auto_mode_payload,
    build_dfan_auto_threshold_payload,
    build_dfan_level_payload,
    cfan_percentage_to_shadow,
    cfan_shadow_to_percentage,
    dfan_percentage_to_shadow,
    dfan_shadow_to_percentage,
)

_CIRCULATION_PRESETS = ["normal", "night", "natural_wind"]
_DUCT_PRESETS = ["manual", "auto"]
_DEFAULT_TURN_ON_PERCENTAGE = 10
_TURN_ON_FEATURE = getattr(FanEntityFeature, "TURN_ON", FanEntityFeature(0))
_TURN_OFF_FEATURE = getattr(FanEntityFeature, "TURN_OFF", FanEntityFeature(0))
_EXPLICIT_TURN_FEATURES = _TURN_ON_FEATURE | _TURN_OFF_FEATURE

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
    """Set up Vivosun fan entities and entity services."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    async_add_entities(
        [
            VivosunCirculationFanEntity(coordinator),
            VivosunDuctFanEntity(coordinator),
        ]
    )

    platform = async_get_current_platform()
    platform.async_register_entity_service(
        "set_duct_fan_auto_threshold",
        {
            vol.Required("field"): str,
            vol.Optional("value"): vol.Any(None, vol.Coerce(int)),
        },
        "async_set_auto_threshold",
    )


class _VivosunFanBase(CoordinatorEntity[VivosunCoordinator], FanEntity):  # type: ignore[misc]
    """Common base for Vivosun fan entities."""

    _attr_has_entity_name = True
    _attr_speed_count = 10
    _enable_turn_on_off_backwards_compatibility = FanEntityFeature(0) == _EXPLICIT_TURN_FEATURES

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return is_entity_available(self.coordinator)

    @property
    def is_on(self) -> bool:
        """Return whether the fan is on."""
        percentage = self.percentage
        return bool((percentage is not None and percentage > 0) or self.preset_mode == "natural_wind")

    @property
    def device_info(self) -> DeviceInfo:
        """Return shared device info for this entity."""
        return build_device_info(self.coordinator)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: object,
    ) -> None:
        """Turn the fan on, optionally applying preset mode and target percentage."""
        requested_preset = preset_mode
        if requested_preset is None:
            raw_preset = kwargs.get("preset_mode")
            if isinstance(raw_preset, str):
                requested_preset = raw_preset
        if requested_preset is not None:
            await self.async_set_preset_mode(requested_preset)

        requested_percentage = percentage
        if requested_percentage is None:
            raw_percentage = kwargs.get("percentage")
            if isinstance(raw_percentage, int):
                requested_percentage = raw_percentage

        # Presets such as natural_wind intentionally do not expose a percentage.
        if requested_preset is not None and requested_percentage is None and self.percentage is None:
            return

        target = requested_percentage if requested_percentage is not None else self.percentage
        if target is None or target <= 0:
            target = _DEFAULT_TURN_ON_PERCENTAGE
        await self.async_set_percentage(target)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn the fan off."""
        _ = kwargs
        await self.async_set_percentage(0)

    async def async_toggle(self, **kwargs: object) -> None:
        """Toggle the fan on/off."""
        if self.is_on:
            await self.async_turn_off(**kwargs)
            return
        requested_percentage = kwargs.get("percentage")
        requested_preset = kwargs.get("preset_mode")
        await self.async_turn_on(
            percentage=requested_percentage if isinstance(requested_percentage, int) else None,
            preset_mode=requested_preset if isinstance(requested_preset, str) else None,
        )

    def turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: object,
    ) -> None:
        """Synchronously bridge turn_on for HA action support detection."""
        asyncio.run_coroutine_threadsafe(
            self.async_turn_on(percentage=percentage, preset_mode=preset_mode, **kwargs),
            self.hass.loop,
        ).result()

    def turn_off(self, **kwargs: object) -> None:
        """Synchronously bridge turn_off for HA action support detection."""
        asyncio.run_coroutine_threadsafe(
            self.async_turn_off(**kwargs),
            self.hass.loop,
        ).result()

    def toggle(self, **kwargs: object) -> None:
        """Synchronously bridge toggle for HA action support detection."""
        asyncio.run_coroutine_threadsafe(
            self.async_toggle(**kwargs),
            self.hass.loop,
        ).result()


class VivosunCirculationFanEntity(_VivosunFanBase):
    """Representation of the circulation fan (cFan)."""

    _attr_name = "Circulation Fan"
    _attr_supported_features = (
        _EXPLICIT_TURN_FEATURES
        | FanEntityFeature.SET_SPEED
        | FanEntityFeature.OSCILLATE
        | FanEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = _CIRCULATION_PRESETS

    def __init__(self, coordinator: VivosunCoordinator) -> None:
        """Initialize the circulation fan entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"vivosun_growhub_{coordinator.device.device_id}_cfan"

    @property
    def percentage(self) -> int | None:
        """Return the current fan speed percentage."""
        return cfan_shadow_to_percentage(_as_int(self._cfan_state().get("level")))

    @property
    def oscillating(self) -> bool | None:
        """Return whether oscillation is enabled."""
        oscillating = self._cfan_state().get("oscillating")
        if isinstance(oscillating, bool):
            return oscillating
        return None

    @property
    def preset_mode(self) -> str | None:
        """Return current circulation fan preset mode."""
        if _as_int(self._cfan_state().get("level")) == 200:
            return "natural_wind"
        night_mode = self._cfan_state().get("night_mode")
        if isinstance(night_mode, bool):
            return "night" if night_mode else "normal"
        return None

    async def async_set_percentage(self, percentage: int) -> None:
        """Set circulation fan speed percentage."""
        await self.coordinator.async_publish_shadow_update(
            build_cfan_level_payload(cfan_percentage_to_shadow(percentage))
        )

    async def async_oscillate(self, oscillating: bool) -> None:
        """Set circulation fan oscillation."""
        await self.coordinator.async_publish_shadow_update(build_cfan_oscillate_payload(oscillating))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set circulation fan preset mode."""
        if preset_mode == "natural_wind":
            await self.coordinator.async_publish_shadow_update(build_cfan_level_payload(200))
            return
        if preset_mode == "night":
            await self.coordinator.async_publish_shadow_update(build_cfan_night_mode_payload(True))
            return
        if preset_mode == "normal":
            await self.coordinator.async_publish_shadow_update(build_cfan_night_mode_payload(False))
            return
        raise ValueError(f"Unsupported circulation fan preset: {preset_mode}")

    def _cfan_state(self) -> Mapping[str, object]:
        return _shadow_slice(self.coordinator, "cFan")


class VivosunDuctFanEntity(_VivosunFanBase):
    """Representation of the duct fan (dFan)."""

    _attr_name = "Duct Fan"
    _attr_supported_features = (
        _EXPLICIT_TURN_FEATURES
        | FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = _DUCT_PRESETS

    def __init__(self, coordinator: VivosunCoordinator) -> None:
        """Initialize the duct fan entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"vivosun_growhub_{coordinator.device.device_id}_dfan"

    @property
    def percentage(self) -> int | None:
        """Return the current fan speed percentage."""
        return dfan_shadow_to_percentage(_as_int(self._dfan_state().get("level")))

    @property
    def preset_mode(self) -> str | None:
        """Return current duct fan preset mode."""
        auto_enabled = self._dfan_state().get("auto_enabled")
        if isinstance(auto_enabled, bool):
            return "auto" if auto_enabled else "manual"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose dFan auto-threshold state attributes."""
        auto = self._dfan_auto_state()
        attributes: dict[str, object] = {}
        for field in (
            "lvMin",
            "lvMax",
            "tMin",
            "tMax",
            "hMin",
            "hMax",
            "vpdMin",
            "vpdMax",
            "tStep",
            "hStep",
            "vpdStep",
            "exChk",
        ):
            value = auto.get(field)
            if isinstance(value, int) or value is None:
                attributes[field] = value
        return attributes

    async def async_set_percentage(self, percentage: int) -> None:
        """Set duct fan speed percentage."""
        await self.coordinator.async_publish_shadow_update(
            build_dfan_level_payload(dfan_percentage_to_shadow(percentage))
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set duct fan preset mode."""
        if preset_mode == "auto":
            await self.coordinator.async_publish_shadow_update(build_dfan_auto_mode_payload(True))
            return
        if preset_mode == "manual":
            await self.coordinator.async_publish_shadow_update(build_dfan_auto_mode_payload(False))
            return
        raise ValueError(f"Unsupported duct fan preset: {preset_mode}")

    async def async_set_auto_threshold(self, field: str, value: int | None = None) -> None:
        """Set a duct fan auto-threshold field."""
        await self.coordinator.async_publish_shadow_update(build_dfan_auto_threshold_payload(field, value))

    def _dfan_state(self) -> Mapping[str, object]:
        return _shadow_slice(self.coordinator, "dFan")

    def _dfan_auto_state(self) -> Mapping[str, object]:
        auto = self._dfan_state().get("auto")
        if isinstance(auto, Mapping):
            return cast("Mapping[str, object]", auto)
        return {}


def _shadow_slice(coordinator: VivosunCoordinator, key: str) -> Mapping[str, object]:
    data = coordinator.data
    if not isinstance(data, Mapping):
        return {}

    shadow = data.get("shadow")
    if not isinstance(shadow, Mapping):
        return {}

    value = shadow.get(key)
    if not isinstance(value, Mapping):
        return {}

    return cast("Mapping[str, object]", value)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None

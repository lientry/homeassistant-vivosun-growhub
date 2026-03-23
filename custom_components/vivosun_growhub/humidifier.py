"""Humidifier platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from homeassistant.components.humidifier import (
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityFeature,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODE_AUTO, MODE_MANUAL, TEMP_SCALE_FACTOR
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, sensor_slice, shadow_slice
from .shadow import (
    build_hmdf_mode_payload,
    build_hmdf_on_payload,
    build_hmdf_target_payload,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData

_HMDF_MODES = ("manual", "auto")
_TURN_ON_FEATURE = getattr(HumidifierEntityFeature, "TURN_ON", HumidifierEntityFeature(0))
_TURN_OFF_FEATURE = getattr(HumidifierEntityFeature, "TURN_OFF", HumidifierEntityFeature(0))
_EXPLICIT_TURN_FEATURES = _TURN_ON_FEATURE | _TURN_OFF_FEATURE


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun humidifier entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    humidifiers = [d for d in coordinator.devices if d.device_type == "humidifier"]
    if humidifiers:
        async_add_entities(
            [VivosunHumidifierEntity(coordinator, d.device_id) for d in humidifiers]
        )


class VivosunHumidifierEntity(CoordinatorEntity[VivosunCoordinator], HumidifierEntity):  # type: ignore[misc]
    """Representation of a Vivosun AeroStream humidifier."""

    _attr_has_entity_name = True
    _attr_name = "Humidifier"
    _attr_device_class = HumidifierDeviceClass.HUMIDIFIER
    _attr_supported_features = _EXPLICIT_TURN_FEATURES | HumidifierEntityFeature.MODES
    _attr_available_modes: ClassVar[list[str]] = list(_HMDF_MODES)
    _attr_min_humidity = 0
    _attr_max_humidity = 100
    _enable_turn_on_off_backwards_compatibility = HumidifierEntityFeature(0) == _EXPLICIT_TURN_FEATURES

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        """Initialize the humidifier entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_humidifier"

    @property
    def is_on(self) -> bool | None:
        """Return whether the humidifier is on."""
        hmdf = self._hmdf_state()
        on = hmdf.get("on")
        if isinstance(on, bool):
            return on
        return None

    @property
    def target_humidity(self) -> float | None:
        """Return the target humidity percentage."""
        hmdf = self._hmdf_state()
        target = hmdf.get("target_humidity")
        if isinstance(target, int):
            return target / TEMP_SCALE_FACTOR
        return None

    @property
    def current_humidity(self) -> float | None:
        """Return the current humidity from probe sensor."""
        sensors = sensor_slice(self.coordinator, self._device_id)
        raw = sensors.get("pHumi")
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw / TEMP_SCALE_FACTOR
        return None

    @property
    def mode(self) -> str | None:
        """Return the current humidifier mode."""
        hmdf = self._hmdf_state()
        mode = hmdf.get("mode")
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
        """Return additional humidifier state attributes."""
        hmdf = self._hmdf_state()
        attrs: dict[str, object] = {}
        level = hmdf.get("level")
        if isinstance(level, int):
            attrs["level"] = level
        water_warning = hmdf.get("water_warning")
        if isinstance(water_warning, bool):
            attrs["water_warning"] = water_warning
        return attrs

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn on the humidifier."""
        _ = kwargs
        await self.coordinator.async_publish_shadow_update(
            build_hmdf_on_payload(True), device_id=self._device_id
        )

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn off the humidifier."""
        _ = kwargs
        await self.coordinator.async_publish_shadow_update(
            build_hmdf_on_payload(False), device_id=self._device_id
        )

    async def async_set_humidity(self, humidity: int) -> None:
        """Set target humidity percentage."""
        target_raw = int(humidity * TEMP_SCALE_FACTOR)
        await self.coordinator.async_publish_shadow_update(
            build_hmdf_target_payload(target_raw), device_id=self._device_id
        )

    async def async_set_mode(self, mode: str) -> None:
        """Set humidifier mode (manual or auto)."""
        if mode == "auto":
            await self.coordinator.async_publish_shadow_update(
                build_hmdf_mode_payload(MODE_AUTO), device_id=self._device_id
            )
        elif mode == "manual":
            await self.coordinator.async_publish_shadow_update(
                build_hmdf_mode_payload(MODE_MANUAL), device_id=self._device_id
            )

    def _hmdf_state(self) -> Mapping[str, object]:
        return shadow_slice(self.coordinator, self._device_id, "hmdf")

"""Light platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LIGHT_MIN_BRIGHTNESS
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, shadow_slice
from .shadow import build_light_level_payload, build_light_spectrum_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    """Set up Vivosun light entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    controllers = [d for d in coordinator.devices if d.device_type == "controller"]
    if not controllers:
        return
    async_add_entities([VivosunLightEntity(coordinator, controllers[0].device_id)])


class VivosunLightEntity(CoordinatorEntity[VivosunCoordinator], LightEntity):  # type: ignore[misc]
    """Representation of the GrowHub grow light."""

    _attr_has_entity_name = True
    _attr_name = "Grow Light"
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        """Initialize the light entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_light"

    @property
    def is_on(self) -> bool:
        """Return whether the light is on."""
        level = self._light_level()
        return level is not None and level > 0

    @property
    def brightness(self) -> int | None:
        """Return light brightness in HA scale (0..255)."""
        level = self._light_level()
        if level is None:
            return None
        return round(level * 255 / 100)

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return shared device info for this entity."""
        return build_device_info(self.coordinator, self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return additional parsed light state attributes."""
        light = self._light_state()
        attributes: dict[str, object] = {}

        mode = light.get("mode")
        if isinstance(mode, int):
            attributes["mode"] = mode

        spectrum = light.get("spectrum")
        if isinstance(spectrum, int):
            attributes["spectrum"] = spectrum
        elif spectrum is None:
            attributes["spectrum"] = None

        return attributes

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn on the light and optionally set brightness/spectrum."""
        brightness = kwargs.get("brightness")
        if isinstance(brightness, int):
            level = round(brightness * 100 / 255)
            await self.coordinator.async_publish_shadow_update(
                build_light_level_payload(level), device_id=self._device_id
            )
        elif self._light_level() in (None, 0):
            await self.coordinator.async_publish_shadow_update(
                build_light_level_payload(LIGHT_MIN_BRIGHTNESS), device_id=self._device_id
            )

        spectrum = kwargs.get("spectrum")
        if isinstance(spectrum, int):
            await self.coordinator.async_publish_shadow_update(
                build_light_spectrum_payload(spectrum), device_id=self._device_id
            )

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn off the light."""
        _ = kwargs
        await self.coordinator.async_publish_shadow_update(
            build_light_level_payload(0), device_id=self._device_id
        )

    def _light_state(self) -> Mapping[str, object]:
        return shadow_slice(self.coordinator, self._device_id, "light")

    def _light_level(self) -> int | None:
        level = self._light_state().get("level")
        if isinstance(level, int):
            return level
        return None

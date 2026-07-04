"""Switch entities for Vivosun curing box control toggles."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, shadow_slice
from .models import RuntimeData

_CURING_BOX_SWITCHES: tuple[tuple[str, str, str], ...] = (
    ("ctlGlass", "Privacy Glass", "mdi:blinds"),
    ("ctlLight", "Interior Light", "mdi:lightbulb-on"),
    ("ctlLock", "Control Lock", "mdi:lock"),
)


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up curing box control switches."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return
    entities: list[SwitchEntity] = []
    for device in coordinator.devices:
        if device.device_type != "curing_box":
            continue
        for key, name, icon in _CURING_BOX_SWITCHES:
            entities.append(
                VivosunCtlSwitch(coordinator, device.device_id, key=key, name=name, icon=icon)
            )
    async_add_entities(entities)


class VivosunCtlSwitch(CoordinatorEntity[VivosunCoordinator], SwitchEntity):  # type: ignore[misc]
    """Toggle for a top-level desired control key (ctlGlass/ctlLight/ctlLock)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VivosunCoordinator,
        device_id: str,
        *,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"vivosun_growhub_{device_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def is_on(self) -> bool | None:
        reported = shadow_slice(self.coordinator, self._device_id, "reported_supported")
        value = reported.get(self._key)
        if value is None:
            return None
        try:
            return bool(int(cast("int", value)))
        except (TypeError, ValueError):
            return None

    async def _publish(self, on: bool) -> None:
        payload = {"state": {"desired": {self._key: int(on)}}}
        await self.coordinator.async_publish_shadow_update(
            payload, device_id=self._device_id, qos=1
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._publish(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._publish(False)

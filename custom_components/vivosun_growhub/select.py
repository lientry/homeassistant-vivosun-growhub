"""Select entities for Vivosun device modes."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, shadow_slice

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData

OPTION_STOPPED = "Stopped"
OPTION_QUICK_CYCLE = "Quick Cycle"
OPTION_REFINE_CYCLE = "Refine Cycle"
OPTION_CURE_ONLY = "Cure Only"
OPTION_COLD_STORE = "Cold Store"
OPTION_EXTRACT_CURE = "Extract Cure"

_MODE_CONT_IDS: dict[str, str] = {
    OPTION_QUICK_CYCLE: "234193+1756947323",
    OPTION_REFINE_CYCLE: "234194+1756947323",
    OPTION_CURE_ONLY: "234195+1756947324",
    OPTION_COLD_STORE: "234196+1756947324",
    OPTION_EXTRACT_CURE: "234197+1757484248",
}
_PREFIX_TO_MODE: dict[str, str] = {
    cont_id.split("+", maxsplit=1)[0]: name for name, cont_id in _MODE_CONT_IDS.items()
}


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun select entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return
    async_add_entities(
        [
            VivosunCureModeSelect(coordinator, device.device_id)
            for device in coordinator.devices
            if device.device_type == "curing_box"
        ]
    )


class VivosunCureModeSelect(CoordinatorEntity[VivosunCoordinator], SelectEntity):  # type: ignore[misc]
    """Mode preset selector writing desired plan stage state."""

    _attr_has_entity_name = True
    _attr_name = "Mode"
    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        """Initialize the curing mode select."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_cure_mode"
        self._attr_options = [*_MODE_CONT_IDS, OPTION_STOPPED]

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def current_option(self) -> str | None:
        stage = self._stage()
        if not stage.get("startT") or not self._cure().get("inPlan"):
            return OPTION_STOPPED
        cont_id = stage.get("contId")
        if not isinstance(cont_id, str):
            return None
        return _PREFIX_TO_MODE.get(cont_id.split("+", maxsplit=1)[0])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        stage = self._stage()
        return {
            "cont_id": stage.get("contId"),
            "start_time": stage.get("startT"),
        }

    async def async_select_option(self, option: str) -> None:
        """Select a curing mode preset."""
        if option not in self._attr_options:
            raise ValueError(f"Unsupported curing mode: {option}")
        await self._publish({"startT": 0})
        if option == OPTION_STOPPED:
            return
        await asyncio.sleep(2)
        await self._publish({"startT": int(time.time()), "contId": _MODE_CONT_IDS[option]})

    def _reported(self, key: str) -> dict[str, object]:
        supported = shadow_slice(self.coordinator, self._device_id, "reported_supported")
        value = supported.get(key)
        return value if isinstance(value, dict) else {}

    def _cure(self) -> dict[str, object]:
        return self._reported("cure")

    def _stage(self) -> dict[str, object]:
        stage = self._reported("plan").get("stage1")
        return stage if isinstance(stage, dict) else {}

    async def _publish(self, payload: dict[str, object]) -> None:
        await self.coordinator.async_publish_shadow_update(
            {"state": {"desired": {"plan": {"stage1": payload}}}},
            device_id=self._device_id,
            qos=1,
        )

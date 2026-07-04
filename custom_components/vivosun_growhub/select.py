"""Select entity for Vivosun curing box mode presets."""

from __future__ import annotations

import asyncio
import time
from typing import cast

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, shadow_slice
from .models import RuntimeData

OPTION_STOPPED = "Gestoppt"

# contId-Konstanten aus Support-Capture 2026-07-04. Preset-IDs sind global,
# die Custom-ID ist kontogebunden und aendert sich bei Rezept-Bearbeitung.
_MODE_CONT_IDS: dict[str, str] = {
    "Schnellzyklus": "234193+1756947323",
    "Feinzyklus": "234194+1756947323",
    "Nur Curen": "234195+1756947324",
    "Kaltlagerung": "234196+1756947324",
    "Extract-Cure": "234197+1757484248",
    "Custom": "352002+1783181090",
}
_PREFIX_TO_MODE: dict[str, str] = {
    cid.split("+")[0]: name for name, cid in _MODE_CONT_IDS.items()
}


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up curing box mode select."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return
    entities = [
        VivosunCureModeSelect(coordinator, device.device_id)
        for device in coordinator.devices
        if device.device_type == "curing_box"
    ]
    async_add_entities(entities)


class VivosunCureModeSelect(CoordinatorEntity[VivosunCoordinator], SelectEntity):  # type: ignore[misc]
    """Mode preset selector writing desired.plan.stage1."""

    _attr_has_entity_name = True
    _attr_name = "Modus"
    _attr_icon = "mdi:tune-variant"
    _attr_options = [*_MODE_CONT_IDS.keys(), OPTION_STOPPED]

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_cure_mode"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    def _reported(self, key: str) -> dict[str, object]:
        supported = shadow_slice(self.coordinator, self._device_id, "reported_supported")
        value = supported.get(key)
        return value if isinstance(value, dict) else {}

    @property
    def current_option(self) -> str | None:
        cure = self._reported("cure")
        plan = self._reported("plan")
        stage = plan.get("stage1")
        stage = stage if isinstance(stage, dict) else {}
        start_t = stage.get("startT")
        in_plan = cure.get("inPlan")
        if not start_t or not in_plan:
            return OPTION_STOPPED
        cont_id = stage.get("contId")
        if not isinstance(cont_id, str):
            return None
        return _PREFIX_TO_MODE.get(cont_id.split("+")[0])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        plan = self._reported("plan")
        stage = plan.get("stage1")
        stage = stage if isinstance(stage, dict) else {}
        return {"cont_id": stage.get("contId"), "start_t": stage.get("startT")}

    async def _publish(self, payload: dict[str, object]) -> None:
        await self.coordinator.async_publish_shadow_update(
            {"state": {"desired": {"plan": {"stage1": payload}}}},
            device_id=self._device_id,
            qos=1,
        )

    async def async_select_option(self, option: str) -> None:
        await self._publish({"startT": 0})
        if option == OPTION_STOPPED:
            return
        await asyncio.sleep(2)
        await self._publish(
            {"startT": int(time.time()), "contId": _MODE_CONT_IDS[option]}
        )

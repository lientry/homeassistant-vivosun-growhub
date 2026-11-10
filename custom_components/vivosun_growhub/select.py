"""Select entities for Vivosun device modes."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OPTION_SPECTRUM_CONTROLLERS
from .controls import require_selected_control, supports_local_program_mode
from .coordinator import VivosunCoordinator
from .discovery import watch_devices
from .entity_helpers import build_device_info, is_entity_available, shadow_slice
from .models import client_model_token
from .ports import A22_PORTS
from .shadow import build_light_spectrum_payload
from .vseed import STAGES, command, reported_seed, stage_name

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import DeviceInfo as VivosunDeviceInfo
    from .models import RuntimeData

OPTION_STOPPED = "Stopped"
OPTION_QUICK_CYCLE = "Quick Cycle"
OPTION_REFINE_CYCLE = "Refine Cycle"
OPTION_CURE_ONLY = "Cure Only"
OPTION_COLD_STORE = "Cold Store"
OPTION_EXTRACT_CURE = "Extract Cure"
SPECTRUM_PRESETS = {"Seedling": 20, "Vegetative": 50, "Bloom": 100}

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
    def factory(device: VivosunDeviceInfo) -> list[SelectEntity]:
        if client_model_token(device.client_id) == "VSPLUGA22":
            return [VivosunA22ModeSelect(coordinator, device.device_id, port) for port in A22_PORTS]
        if device.device_type == "curing_box":
            return [VivosunCureModeSelect(coordinator, device.device_id)]
        if device.device_type == "vseed":
            return [VivosunSeedStageSelect(coordinator, device.device_id)]
        result: list[SelectEntity] = []
        if supports_local_program_mode(coordinator, device.device_id, "light"):
            result.append(VivosunLightModeSelect(coordinator, device.device_id))
        if (device.device_type == "controller"
                and device.device_id in entry.options.get(OPTION_SPECTRUM_CONTROLLERS, [])):
            result.append(VivosunSpectrumSelect(coordinator, device.device_id))
        return result

    watch_devices(entry, coordinator, async_add_entities, factory)


class VivosunLightModeSelect(CoordinatorEntity[VivosunCoordinator], SelectEntity):  # type: ignore[misc]
    """Select the saved E42A light program without writing its settings or level."""

    _attr_has_entity_name = True
    _attr_name = "Grow Light Mode"
    _attr_translation_key = "grow_light_mode"

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_options = ["Manual", "Cycle"]
        self._attr_unique_id = f"vivosun_growhub_{device_id}_light_mode"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return (is_entity_available(self.coordinator, self._device_id)
                and supports_local_program_mode(self.coordinator, self._device_id, "light"))

    @property
    def current_option(self) -> str | None:
        mode = self.coordinator.reported_operation_mode(self._device_id, "light")
        return self._attr_options[mode] if type(mode) is int and mode in (0, 1) else None

    async def async_select_option(self, option: str) -> None:
        require_selected_control(self.coordinator, self._device_id, "light")
        if not supports_local_program_mode(self.coordinator, self._device_id, "light"):
            raise ServiceValidationError("Light program modes are not verified for this controller")
        if option not in self._attr_options:
            raise ServiceValidationError("Unsupported light program mode")
        await self.coordinator.async_publish_shadow_update(
            {"state": {"desired": {"light": {"mode": self._attr_options.index(option)}}}},
            device_id=self._device_id,
        )


class VivosunA22ModeSelect(CoordinatorEntity[VivosunCoordinator], SelectEntity):  # type: ignore[misc]
    """Configure the A22 firmware mode for one physical output."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cog-outline"

    def __init__(self, coordinator: VivosunCoordinator, device_id: str, port: str) -> None:
        super().__init__(coordinator)
        if port not in A22_PORTS:
            raise ValueError("Unknown A22 port")
        self._device_id = device_id
        self._port = port
        self._attr_options = ["Manual", "Cycle", "Auto"]
        label = port.upper() if port.startswith("usb") else f"Outlet {port[-1]}"
        self._attr_name = f"{label} Mode"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_{port}_mode"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def current_option(self) -> str | None:
        mode = self.coordinator.a22_settings(self._device_id, self._port).status.mode
        return self._attr_options[mode] if type(mode) is int and mode in (0, 1, 2) else None

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise ServiceValidationError("Unsupported A22 mode")
        await self.coordinator.async_set_a22_mode(self._device_id, self._port, self._attr_options.index(option))


class VivosunSpectrumSelect(CoordinatorEntity[VivosunCoordinator], SelectEntity):  # type: ignore[misc]
    """Vivosun app growth-spectrum presets, enabled per controller."""

    _attr_has_entity_name = True
    _attr_name = "Spectrum"
    _attr_translation_key = "spectrum"
    _attr_icon = "mdi:palette"

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_spectrum"
        self._attr_options = list(SPECTRUM_PRESETS)

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def current_option(self) -> str | None:
        reported = shadow_slice(self.coordinator, self._device_id, "confirmed_spectrum")
        value = reported.get("spec")
        return next((label for label, code in SPECTRUM_PRESETS.items() if type(value) is int and code == value), None)

    async def async_select_option(self, option: str) -> None:
        if option not in SPECTRUM_PRESETS:
            raise ServiceValidationError(f"Unknown spectrum preset: {option}")
        if not self.available:
            raise HomeAssistantError("Spectrum controller is unavailable")
        reported = shadow_slice(self.coordinator, self._device_id, "confirmed_spectrum")
        mode = reported.get("mode")
        in_plan = reported.get("inPlan")
        if type(mode) is not int or mode != 0 or not (
            in_plan is False or (type(in_plan) is int and in_plan == 0)
        ):
            raise ServiceValidationError("Spectrum requires reported manual mode with no active plan")
        await self.coordinator.async_publish_shadow_update(
            build_light_spectrum_payload(SPECTRUM_PRESETS[option]), device_id=self._device_id, qos=1
        )


class VivosunSeedStageSelect(CoordinatorEntity[VivosunCoordinator], SelectEntity):  # type: ignore[misc]
    """Select the S40 growth stage; reflect only reported firmware state."""

    _attr_has_entity_name = True
    _attr_name = "Growth Stage"
    _attr_icon = "mdi:sprout"

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_options = list(STAGES)
        self._attr_unique_id = f"vivosun_growhub_{device_id}_seed_stage"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def current_option(self) -> str | None:
        state = reported_seed(self.coordinator, self._device_id).get("state")
        return stage_name(state) if type(state) is int else None  # type: ignore[arg-type]

    async def async_select_option(self, option: str) -> None:
        if option not in STAGES:
            raise ValueError("Unknown VSeed stage")
        await self.coordinator.async_publish_shadow_update(
            command(("state",), STAGES.index(option)), device_id=self._device_id, qos=1
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

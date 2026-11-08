"""Stored VSeed S40 growth-stage settings on Home Assistant number entities."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .discovery import watch_devices
from .entity_helpers import build_device_info, is_entity_available
from .models import client_model_token
from .ports import A22_PORTS
from .vseed import SETTING_LIMITS, STAGES, command, reported_seed

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import DeviceInfo as VivosunDeviceInfo
    from .models import RuntimeData

_SETTINGS = {
    "tTemp": ("Root Temperature", 100, UnitOfTemperature.CELSIUS),
    "tHumi": ("Dome Humidity", 100, PERCENTAGE),
    "lgtLv": ("Light Intensity", 1, PERCENTAGE),
    "lgtOnDur": ("Light Duration", 1, "s"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up native number controls for VSeed stages and A22 ports."""
    runtime = cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])
    if runtime.coordinator is None:
        return
    coordinator = runtime.coordinator
    def factory(device: VivosunDeviceInfo) -> list[NumberEntity]:
        if device.device_type == "vseed":
            return [VivosunSeedSettingNumber(coordinator, device.device_id, phase, key)
                    for phase in (1, 2, 3) for key in _SETTINGS]
        if client_model_token(device.client_id) == "VSPLUGA22":
            return [VivosunA22Number(coordinator, device.device_id, port, field)
                    for port in A22_PORTS
                    for field in ("onDur", "offDur", "tMin", "tMax", "hMin", "hMax")]
        return []

    watch_devices(entry, coordinator, async_add_entities, factory)


class VivosunA22Number(CoordinatorEntity[VivosunCoordinator], NumberEntity):  # type: ignore[misc]
    """A22 firmware Cycle durations and automatic trigger thresholds."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: VivosunCoordinator, device_id: str, port: str, field: str) -> None:
        super().__init__(coordinator)
        if port not in A22_PORTS or field not in ("onDur", "offDur", "tMin", "tMax", "hMin", "hMax"):
            raise ValueError("Unknown A22 setting")
        self._device_id = device_id
        self._port = port
        self._field = field
        label = port.upper() if port.startswith("usb") else f"Outlet {port[-1]}"
        description = {
            "onDur": "Cycle On Duration", "offDur": "Cycle Off Duration",
            "tMin": "Low Temperature", "tMax": "High Temperature",
            "hMin": "Low Humidity", "hMax": "High Humidity",
        }[field]
        self._attr_name = f"{label} {description}"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_{port}_{field}"
        if field in ("onDur", "offDur"):
            self._attr_device_class = NumberDeviceClass.DURATION
            self._attr_native_min_value = 1
            self._attr_native_max_value = 86400
            self._attr_native_step = 1
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
        elif field.startswith("t"):
            self._attr_device_class = NumberDeviceClass.TEMPERATURE
            self._attr_native_min_value = -40
            self._attr_native_max_value = 125
            self._attr_native_step = 0.1
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        else:
            self._attr_native_min_value = 0
            self._attr_native_max_value = 100
            self._attr_native_step = 0.01
            self._attr_native_unit_of_measurement = PERCENTAGE

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def native_value(self) -> float | None:
        settings = self.coordinator.a22_settings(self._device_id, self._port)
        if self._field == "onDur":
            raw = settings.cycle.on
        elif self._field == "offDur":
            raw = settings.cycle.off
        else:
            raw = {
                "tMin": settings.auto.temp_low, "tMax": settings.auto.temp_high,
                "hMin": settings.auto.humidity_low, "hMax": settings.auto.humidity_high,
            }[self._field]
        if raw is None:
            return None
        return float(raw / 100) if self._field.startswith(("t", "h")) else float(raw)

    async def async_set_native_value(self, value: float) -> None:
        scale = 100 if self._field.startswith(("t", "h")) else 1
        raw = round(value * scale)
        if abs(value * scale - raw) > 1e-6:
            raise ValueError("A22 setting precision is invalid")
        await self.coordinator.async_set_a22_setting(
            self._device_id, self._port, "cycle" if self._field.endswith("Dur") else "auto", self._field, raw,
        )


class VivosunSeedSettingNumber(CoordinatorEntity[VivosunCoordinator], NumberEntity):  # type: ignore[misc]
    """Edit one stored stage recipe value without implying live actuator state."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VivosunCoordinator, device_id: str, phase: int, key: str) -> None:
        super().__init__(coordinator)
        if phase not in (1, 2, 3) or key not in _SETTINGS:
            raise ValueError("Unsupported VSeed stage setting")
        self._device_id = device_id
        self._phase = phase
        self._key = key
        name, scale, unit = _SETTINGS[key]
        low, high, step = SETTING_LIMITS[key]
        self._scale = scale
        self._attr_name = f"{STAGES[phase]} {name}"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_seed_stage{phase}_{key}"
        self._attr_native_min_value = low / scale
        self._attr_native_max_value = high / scale
        self._attr_native_step = step / scale
        self._attr_native_unit_of_measurement = unit
        self._attr_mode = NumberMode.BOX

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def native_value(self) -> float | None:
        stored = reported_seed(self.coordinator, self._device_id).get(f"stage{self._phase}")
        value = stored.get(self._key) if isinstance(stored, dict) else None
        return value / self._scale if type(value) is int else None

    async def async_set_native_value(self, value: float) -> None:
        """Publish the exact stored recipe field; reported echo owns the UI state."""
        scaled = value * self._scale
        if not scaled.is_integer():
            raise ValueError("VSeed setting precision is invalid")
        await self.coordinator.async_publish_shadow_update(
            command((f"stage{self._phase}", self._key), int(scaled)),
            device_id=self._device_id,
            qos=1,
        )

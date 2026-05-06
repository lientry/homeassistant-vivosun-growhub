"""Sensor platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_TEMP_UNIT,
    DFAN_LEVEL_MAP,
    DOMAIN,
    SENSOR_UNAVAILABLE_SENTINEL,
    TEMP_SCALE_FACTOR,
    WATER_LEVEL_SCALE_FACTOR,
)
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, plan_slice, plan_stage_cache, sensor_slice
from .shadow import cfan_shadow_to_percentage, dfan_shadow_to_percentage

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData

_OPTIONS_TEMP_UNIT = "temp_unit"
_UNIT_CELSIUS = "celsius"
_UNIT_FAHRENHEIT = "fahrenheit"
_PLAN_AUTO_MODE = 2

_PLAN_DEVICE_ENTITIES: tuple[tuple[str, str, str, str], ...] = (
    ("cfan", "Circulator Fan", "mdi:fan", "fan"),
    ("dfan", "Duct Fan", "mdi:fan-auto", "fan"),
    ("hmdf", "Humidifier", "mdi:air-humidifier", "humidifier"),
    ("dhmdf", "Dehumidifier", "mdi:water-off", "dehumidifier"),
    ("drip", "Drip Irrigation", "mdi:water-sync", "drip"),
    ("heat", "Heater", "mdi:radiator", "heater"),
    ("aircd", "Air Conditioner", "mdi:air-conditioner", "air_conditioner"),
)
_AIR_CONDITIONER_FUNCTIONS: dict[int, str] = {
    1: "Cooling",
    2: "Heating",
    3: "Dry",
    4: "Fan",
}


@dataclass(frozen=True, kw_only=True)
class VivosunSensorDescription(SensorEntityDescription):  # type: ignore[misc]
    """Description for a Vivosun channel sensor entity."""

    channel_key: str
    channel_key_aliases: tuple[str, ...] = ()
    quantity: str
    state_class: SensorStateClass = SensorStateClass.MEASUREMENT

    @property
    def channel_key_lookup(self) -> tuple[str, ...]:
        """Return the ordered tuple of channel keys to consult when reading."""
        return (self.channel_key, *self.channel_key_aliases)


_ALL_SENSOR_DESCRIPTIONS: tuple[VivosunSensorDescription, ...] = (
    VivosunSensorDescription(
        key="inside_temperature",
        name="Inside Temperature",
        channel_key="inTemp",
        channel_key_aliases=("bTemp",),
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="inside_humidity",
        name="Inside Humidity",
        channel_key="inHumi",
        channel_key_aliases=("bHumi",),
        quantity="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="inside_vpd",
        name="Inside VPD",
        channel_key="inVpd",
        channel_key_aliases=("bVpd",),
        quantity="vpd",
        icon="mdi:leaf",
        native_unit_of_measurement="kPa",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_temperature",
        name="Outside Temperature",
        channel_key="outTemp",
        channel_key_aliases=("pTemp",),
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_humidity",
        name="Outside Humidity",
        channel_key="outHumi",
        channel_key_aliases=("pHumi",),
        quantity="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_vpd",
        name="Outside VPD",
        channel_key="outVpd",
        channel_key_aliases=("pVpd",),
        quantity="vpd",
        icon="mdi:leaf",
        native_unit_of_measurement="kPa",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="probe_temperature",
        name="Probe Temperature",
        channel_key="pTemp",
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="probe_humidity",
        name="Probe Humidity",
        channel_key="pHumi",
        quantity="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="probe_vpd",
        name="Probe VPD",
        channel_key="pVpd",
        quantity="vpd",
        icon="mdi:leaf",
        native_unit_of_measurement="kPa",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="water_level",
        name="Water Level",
        channel_key="waterLv",
        quantity="water_level",
        icon="mdi:waves-arrow-up",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="core_temperature",
        name="Core Temperature",
        channel_key="coreTemp",
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    VivosunSensorDescription(
        key="wifi_signal",
        name="WiFi Signal",
        channel_key="rssi",
        quantity="signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
)

_DEVICE_TYPE_SENSORS: dict[str, frozenset[str]] = {
    "controller": frozenset({"inTemp", "inHumi", "inVpd", "outTemp", "outHumi", "outVpd", "coreTemp", "rssi"}),
    "humidifier": frozenset({"pTemp", "pHumi", "pVpd", "waterLv", "coreTemp"}),
    "heater": frozenset({"pTemp", "pHumi", "pVpd"}),
}


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun sensor entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    entities: list[SensorEntity] = []
    for device in coordinator.devices:
        allowed_keys = _DEVICE_TYPE_SENSORS.get(device.device_type, frozenset())
        for description in _ALL_SENSOR_DESCRIPTIONS:
            if description.channel_key in allowed_keys:
                entities.append(
                    VivosunChannelSensorEntity(coordinator, entry, description, device.device_id)
                )
        if device.device_type == "controller":
            entities.append(VivosunPlanStageSensor(coordinator, entry, device.device_id))
            entities.append(VivosunPlanLightSensor(coordinator, entry, device.device_id))
            for plan_key, plan_name, icon, entity_kind in _PLAN_DEVICE_ENTITIES:
                entities.append(
                    VivosunPlanDeviceSensor(
                        coordinator,
                        entry,
                        device.device_id,
                        plan_key=plan_key,
                        name=plan_name,
                        icon=icon,
                        entity_kind=entity_kind,
                    )
                )
    async_add_entities(entities)


class VivosunChannelSensorEntity(CoordinatorEntity[VivosunCoordinator], SensorEntity):  # type: ignore[misc]
    """Representation of a channel telemetry sensor."""

    entity_description: VivosunSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VivosunCoordinator,
        entry: ConfigEntry,
        description: VivosunSensorDescription,
        device_id: str,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._device_id = device_id
        self._attr_name = description.name
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_unique_id = f"vivosun_growhub_{device_id}_{description.channel_key}"

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        return build_device_info(self.coordinator, self._device_id)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return native measurement unit."""
        if self.entity_description.quantity == "temperature":
            if self._temp_unit() == _UNIT_FAHRENHEIT:
                return str(UnitOfTemperature.FAHRENHEIT)
            return str(UnitOfTemperature.CELSIUS)
        return cast("str | None", self.entity_description.native_unit_of_measurement)

    @property
    def native_value(self) -> float | None:
        """Return current sensor value from the latest point-log sample."""
        raw_value = self._raw_channel_value()
        if raw_value is None:
            return None

        if self.entity_description.quantity == "signal_strength":
            return float(raw_value)
        if self.entity_description.quantity == "water_level":
            return raw_value / WATER_LEVEL_SCALE_FACTOR
        value = raw_value / TEMP_SCALE_FACTOR
        if self.entity_description.quantity == "temperature" and self._temp_unit() == _UNIT_FAHRENHEIT:
            return (value * 9 / 5) + 32
        return value

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return extra attributes for non-standard quantities."""
        if self.entity_description.quantity == "vpd":
            return {"quantity": "vpd"}
        return None

    def _raw_channel_value(self) -> int | None:
        sensors = sensor_slice(self.coordinator, self._device_id)
        for key in self.entity_description.channel_key_lookup:
            value = sensors.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
        return None

    def _temp_unit(self) -> str:
        configured_unit = self._entry.options.get(_OPTIONS_TEMP_UNIT, DEFAULT_TEMP_UNIT)
        if configured_unit == _UNIT_FAHRENHEIT:
            return _UNIT_FAHRENHEIT
        return _UNIT_CELSIUS


def _get_active_stage_info(coordinator: VivosunCoordinator, device_id: str) -> tuple[str | None, dict[str, object]]:
    """Return (stage_name, stage_content) for the active plan stage, or (None, {})."""
    plan = plan_slice(coordinator, device_id)
    active_key = plan.get("active_stage")
    if not isinstance(active_key, str) or not active_key:
        return None, {}
    stages = plan.get("stages")
    if not isinstance(stages, dict):
        return None, {}
    stage_entry = stages.get(active_key)
    if not isinstance(stage_entry, dict):
        return None, {}
    stage_id = stage_entry.get("stage_id", "")
    if not stage_id:
        return None, {}
    cache = plan_stage_cache(coordinator)
    info = cache.get(stage_id)
    if info is None:
        return None, {}
    return getattr(info, "stage_name", None), getattr(info, "content", {})


def _seconds_from_midnight() -> int:
    """Return seconds elapsed since midnight in Home Assistant local time."""
    now = dt_util.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((now - midnight).total_seconds())


def _normalize_slot_time(value: object) -> int:
    """Normalize slot time to seconds-from-midnight.

    Vivosun planStageContent appears to use minutes-from-midnight in some payloads,
    but seconds-from-midnight is also plausible. Values <= 1440 are treated as
    minutes; larger values are treated as seconds.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    if value <= 1440:
        return value * 60
    return value


def _format_time(seconds: int) -> str:
    """Format seconds-from-midnight as HH:MM."""
    h, m = divmod(seconds // 60, 60)
    return f"{h:02d}:{m:02d}"


def _active_slot(slots: list[object]) -> tuple[dict[str, object], int, int | None] | None:
    """Return the currently active timed slot plus its start/next-change time."""
    normalized: list[tuple[int, dict[str, object]]] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        normalized.append((_normalize_slot_time(slot.get("time", 0)), slot))
    if not normalized:
        return None

    normalized.sort(key=lambda item: item[0])
    now_secs = _seconds_from_midnight()
    next_index = next((idx for idx, (time_value, _) in enumerate(normalized) if time_value > now_secs), None)
    current_index = len(normalized) - 1 if next_index is None or next_index == 0 else next_index - 1
    current_time, current_slot = normalized[current_index]
    next_time = normalized[next_index][0] if next_index is not None else normalized[0][0]
    return current_slot, current_time, next_time


def _compute_light_schedule(content: dict[str, object]) -> dict[str, Any]:
    """Compute light schedule info from plan stage content."""
    light = content.get("light")
    if not isinstance(light, dict):
        return {}
    slots = light.get("slot")
    if not isinstance(slots, list) or not slots:
        return {}

    on_slots = [s for s in slots if isinstance(s, dict) and s.get("level", 0) > 0]
    off_slots = [s for s in slots if isinstance(s, dict) and s.get("level", 0) == 0]

    if not on_slots:
        return {"state": "off", "level": 0, "on_hours": 0}

    first_on = on_slots[0]
    level = first_on.get("level", 0)
    spectrum = light.get("spec", 0)
    on_times = sorted(_normalize_slot_time(s.get("time", 0)) for s in on_slots)
    off_times = sorted(
        _normalize_slot_time(s.get("time", 0)) for s in off_slots if _normalize_slot_time(s.get("time", 0)) >= 0
    )

    start_time = on_times[0] if on_times else 0
    end_time = off_times[0] if off_times else None

    if end_time is None:
        on_duration = 86400
    elif end_time > start_time:
        on_duration = end_time - start_time
    else:
        on_duration = (86400 - start_time) + end_time

    on_hours = round(on_duration / 3600, 1)

    now_secs = _seconds_from_midnight()
    if end_time is None:
        is_on = True
        remaining = 0
    elif end_time > start_time:
        is_on = start_time <= now_secs < end_time
        if is_on:
            remaining = end_time - now_secs
        else:
            remaining = -(start_time - now_secs) if now_secs < start_time else -(86400 - now_secs + start_time)
    else:
        is_on = now_secs >= start_time or now_secs < end_time
        if is_on:
            remaining = 86400 - now_secs + end_time if now_secs >= start_time else end_time - now_secs
        else:
            remaining = -(start_time - now_secs)

    remaining_h = abs(remaining) / 3600

    return {
        "state": "on" if is_on else "off",
        "level": level,
        "spectrum": spectrum,
        "on_hours": on_hours,
        "on_time": _format_time(start_time),
        "off_time": _format_time(end_time) if end_time is not None else "00:00",
        "remaining_hours": round(remaining_h, 1),
        "remaining_label": f"{round(remaining_h, 1)}h until {'off' if is_on else 'on'}",
    }


def _compute_fan_schedule(content: dict[str, object], fan_key: str) -> dict[str, Any]:
    """Compute fan schedule info from plan stage content."""
    fan = content.get(fan_key)
    if not isinstance(fan, dict):
        return {}
    slots = fan.get("slot")
    if not isinstance(slots, list) or not slots:
        return {}

    active_slot = _active_slot(slots)
    if active_slot is None:
        return {}
    slot, slot_time, next_time = active_slot

    mode = slot.get("mode", 0)
    if mode == 1:
        lv_on = _normalize_fan_percentage(slot.get("lvOn", slot.get("level", 0)), fan_key=fan_key)
        lv_off = _normalize_fan_percentage(slot.get("lvOff", 0), fan_key=fan_key)
        on_dur = slot.get("onDur", 0)
        off_dur = slot.get("offDur", 0)
        if not isinstance(on_dur, int) or not isinstance(off_dur, int):
            return {}
        on_min = round(on_dur / 60)
        off_min = round(off_dur / 60)
        return {
            "mode": "cycle",
            "level_on": lv_on,
            "level_off": lv_off,
            "on_minutes": on_min,
            "off_minutes": off_min,
            "slot_time": _format_time(slot_time),
            "next_time": _format_time(next_time) if next_time is not None else None,
            "cycle": f"{on_min}m on / {off_min}m off",
        }
    if mode == _PLAN_AUTO_MODE:
        level_min = _normalize_fan_percentage(slot.get("lvMin"), fan_key=fan_key)
        level_max = _normalize_fan_percentage(slot.get("lvMax"), fan_key=fan_key)
        standby_status = _normalize_dfan_standby_status(slot.get("lvMin")) if fan_key == "dfan" else None
        return {
            "mode": "auto",
            "standby_speed": level_min,
            "standby_status": standby_status,
            "trigger_speed": level_max,
            "temperature_min": _normalize_scaled_plan_value(slot.get("tMin")),
            "temperature_max": _normalize_scaled_plan_value(slot.get("tMax")),
            "humidity_min": _normalize_scaled_plan_value(slot.get("hMin")),
            "humidity_max": _normalize_scaled_plan_value(slot.get("hMax")),
            "vpd_min": _normalize_scaled_plan_value(slot.get("vpdMin")),
            "vpd_max": _normalize_scaled_plan_value(slot.get("vpdMax")),
            "slot_time": _format_time(slot_time),
            "next_time": _format_time(next_time) if next_time is not None else None,
        }
    else:
        level = _normalize_fan_percentage(slot.get("level", 0), fan_key=fan_key)
        return {
            "mode": "manual",
            "level": level,
            "slot_time": _format_time(slot_time),
            "next_time": _format_time(next_time) if next_time is not None else None,
        }


def _format_percent_value(value: object) -> str | None:
    if isinstance(value, int):
        return f"{value}%"
    return None


def _normalize_plan_int(value: object) -> int | None:
    if not isinstance(value, int):
        return None
    if value == SENSOR_UNAVAILABLE_SENTINEL:
        return None
    return value


def _normalize_scaled_plan_value(value: object) -> float | None:
    normalized = _normalize_plan_int(value)
    if normalized is None:
        return None
    return normalized / 100


def _normalize_fan_percentage(value: object, *, fan_key: str) -> int | None:
    normalized = _normalize_plan_int(value)
    if normalized is None:
        return None
    if fan_key == "cfan":
        return cfan_shadow_to_percentage(normalized)
    return dfan_shadow_to_percentage(normalized)


def _normalize_dfan_standby_status(value: object) -> str | None:
    normalized = _normalize_plan_int(value)
    if normalized is None:
        return None
    if normalized == 0:
        return "Off"
    try:
        return f"S{DFAN_LEVEL_MAP.index(normalized)}"
    except ValueError:
        percentage = dfan_shadow_to_percentage(normalized)
        return f"{percentage}%" if percentage is not None else None


def _format_humidity_target(value: object) -> str | None:
    if isinstance(value, float):
        return f"{value:.1f}%"
    return None


def _format_temperature_target(value: object) -> str | None:
    if isinstance(value, float):
        return f"{value:.1f}C"
    return None


def _format_vpd_target(value: object) -> str | None:
    if isinstance(value, float):
        return f"{value:.1f}kPa"
    return None


def _stringify_plan_attribute(value: object) -> object:
    if value is None:
        return "Not set"
    if isinstance(value, dict):
        return {key: _stringify_plan_attribute(item) for key, item in value.items()}
    return value


def _compute_recipe_device_schedule(content: dict[str, object], recipe_key: str) -> dict[str, Any]:
    recipe = content.get(recipe_key)
    if not isinstance(recipe, dict):
        return {}
    slots = recipe.get("slot")
    if not isinstance(slots, list) or not slots:
        return {}

    active_slot = _active_slot(slots)
    if active_slot is None:
        return {}
    slot, slot_time, next_time = active_slot

    info: dict[str, Any] = {
        "mode": slot.get("mode", 0),
        "slot_time": _format_time(slot_time),
        "next_time": _format_time(next_time) if next_time is not None else None,
    }

    if recipe_key == "hmdf":
        if slot.get("mode") == _PLAN_AUTO_MODE:
            info["state"] = "auto"
            info["target_humidity"] = _normalize_scaled_plan_value(slot.get("tHumi"))
            info["target_vpd"] = _normalize_scaled_plan_value(slot.get("tVpd"))
            info["control_basis"] = "vpd" if slot.get("vpdSwit") else "humidity"
            info["level_on"] = _normalize_plan_int(slot.get("lvOn"))
        else:
            info["state"] = "manual"
            info["level"] = _normalize_plan_int(slot.get("level", 0))
        return info

    if recipe_key == "dhmdf":
        if slot.get("mode") == _PLAN_AUTO_MODE:
            info["state"] = "auto" if slot.get("state", 0) else "off"
            info["target_humidity"] = _normalize_scaled_plan_value(slot.get("tHumi"))
        else:
            info["state"] = "manual" if slot.get("state", 0) else "off"
        return info

    if recipe_key == "drip":
        if slot.get("mode") == 1:
            on_dur = slot.get("onDur")
            off_dur = slot.get("offDur")
            if not isinstance(on_dur, int) or not isinstance(off_dur, int):
                return {}
            info["state"] = "cycle"
            info["level"] = slot.get("level", 0)
            info["on_minutes"] = round(on_dur / 60)
            info["off_minutes"] = round(off_dur / 60)
            info["cycle"] = f"{info['on_minutes']}m on / {info['off_minutes']}m off"
        else:
            level = _normalize_plan_int(slot.get("level", 0))
            info["state"] = "manual"
            info["level"] = level
        return info

    if recipe_key == "heat":
        info["state"] = "on" if slot.get("state", 0) else "off"
        return info

    if recipe_key == "aircd":
        info["state"] = "on" if slot.get("state", 0) else "off"
        info["function"] = _normalize_plan_int(slot.get("func"))
        info["target_temperature"] = _normalize_scaled_plan_value(slot.get("tTemp"))
        info["target_humidity"] = _normalize_scaled_plan_value(slot.get("tHumi"))
        return info

    return {}


def _recipe_device_label(entity_kind: str, info: dict[str, Any]) -> str:
    if entity_kind == "fan":
        mode = info.get("mode", "off")
        if mode == "cycle":
            return cast("str", info.get("cycle", "Not set"))
        if mode == "auto":
            standby_status = info.get("standby_status")
            if isinstance(standby_status, str):
                return f"Auto Standby {standby_status}"
            return "Auto"
        level = info.get("level", 0)
        return f"{level}%" if level > 0 else "Off"

    if entity_kind == "humidifier":
        if info.get("state") == "auto":
            if info.get("control_basis") == "vpd":
                return "Auto VPD-based"
            return "Auto Humidity-based"
        level = _format_percent_value(info.get("level"))
        return level if level and level != "0%" else "Off"

    if entity_kind == "dehumidifier":
        if info.get("state") == "auto":
            return "Auto Humidity"
        return "On" if info.get("state") == "manual" else "Off"

    if entity_kind == "drip":
        if info.get("state") == "cycle":
            return cast("str", info.get("cycle", "Cycle"))
        level = _format_percent_value(info.get("level"))
        return level if level and level != "0%" else "Off"

    if entity_kind == "heater":
        return "On" if info.get("state") == "on" else "Off"

    if entity_kind == "air_conditioner":
        if info.get("state") != "on":
            return "Off"
        function = info.get("function")
        function_name = _AIR_CONDITIONER_FUNCTIONS.get(function) if isinstance(function, int) else None
        if function_name:
            return function_name
        if isinstance(function, int):
            return f"Mode {function}"
        return "On"

    return "Not set"


class VivosunPlanStageSensor(CoordinatorEntity[VivosunCoordinator], SensorEntity):  # type: ignore[misc]
    """Sensor showing the active grow plan stage name."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:sprout"

    def __init__(self, coordinator: VivosunCoordinator, entry: ConfigEntry, device_id: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device_id
        self._attr_name = "Grow Plan Stage"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_plan_stage"

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def native_value(self) -> str | None:
        name, _ = _get_active_stage_info(self.coordinator, self._device_id)
        if name:
            return name
        return "Manual"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        plan = plan_slice(self.coordinator, self._device_id)
        active_key = plan.get("active_stage")
        stages = plan.get("stages")
        attrs: dict[str, Any] = {}
        if active_key:
            attrs["active_stage_key"] = active_key
        if isinstance(stages, dict):
            for key, entry in stages.items():
                if isinstance(entry, dict) and entry.get("start_time", 0) > 0:
                    attrs[f"{key}_started"] = datetime.fromtimestamp(
                        entry["start_time"], tz=UTC
                    ).isoformat()
        return attrs if attrs else None


class VivosunPlanLightSensor(CoordinatorEntity[VivosunCoordinator], SensorEntity):  # type: ignore[misc]
    """Sensor showing light schedule from the active plan stage."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:lightbulb-on-outline"

    def __init__(self, coordinator: VivosunCoordinator, entry: ConfigEntry, device_id: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device_id
        self._attr_name = "Plan Light Schedule"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_plan_light"

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def native_value(self) -> str | None:
        _, content = _get_active_stage_info(self.coordinator, self._device_id)
        if not content:
            return "Not set"
        info = _compute_light_schedule(content)
        if not info:
            return "Not set"
        return info.get("remaining_label")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        _, content = _get_active_stage_info(self.coordinator, self._device_id)
        if not content:
            return {"state": "manual"}
        info = _compute_light_schedule(content)
        if not info:
            return {"state": "manual"}
        return {
            "state": info.get("state"),
            "level": info.get("level"),
            "spectrum": info.get("spectrum"),
            "on_hours": info.get("on_hours"),
            "on_time": info.get("on_time"),
            "off_time": info.get("off_time"),
            "remaining_hours": info.get("remaining_hours"),
        }


class VivosunPlanDeviceSensor(CoordinatorEntity[VivosunCoordinator], SensorEntity):  # type: ignore[misc]
    """Sensor showing recipe schedule info from the active plan stage."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VivosunCoordinator,
        entry: ConfigEntry,
        device_id: str,
        *,
        plan_key: str,
        name: str,
        icon: str,
        entity_kind: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device_id
        self._plan_key = plan_key
        self._entity_kind = entity_kind
        self._attr_name = f"Plan {name} Schedule"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_plan_{plan_key}"
        self._attr_icon = icon

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def native_value(self) -> str | None:
        _, content = _get_active_stage_info(self.coordinator, self._device_id)
        if not content:
            return "Not set"
        if self._entity_kind == "fan":
            info = _compute_fan_schedule(content, self._plan_key)
        else:
            info = _compute_recipe_device_schedule(content, self._plan_key)
        if not info:
            return "Not set"
        return _recipe_device_label(self._entity_kind, info)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        _, content = _get_active_stage_info(self.coordinator, self._device_id)
        if not content:
            return {"state": "manual"}
        if self._entity_kind == "fan":
            info = _compute_fan_schedule(content, self._plan_key)
        else:
            info = _compute_recipe_device_schedule(content, self._plan_key)
        if not info:
            return {"state": "manual"}
        return cast("dict[str, Any]", _stringify_plan_attribute(info))

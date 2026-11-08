"""Sensor platform for the Vivosun GrowHub integration."""

from __future__ import annotations

import math
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CFAN_NATURAL_WIND_VALUE,
    DEFAULT_TEMP_UNIT,
    DFAN_LEVEL_MAP,
    DOMAIN,
    DWC_WATER_LEVEL_SCALE_FACTOR,
    SENSOR_UNAVAILABLE_SENTINEL,
    TEMP_SCALE_FACTOR,
    WATER_LEVEL_SCALE_FACTOR,
)
from .coordinator import VivosunCoordinator
from .discovery import watch_devices, watch_gardens
from .entity_helpers import build_device_info, is_entity_available, sensor_slice
from .garden_display import GardenScheduleClock, GardenSelection, garden_stage_clock, select_garden_plan
from .models import client_model_token
from .shadow import cfan_shadow_to_percentage, dfan_shadow_to_percentage
from .vseed import reported_seed

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .gardens import Garden
    from .models import DeviceInfo as VivosunDeviceInfo
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
_GARDEN_SCHEDULE_ENTITIES = (
    ("light", "Light", "mdi:lightbulb-on-outline", "light"),
    *_PLAN_DEVICE_ENTITIES,
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
    scale_factor: int | None = None

    @property
    def channel_key_lookup(self) -> tuple[str, ...]:
        """Return the ordered tuple of channel keys to consult when reading."""
        return (self.channel_key, *self.channel_key_aliases)


_ALL_SENSOR_DESCRIPTIONS: tuple[VivosunSensorDescription, ...] = (
    VivosunSensorDescription(
        key="inside_temperature",
        name="Inside Temperature",
        channel_key="inTemp",
        channel_key_aliases=("pTemp",),
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="inside_humidity",
        name="Inside Humidity",
        channel_key="inHumi",
        channel_key_aliases=("pHumi",),
        quantity="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="inside_vpd",
        name="Inside VPD",
        channel_key="inVpd",
        channel_key_aliases=("pVpd",),
        quantity="vpd",
        icon="mdi:leaf",
        native_unit_of_measurement="kPa",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_temperature",
        name="Outside Temperature",
        channel_key="outTemp",
        channel_key_aliases=("bTemp",),
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_humidity",
        name="Outside Humidity",
        channel_key="outHumi",
        channel_key_aliases=("bHumi",),
        quantity="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_vpd",
        name="Outside VPD",
        channel_key="outVpd",
        channel_key_aliases=("bVpd",),
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

_VGROW_DWC_SENSORS: tuple[VivosunSensorDescription, ...] = (
    VivosunSensorDescription(
        key="water_level",
        name="Water Level",
        channel_key="waterLv",
        quantity="water_level",
        scale_factor=DWC_WATER_LEVEL_SCALE_FACTOR,
        icon="mdi:waves-arrow-up",
        native_unit_of_measurement=PERCENTAGE,
    ),
    VivosunSensorDescription(
        key="water_temperature",
        name="Water Temperature",
        channel_key="waterT",
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
)

_VSEED_SENSORS: tuple[VivosunSensorDescription, ...] = (
    VivosunSensorDescription(
        key="seed_root_temperature", name="Root Temperature", channel_key="rTemp",
        quantity="temperature", device_class=SensorDeviceClass.TEMPERATURE,
    ),
    VivosunSensorDescription(
        key="seed_dome_temperature", name="Dome Temperature", channel_key="pTemp",
        quantity="temperature", device_class=SensorDeviceClass.TEMPERATURE,
    ),
    VivosunSensorDescription(
        key="seed_dome_humidity", name="Dome Humidity", channel_key="pHumi",
        quantity="humidity", device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
    ),
    VivosunSensorDescription(
        key="seed_vpd", name="VPD", channel_key="pVpd",
        quantity="vpd", native_unit_of_measurement="kPa", icon="mdi:leaf",
    ),
)

_DEVICE_TYPE_SENSORS: dict[str, frozenset[str]] = {
    "a22": frozenset({"pTemp", "pHumi", "pVpd"}),
    "controller": frozenset({"inTemp", "inHumi", "inVpd", "outTemp", "outHumi", "outVpd", "coreTemp", "rssi"}),
    "dehumidifier": frozenset({"pTemp", "pHumi", "pVpd"}),
    "humidifier": frozenset({"pTemp", "pHumi", "pVpd", "waterLv", "coreTemp"}),
    "heater": frozenset({"pTemp", "pHumi", "pVpd"}),
    "air_conditioner": frozenset({"pTemp", "pHumi", "pVpd"}),
    "curing_box": frozenset({"pTemp", "pHumi", "pVpd", "outTemp", "outHumi", "outVpd", "coreTemp", "rssi"}),
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

    def factory(device: VivosunDeviceInfo) -> list[SensorEntity]:
        entities: list[SensorEntity] = []
        if device.device_type == "vseed":
            entities.extend(
                VivosunChannelSensorEntity(coordinator, entry, description, device.device_id)
                for description in _VSEED_SENSORS
            )
            entities.append(VivosunSeedLiveLightSensor(coordinator, device.device_id))
        allowed_keys = _DEVICE_TYPE_SENSORS.get(device.device_type, frozenset())
        for description in _ALL_SENSOR_DESCRIPTIONS:
            if description.channel_key in allowed_keys:
                entities.append(
                    VivosunChannelSensorEntity(coordinator, entry, description, device.device_id)
                )
        if client_model_token(device.client_id) == "VSCTLY18":
            for description in _VGROW_DWC_SENSORS:
                entities.append(VivosunChannelSensorEntity(coordinator, entry, description, device.device_id))
        return entities

    watch_devices(entry, coordinator, async_add_entities, factory)

    def garden_factory(garden: Garden) -> list[SensorEntity]:
        entities: list[SensorEntity] = [
            VivosunGardenSensor(coordinator, entry, garden.scene_id, "recipe", "Recipe", "mdi:book-open-variant"),
            VivosunGardenSensor(coordinator, entry, garden.scene_id, "stage", "Current Stage", "mdi:sprout"),
        ]
        relevant = coordinator.garden_schedule_keys(garden.scene_id)
        entities.extend(
            VivosunGardenSensor(
                coordinator, entry, garden.scene_id, f"plan_{key}", f"{name} Schedule", icon,
                schedule_kind=kind,
            )
            for key, name, icon, kind in _GARDEN_SCHEDULE_ENTITIES if key in relevant
        )
        return entities

    watch_gardens(entry, coordinator, async_add_entities, garden_factory)


class VivosunGardenSensor(CoordinatorEntity[VivosunCoordinator], SensorEntity):  # type: ignore[misc]
    """Garden-configured intent, not evidence of physical device execution."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VivosunCoordinator,
        entry: ConfigEntry,
        scene_id: int,
        role: str,
        name: str,
        icon: str,
        *,
        schedule_kind: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._scene_id = scene_id
        self._role = role
        self._schedule_kind = schedule_kind
        self._attr_unique_id = f"vivosun_growhub_{entry.entry_id}_garden_{scene_id}_{role}"
        self._attr_name = name
        self._attr_icon = icon

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"garden:{self._entry_id}:{self._scene_id}")},
            manufacturer="VIVOSUN",
            model="Garden",
        )

    @property
    def available(self) -> bool:
        read = self.coordinator.garden_plan_read(self._scene_id)
        if read.status not in ("current", "refreshing") or read.plans is None:
            return False
        return self._schedule_kind is None or self._role.removeprefix("plan_") in (
            self.coordinator.garden_schedule_keys(self._scene_id)
        )

    def _selected(self) -> GardenSelection | None:
        read = self.coordinator.garden_plan_read(self._scene_id)
        if read.status not in ("current", "refreshing") or read.plans is None:
            return None
        return select_garden_plan(read.plans, int(datetime.now(tz=UTC).timestamp()))

    def _garden_timezone(self) -> str | None:
        inventory = self.coordinator.garden_inventory
        if inventory is None:
            return None
        garden = next((g for g in inventory.gardens if g.scene_id == self._scene_id), None)
        return garden.timezone_id if garden is not None else None

    def _schedule_clock(self, selected: GardenSelection) -> tuple[GardenScheduleClock | None, str]:
        timezone = self._garden_timezone()
        if selected.stage is None or timezone is None:
            return None, "unknown_time"
        return garden_stage_clock(selected.stage, timezone, datetime.now(tz=UTC))

    def _schedule(self, selected: GardenSelection) -> dict[str, Any]:
        stage = selected.stage
        clock, _ = self._schedule_clock(selected)
        if stage is None or stage.content_status != "valid" or stage.content is None or clock is None:
            return {}
        key = self._role.removeprefix("plan_")
        if key == "light":
            return _compute_light_schedule(
                stage.content, now_seconds=clock.now_seconds, phase_seconds=clock.phase_seconds,
            )
        if self._schedule_kind == "fan":
            return _compute_fan_schedule(
                stage.content, key, now_seconds=clock.now_seconds, phase_seconds=clock.phase_seconds,
            )
        return _compute_recipe_device_schedule(
            stage.content, key, now_seconds=clock.now_seconds, phase_seconds=clock.phase_seconds,
        )

    @property
    def native_value(self) -> str | None:
        selected = self._selected()
        if selected is None:
            return None
        if self._role == "recipe":
            return selected.recipe
        if self._role == "stage":
            if selected.state == "running" and selected.stage is not None:
                return selected.stage.name
            return selected.state.capitalize() if selected.state != "unknown" else None
        if selected.state == "scheduled":
            return "Scheduled"
        if selected.state != "running":
            return selected.state.capitalize() if selected.state != "unknown" else None
        info = self._schedule(selected)
        if not info:
            return None
        if self._schedule_kind == "light":
            label = info.get("remaining_label")
            return label if isinstance(label, str) else None
        if (
            info.get("level") is None and self._schedule_kind in ("fan", "humidifier", "drip")
            and info.get("mode") not in ("auto", "cycle") and info.get("state") not in ("auto", "cycle")
            and info.get("preset_mode") != "natural_wind"
        ):
            return None
        return _recipe_device_label(self._schedule_kind or "", info)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        read = self.coordinator.garden_plan_read(self._scene_id)
        attrs: dict[str, Any] = {"read_status": read.status}
        selected = self._selected()
        if selected is None:
            return attrs
        if self._role == "recipe":
            attrs["plan_count"] = len(read.plans.plans) if read.plans else 0
            return attrs
        attrs["stage_status"] = selected.state
        attrs["configured_only"] = True
        if selected.stage is not None:
            attrs["stage_name"] = selected.stage.name
            if selected.stage.start_time:
                timezone = self._garden_timezone()
                if timezone is not None:
                    with suppress(OverflowError, OSError, ValueError, ZoneInfoNotFoundError):
                        attrs["starts_at"] = datetime.fromtimestamp(
                            selected.stage.start_time, tz=ZoneInfo(timezone)
                        ).isoformat()
        if self._role.startswith("plan_"):
            timezone = self._garden_timezone()
            if timezone is not None:
                attrs["timezone"] = timezone
            if selected.state in ("running", "scheduled"):
                _, status = self._schedule_clock(selected)
                attrs["schedule_status"] = status
                if status == "current":
                    attrs.update(self._schedule(selected))
        return attrs


class VivosunSeedLiveLightSensor(CoordinatorEntity[VivosunCoordinator], SensorEntity):  # type: ignore[misc]
    """Actual S40 light level from the reported actuator state."""

    _attr_has_entity_name = True
    _attr_name = "Current Light Intensity"
    _attr_icon = "mdi:brightness-6"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_seed_current_light_intensity"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def native_value(self) -> int | None:
        level = reported_seed(self.coordinator, self._device_id).get("lgtLv")
        return level if type(level) is int and 0 <= level <= 100 else None


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
        self._attr_name = description.name if isinstance(description.name, str) else None
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
        native_unit = self.entity_description.native_unit_of_measurement
        return native_unit if isinstance(native_unit, str) else None

    @property
    def native_value(self) -> float | None:
        """Return current sensor value from the latest point-log sample."""
        raw_value = self._raw_channel_value()
        if raw_value is None:
            if self.entity_description.quantity == "vpd":
                return self._computed_vpd()
            return None

        if self.entity_description.quantity == "signal_strength":
            return float(raw_value)
        if self.entity_description.quantity == "water_level":
            return raw_value / (self.entity_description.scale_factor or WATER_LEVEL_SCALE_FACTOR)
        value = raw_value / (self.entity_description.scale_factor or TEMP_SCALE_FACTOR)
        if self.entity_description.quantity == "temperature" and self._temp_unit() == _UNIT_FAHRENHEIT:
            return (value * 9 / 5) + 32
        return value

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return extra attributes for non-standard quantities."""
        if self.entity_description.quantity == "vpd":
            source = (
                "reported" if self._raw_channel_value() is not None
                else "computed" if self._computed_vpd() is not None
                else "unavailable"
            )
            return {"quantity": "vpd", "source": source}
        return None

    def _raw_channel_value(self) -> int | None:
        sensors = sensor_slice(self.coordinator, self._device_id)
        for key in self.entity_description.channel_key_lookup:
            value = sensors.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value != SENSOR_UNAVAILABLE_SENTINEL:
                return value
        return None

    def _computed_vpd(self) -> float | None:
        """Compute air VPD in kPa from matching temperature/RH channels (FAO-56)."""
        if self.entity_description.key == "seed_vpd":
            # S40 VPD is separately reported; an absent reading stays unavailable.
            return None
        sensors = sensor_slice(self.coordinator, self._device_id)
        for vpd_key in self.entity_description.channel_key_lookup:
            prefix = vpd_key.removesuffix("Vpd")
            temp = sensors.get(f"{prefix}Temp")
            humidity = sensors.get(f"{prefix}Humi")
            if (
                type(temp) is not int
                or type(humidity) is not int
                or temp == SENSOR_UNAVAILABLE_SENTINEL
                or humidity == SENSOR_UNAVAILABLE_SENTINEL
                or not 0 <= humidity <= 100 * TEMP_SCALE_FACTOR
            ):
                continue
            temp_c = temp / TEMP_SCALE_FACTOR
            if temp_c <= -237.3:
                continue
            saturation = 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))
            return round(saturation * (1 - humidity / (100 * TEMP_SCALE_FACTOR)), 2)
        return None

    def _temp_unit(self) -> str:
        configured_unit = self._entry.options.get(_OPTIONS_TEMP_UNIT, DEFAULT_TEMP_UNIT)
        if configured_unit == _UNIT_FAHRENHEIT:
            return _UNIT_FAHRENHEIT
        return _UNIT_CELSIUS


def _seconds_from_midnight() -> int:
    """Return seconds elapsed since midnight in Home Assistant local time."""
    now = dt_util.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((now - midnight).total_seconds())


def _normalize_slot_time(value: object) -> int | None:
    """The app's slot.time wire unit is seconds, including the 86400 endpoint."""
    return value if type(value) is int and 0 <= value <= 86400 else None


def _format_time(seconds: int) -> str:
    """Format seconds-from-midnight as HH:MM."""
    h, m = divmod(seconds // 60, 60)
    return f"{h:02d}:{m:02d}"


def _schedule_slots(
    slots: list[object], *, phase_seconds: int = 0,
) -> list[tuple[int, dict[str, object]]] | None:
    """Validate complete slot timing before shifting the daily cycle."""
    if type(phase_seconds) is not int or not 0 <= phase_seconds < 86400:
        return None
    normalized: list[tuple[int, dict[str, object]]] = []
    raw_times: set[int] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            return None
        seconds = _normalize_slot_time(slot.get("time", 0))
        if seconds is None or seconds in raw_times:
            return None
        raw_times.add(seconds)
        normalized.append((seconds, slot))

    # Native interval conversion starts with an off slot when 00:00 is omitted.
    if 0 not in raw_times:
        normalized.append((0, {}))
        raw_times.add(0)

    # A 24:00 endpoint closes a day's final interval; when 00:00 is also
    # supplied, that 00:00 slot owns the start of the next daily cycle.
    if 0 in raw_times and 86400 in raw_times:
        normalized = [(seconds, slot) for seconds, slot in normalized if seconds != 86400]
    result = [((seconds + phase_seconds) % 86400, slot) for seconds, slot in normalized]
    result.sort(key=lambda item: item[0])
    return result


def _active_slot(
    slots: list[object], *, now_seconds: int | None = None, phase_seconds: int = 0,
) -> tuple[dict[str, object], int, int | None] | None:
    """Return the currently active timed slot plus its start/next-change time."""
    normalized = _schedule_slots(slots, phase_seconds=phase_seconds)
    if not normalized:
        return None

    now_secs = _seconds_from_midnight() if now_seconds is None else now_seconds
    next_index = next((idx for idx, (time_value, _) in enumerate(normalized) if time_value > now_secs), None)
    current_index = len(normalized) - 1 if next_index is None or next_index == 0 else next_index - 1
    current_time, current_slot = normalized[current_index]
    next_time = normalized[next_index][0] if next_index is not None else normalized[0][0]
    return current_slot, current_time, next_time


def _compute_light_schedule(
    content: dict[str, object], *, now_seconds: int | None = None, phase_seconds: int = 0,
) -> dict[str, Any]:
    """Read the active level and on/off intervals from one daily light schedule."""
    light = content.get("light")
    if not isinstance(light, dict):
        return {}
    slots = light.get("slot")
    if not isinstance(slots, list) or not slots:
        return {}

    normalized = _schedule_slots(slots, phase_seconds=phase_seconds)
    if not normalized:
        return {}
    events: list[tuple[int, int]] = []
    for seconds, slot in normalized:
        level = slot.get("level", 0)
        if type(level) is not int or not 0 <= level <= 100:
            return {}
        events.append((seconds, level))

    now_secs = _seconds_from_midnight() if now_seconds is None else now_seconds
    current_index = next((i - 1 for i, (second, _) in enumerate(events) if second > now_secs),
                         len(events) - 1)
    _, level = events[current_index]
    is_on = level > 0
    spectrum = light.get("spec", 0)
    count = len(events)
    on_seconds = (
        86400 if events[0][1] > 0 else 0
    ) if count == 1 else sum(
        ((events[(i + 1) % count][0] - seconds) % 86400)
        for i, (seconds, slot_level) in enumerate(events) if slot_level > 0
    )

    on_starts = [
        i for i, (_, slot_level) in enumerate(events)
        if slot_level > 0 and events[(i - 1) % count][1] == 0
    ]
    off_starts = [
        i for i, (_, slot_level) in enumerate(events)
        if slot_level == 0 and events[(i - 1) % count][1] > 0
    ]
    if on_starts and off_starts:
        if is_on:
            start_index = next(
                i for step in range(count) if (i := (current_index - step) % count) in on_starts
            )
        else:
            start_index = next(
                i for step in range(1, count + 1)
                if (i := (current_index + step) % count) in on_starts
            )
        end_index = next(
            i for step in range(1, count + 1)
            if (i := (start_index + step) % count) in off_starts
        )
        next_change = events[end_index if is_on else start_index][0]
        remaining_hours: float | None = round(((next_change - now_secs) % 86400) / 3600, 1)
        remaining_label = f"{remaining_hours}h until {'off' if is_on else 'on'}"
        on_time: str | None = _format_time(events[start_index][0])
        off_time: str | None = _format_time(events[end_index][0])
    else:
        remaining_hours = None
        remaining_label = "On" if is_on else "Off"
        on_time = _format_time(events[0][0]) if is_on else None
        off_time = None
    return {
        "state": "on" if is_on else "off",
        "level": level,
        "spectrum": spectrum,
        "on_hours": round(on_seconds / 3600, 1),
        "on_time": on_time,
        "off_time": off_time,
        "remaining_hours": remaining_hours,
        "remaining_label": remaining_label,
    }


def _compute_fan_schedule(
    content: dict[str, object], fan_key: str, *, now_seconds: int | None = None, phase_seconds: int = 0,
) -> dict[str, Any]:
    """Compute fan schedule info from plan stage content."""
    fan = content.get(fan_key)
    if not isinstance(fan, dict):
        return {}
    slots = fan.get("slot")
    if not isinstance(slots, list) or not slots:
        return {}

    active_slot = _active_slot(slots, now_seconds=now_seconds, phase_seconds=phase_seconds)
    if active_slot is None:
        return {}
    slot, slot_time, next_time = active_slot

    mode = slot.get("mode", 0)
    if type(mode) is not int or mode not in (0, 1, _PLAN_AUTO_MODE):
        return {}
    if mode == 1:
        lv_on = _normalize_recipe_fan_selection(
            slot.get("lvOn", slot.get("level", 0)), fan_key=fan_key, allow_preset="lvOn" in slot,
        )
        lv_off = _normalize_fan_percentage(slot.get("lvOff", 0), fan_key=fan_key)
        if lv_on is None or lv_off is None:
            return {}
        on_dur = slot.get("onDur", 0)
        off_dur = slot.get("offDur", 0)
        if type(on_dur) is not int or type(off_dur) is not int or on_dur < 0 or off_dur < 0:
            return {}
        on_min = round(on_dur / 60)
        off_min = round(off_dur / 60)
        result: dict[str, Any] = {
            "mode": "cycle",
            "level_on": lv_on if type(lv_on) is int else None,
            "level_off": lv_off,
            "on_minutes": on_min,
            "off_minutes": off_min,
            "slot_time": _format_time(slot_time),
            "next_time": _format_time(next_time) if next_time is not None else None,
            "cycle": f"{on_min}m on / {off_min}m off",
        }
        if lv_on == "natural_wind":
            result["preset_mode"] = lv_on
        return result
    if mode == _PLAN_AUTO_MODE:
        if any(key in slot and _normalize_plan_int(slot[key]) is None for key in ("lvMin", "lvMax")):
            return {}
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
    level = _normalize_recipe_fan_selection(slot.get("level", 0), fan_key=fan_key, allow_preset=True)
    if level is None:
        return {}
    result = {
        "mode": "manual",
        "level": level if type(level) is int else None,
        "slot_time": _format_time(slot_time),
        "next_time": _format_time(next_time) if next_time is not None else None,
    }
    if level == "natural_wind":
        result["preset_mode"] = level
    return result


def _format_percent_value(value: object) -> str | None:
    if isinstance(value, int):
        return f"{value}%"
    return None


def _normalize_plan_int(value: object) -> int | None:
    if type(value) is not int:
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
    if normalized is None or not 0 <= normalized <= 100:
        return None
    if fan_key == "cfan":
        return cfan_shadow_to_percentage(normalized)
    return dfan_shadow_to_percentage(normalized)


def _normalize_recipe_fan_selection(
    value: object, *, fan_key: str, allow_preset: bool,
) -> int | Literal["natural_wind"] | None:
    """App recipe slots support Natural Wind only for cFan manual level and cycle lvOn."""
    if fan_key == "cfan" and allow_preset and type(value) is int and value == CFAN_NATURAL_WIND_VALUE:
        return "natural_wind"
    return _normalize_fan_percentage(value, fan_key=fan_key)


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


def _compute_recipe_device_schedule(
    content: dict[str, object], recipe_key: str, *, now_seconds: int | None = None,
    phase_seconds: int = 0,
) -> dict[str, Any]:
    recipe = content.get(recipe_key)
    if not isinstance(recipe, dict):
        return {}
    slots = recipe.get("slot")
    if not isinstance(slots, list) or not slots:
        return {}

    active_slot = _active_slot(slots, now_seconds=now_seconds, phase_seconds=phase_seconds)
    if active_slot is None:
        return {}
    slot, slot_time, next_time = active_slot
    mode = slot.get("mode", 0)
    if recipe_key in ("hmdf", "dhmdf", "drip"):
        supported = (0, 1) if recipe_key == "drip" else (0, _PLAN_AUTO_MODE)
        if type(mode) is not int or mode not in supported:
            return {}

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
            if info.get("preset_mode") == "natural_wind":
                return f"Natural Wind · {info.get('cycle', 'Not set')}"
            return cast("str", info.get("cycle", "Not set"))
        if mode == "auto":
            standby_status = info.get("standby_status")
            if isinstance(standby_status, str):
                return f"Auto Standby {standby_status}"
            return "Auto"
        if info.get("preset_mode") == "natural_wind":
            return "Natural Wind"
        level = info.get("level")
        if type(level) is not int:
            return "Not set"
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

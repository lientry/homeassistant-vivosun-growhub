"""Sensor platform for the Vivosun GrowHub integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_TEMP_UNIT, DOMAIN, TEMP_SCALE_FACTOR, WATER_LEVEL_SCALE_FACTOR
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, sensor_slice

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData

_OPTIONS_TEMP_UNIT = "temp_unit"
_UNIT_CELSIUS = "celsius"
_UNIT_FAHRENHEIT = "fahrenheit"


@dataclass(frozen=True, kw_only=True)
class VivosunSensorDescription(SensorEntityDescription):  # type: ignore[misc]
    """Description for a Vivosun channel sensor entity."""

    channel_key: str
    quantity: str
    state_class: SensorStateClass = SensorStateClass.MEASUREMENT


_ALL_SENSOR_DESCRIPTIONS: tuple[VivosunSensorDescription, ...] = (
    VivosunSensorDescription(
        key="inside_temperature",
        name="Inside Temperature",
        channel_key="inTemp",
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="inside_humidity",
        name="Inside Humidity",
        channel_key="inHumi",
        quantity="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="inside_vpd",
        name="Inside VPD",
        channel_key="inVpd",
        quantity="vpd",
        icon="mdi:leaf",
        native_unit_of_measurement="kPa",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_temperature",
        name="Outside Temperature",
        channel_key="outTemp",
        quantity="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_humidity",
        name="Outside Humidity",
        channel_key="outHumi",
        quantity="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VivosunSensorDescription(
        key="outside_vpd",
        name="Outside VPD",
        channel_key="outVpd",
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

    entities: list[VivosunChannelSensorEntity] = []
    for device in coordinator.devices:
        allowed_keys = _DEVICE_TYPE_SENSORS.get(device.device_type, frozenset())
        for description in _ALL_SENSOR_DESCRIPTIONS:
            if description.channel_key in allowed_keys:
                entities.append(
                    VivosunChannelSensorEntity(coordinator, entry, description, device.device_id)
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
        value = sensors.get(self.entity_description.channel_key)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return None

    def _temp_unit(self) -> str:
        configured_unit = self._entry.options.get(_OPTIONS_TEMP_UNIT, DEFAULT_TEMP_UNIT)
        if configured_unit == _UNIT_FAHRENHEIT:
            return _UNIT_FAHRENHEIT
        return _UNIT_CELSIUS

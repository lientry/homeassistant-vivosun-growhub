"""Tests for Vivosun sensor platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.sensor import VivosunChannelSensorEntity, async_setup_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _StubCoordinator:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.device = DeviceInfo(
            device_id="dev-1",
            client_id="vivosun-VSCTLE42A-acc-dev-1",
            topic_prefix="prefix",
            name="GrowHub",
            online=True,
            scene_id=66078,
        )
        self.is_mqtt_connected = True


async def test_sensor_setup_creates_eight_entities(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 8
    assert {entity.unique_id for entity in added} == {
        "vivosun_growhub_dev-1_inTemp",
        "vivosun_growhub_dev-1_inHumi",
        "vivosun_growhub_dev-1_inVpd",
        "vivosun_growhub_dev-1_outTemp",
        "vivosun_growhub_dev-1_outHumi",
        "vivosun_growhub_dev-1_outVpd",
        "vivosun_growhub_dev-1_coreTemp",
        "vivosun_growhub_dev-1_rssi",
    }


async def test_sensor_values_scale_and_map_correctly(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "sensors": {
            "inTemp": 2345,
            "inHumi": 6012,
            "inVpd": 145,
            "outTemp": 1876,
            "outHumi": 5234,
            "outVpd": 98,
            "coreTemp": 3839,
            "rssi": -35,
        },
        "shadow": {"connection": {"connected": True}},
    }

    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    entity_by_unique_id = {entity.unique_id: entity for entity in added}

    assert entity_by_unique_id["vivosun_growhub_dev-1_inTemp"].native_value == 23.45
    assert entity_by_unique_id["vivosun_growhub_dev-1_inTemp"].native_unit_of_measurement == UnitOfTemperature.CELSIUS

    assert entity_by_unique_id["vivosun_growhub_dev-1_inHumi"].native_value == 60.12
    assert entity_by_unique_id["vivosun_growhub_dev-1_inHumi"].native_unit_of_measurement == PERCENTAGE

    inside_vpd = entity_by_unique_id["vivosun_growhub_dev-1_inVpd"]
    assert inside_vpd.native_value == 1.45
    assert inside_vpd.device_class is None
    assert inside_vpd.state_class == SensorStateClass.MEASUREMENT
    assert inside_vpd.native_unit_of_measurement == "kPa"
    assert inside_vpd.extra_state_attributes == {"quantity": "vpd"}

    assert entity_by_unique_id["vivosun_growhub_dev-1_outTemp"].native_value == 18.76
    assert entity_by_unique_id["vivosun_growhub_dev-1_outHumi"].native_value == 52.34
    assert entity_by_unique_id["vivosun_growhub_dev-1_outVpd"].native_value == 0.98
    assert entity_by_unique_id["vivosun_growhub_dev-1_coreTemp"].native_value == 38.39
    assert entity_by_unique_id["vivosun_growhub_dev-1_rssi"].native_value == -35.0


async def test_sensor_normalized_sentinel_none_maps_to_unavailable(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "sensors": {
            "inTemp": None,
            "inHumi": None,
            "inVpd": None,
            "outTemp": None,
            "outHumi": None,
            "outVpd": None,
            "coreTemp": None,
            "rssi": None,
        }
    }

    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert all(entity.native_value is None for entity in added)


async def test_sensor_temperature_options_celsius_and_fahrenheit(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"sensors": {"inTemp": 2500, "outTemp": 1000}}

    celsius_entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={"temp_unit": "celsius"})
    celsius_runtime = RuntimeData(entry_id=celsius_entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[celsius_entry.entry_id] = celsius_runtime

    celsius_entities: list[VivosunChannelSensorEntity] = []

    def _add_celsius(entities: list[VivosunChannelSensorEntity]) -> None:
        celsius_entities.extend(entities)

    await async_setup_entry(hass, celsius_entry, _add_celsius)
    in_temp_c = next(entity for entity in celsius_entities if entity.unique_id.endswith("_inTemp"))
    assert in_temp_c.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert in_temp_c.native_value == 25.0
    assert in_temp_c.suggested_unit_of_measurement is None

    fahrenheit_entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={"temp_unit": "fahrenheit"})
    fahrenheit_runtime = RuntimeData(entry_id=fahrenheit_entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[fahrenheit_entry.entry_id] = fahrenheit_runtime

    fahrenheit_entities: list[VivosunChannelSensorEntity] = []

    def _add_fahrenheit(entities: list[VivosunChannelSensorEntity]) -> None:
        fahrenheit_entities.extend(entities)

    await async_setup_entry(hass, fahrenheit_entry, _add_fahrenheit)
    in_temp_f = next(entity for entity in fahrenheit_entities if entity.unique_id.endswith("_inTemp"))
    assert in_temp_f.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT
    assert in_temp_f.native_value == 77.0

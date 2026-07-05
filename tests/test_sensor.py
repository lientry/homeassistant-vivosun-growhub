"""Tests for Vivosun sensor platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub import sensor as sensor_module
from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.sensor import (
    VivosunChannelSensorEntity,
    VivosunPlanStageSensor,
    async_setup_entry,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch

_DEV_ID = "dev-1"


class _StubCoordinator:
    def __init__(self, *, device_type: str = "controller") -> None:
        self.data: dict[str, object] = {}
        self._device = DeviceInfo(
            device_id=_DEV_ID,
            client_id="vivosun-VSCTLE42A-acc-dev-1",
            topic_prefix="prefix",
            name="GrowHub",
            online=True,
            scene_id=66078,
            device_type=device_type,
        )
        self.is_mqtt_connected = True

    @property
    def device(self) -> DeviceInfo:
        return self._device

    @property
    def devices(self) -> list[DeviceInfo]:
        return [self._device]

    def get_device(self, device_id: str) -> DeviceInfo | None:
        if device_id == self._device.device_id:
            return self._device
        return None


async def test_sensor_setup_creates_seventeen_entities(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 17
    assert {entity.unique_id for entity in added} == {
        f"vivosun_growhub_{_DEV_ID}_inTemp",
        f"vivosun_growhub_{_DEV_ID}_inHumi",
        f"vivosun_growhub_{_DEV_ID}_inVpd",
        f"vivosun_growhub_{_DEV_ID}_outTemp",
        f"vivosun_growhub_{_DEV_ID}_outHumi",
        f"vivosun_growhub_{_DEV_ID}_outVpd",
        f"vivosun_growhub_{_DEV_ID}_coreTemp",
        f"vivosun_growhub_{_DEV_ID}_rssi",
        f"vivosun_growhub_{_DEV_ID}_plan_stage",
        f"vivosun_growhub_{_DEV_ID}_plan_light",
        f"vivosun_growhub_{_DEV_ID}_plan_cfan",
        f"vivosun_growhub_{_DEV_ID}_plan_dfan",
        f"vivosun_growhub_{_DEV_ID}_plan_hmdf",
        f"vivosun_growhub_{_DEV_ID}_plan_dhmdf",
        f"vivosun_growhub_{_DEV_ID}_plan_drip",
        f"vivosun_growhub_{_DEV_ID}_plan_heat",
        f"vivosun_growhub_{_DEV_ID}_plan_aircd",
    }


async def test_sensor_setup_creates_curing_box_telemetry_entities(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator(device_type="curing_box")
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert {entity.unique_id for entity in added} == {
        f"vivosun_growhub_{_DEV_ID}_pTemp",
        f"vivosun_growhub_{_DEV_ID}_pHumi",
        f"vivosun_growhub_{_DEV_ID}_pVpd",
        f"vivosun_growhub_{_DEV_ID}_outTemp",
        f"vivosun_growhub_{_DEV_ID}_outHumi",
        f"vivosun_growhub_{_DEV_ID}_outVpd",
        f"vivosun_growhub_{_DEV_ID}_coreTemp",
        f"vivosun_growhub_{_DEV_ID}_rssi",
    }


async def test_sensor_values_scale_and_map_correctly(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "sensors": {
            _DEV_ID: {
                "inTemp": 2345,
                "inHumi": 6012,
                "inVpd": 145,
                "outTemp": 1876,
                "outHumi": 5234,
                "outVpd": 98,
                "coreTemp": 3839,
                "rssi": -35,
            },
        },
        "shadows": {_DEV_ID: {"connection": {"connected": True}}},
    }

    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    entity_by_unique_id = {entity.unique_id: entity for entity in added}

    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_inTemp"].native_value == 23.45
    assert (
        entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_inTemp"].native_unit_of_measurement
        == UnitOfTemperature.CELSIUS
    )

    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_inHumi"].native_value == 60.12
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_inHumi"].native_unit_of_measurement == PERCENTAGE

    inside_vpd = entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_inVpd"]
    assert inside_vpd.native_value == 1.45
    assert inside_vpd.device_class is None
    assert inside_vpd.state_class == SensorStateClass.MEASUREMENT
    assert inside_vpd.native_unit_of_measurement == "kPa"
    assert inside_vpd.extra_state_attributes == {"quantity": "vpd"}

    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_outTemp"].native_value == 18.76
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_outHumi"].native_value == 52.34
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_outVpd"].native_value == 0.98
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_coreTemp"].native_value == 38.39
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_rssi"].native_value == -35.0


async def test_sensor_aliases_map_e42a_plus_channel_keys(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "sensors": {
            _DEV_ID: {
                "bTemp": 2345,
                "bHumi": 6012,
                "bVpd": 145,
                "pTemp": 1876,
                "pHumi": 5234,
                "pVpd": 98,
            },
        },
        "shadows": {_DEV_ID: {"connection": {"connected": True}}},
    }

    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    entity_by_unique_id = {entity.unique_id: entity for entity in added}

    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_inTemp"].native_value == 18.76
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_inHumi"].native_value == 52.34
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_inVpd"].native_value == 0.98
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_outTemp"].native_value == 23.45
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_outHumi"].native_value == 60.12
    assert entity_by_unique_id[f"vivosun_growhub_{_DEV_ID}_outVpd"].native_value == 1.45


async def test_sensor_normalized_sentinel_none_maps_to_unavailable(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "sensors": {
            _DEV_ID: {
                "inTemp": None,
                "inHumi": None,
                "inVpd": None,
                "outTemp": None,
                "outHumi": None,
                "outVpd": None,
                "coreTemp": None,
                "rssi": None,
            },
        },
    }

    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert all(
        entity.native_value is None
        for entity in added
        if not entity.unique_id.startswith("vivosun_growhub_dev-1_plan_")
    )


async def test_sensor_temperature_options_celsius_and_fahrenheit(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"sensors": {_DEV_ID: {"inTemp": 2500, "outTemp": 1000}}}

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


async def test_plan_stage_sensor_returns_manual_when_inactive() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadows": {
            _DEV_ID: {
                "plan": {
                    "active_stage": None,
                    "stages": {
                        "stage1": {"stage_id": "abc", "start_time": 0},
                    },
                }
            }
        }
    }

    sensor = VivosunPlanStageSensor(
        cast("object", coordinator),
        MockConfigEntry(domain=DOMAIN, title="t", data={}),
        _DEV_ID,
    )
    assert sensor.native_value == "Manual"


def test_compute_light_schedule_handles_wraparound_window(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 21 * 3600)
    content = {
        "light": {
            "spec": 0,
            "slot": [
                {"time": 20 * 3600, "level": 25},
                {"time": 8 * 3600, "level": 0},
            ],
        }
    }

    info = sensor_module._compute_light_schedule(content)
    assert info["state"] == "on"
    assert info["remaining_hours"] == 11.0
    assert info["remaining_label"] == "11.0h until off"


def test_compute_light_schedule_handles_wraparound_off_period(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 17 * 3600)
    content = {
        "light": {
            "spec": 0,
            "slot": [
                {"time": 20 * 3600, "level": 25},
                {"time": 8 * 3600, "level": 0},
            ],
        }
    }

    info = sensor_module._compute_light_schedule(content)
    assert info["state"] == "off"
    assert info["remaining_hours"] == 3.0
    assert info["remaining_label"] == "3.0h until on"


def test_compute_light_schedule_uses_local_time_semantics(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 18 * 3600)
    content = {
        "light": {
            "spec": 0,
            "slot": [
                {"time": 12 * 3600, "level": 25},
                {"time": 18 * 3600, "level": 0},
            ],
        }
    }

    info = sensor_module._compute_light_schedule(content)
    assert info["state"] == "off"
    assert info["remaining_hours"] == 18.0
    assert info["remaining_label"] == "18.0h until on"


def test_compute_light_schedule_minute_based_slots(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: (19 * 3600) + (26 * 60))
    content = {
        "light": {
            "spec": 0,
            "slot": [
                {"time": 0, "level": 29},
                {"time": 20 * 60, "level": 0},
            ],
        }
    }

    info = sensor_module._compute_light_schedule(content)
    assert info["state"] == "on"
    assert info["remaining_hours"] == 0.6
    assert info["remaining_label"] == "0.6h until off"


def test_compute_light_schedule_second_based_slots(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: (19 * 3600) + (26 * 60))
    content = {
        "light": {
            "spec": 0,
            "slot": [
                {"time": 0, "level": 29},
                {"time": 20 * 3600, "level": 0},
            ],
        }
    }

    info = sensor_module._compute_light_schedule(content)
    assert info["state"] == "on"
    assert info["remaining_hours"] == 0.6
    assert info["remaining_label"] == "0.6h until off"


def test_compute_fan_schedule_selects_active_auto_slot(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 7 * 3600)
    content = {
        "dfan": {
            "slot": [
                {"time": 0, "mode": 0, "level": 0},
                {"time": 6 * 3600, "mode": 2, "lvMin": 30, "lvMax": 100, "tMax": 2667},
                {"time": 12 * 3600, "mode": 0, "level": 0},
            ]
        }
    }

    info = sensor_module._compute_fan_schedule(content, "dfan")
    assert info["mode"] == "auto"
    assert info["standby_speed"] == 10
    assert info["standby_status"] == "S1"
    assert info["trigger_speed"] == 100
    assert info["temperature_max"] == 26.67
    assert info["slot_time"] == "06:00"
    assert info["next_time"] == "12:00"
    assert sensor_module._recipe_device_label("fan", info) == "Auto Standby S1"


def test_compute_fan_schedule_wraps_manual_slot_before_first_change(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 2 * 3600)
    content = {
        "dfan": {
            "slot": [
                {"time": 6 * 3600, "mode": 2, "lvMin": 30, "lvMax": 100},
                {"time": 12 * 3600, "mode": 0, "level": 0},
                {"time": 18 * 3600, "mode": 0, "level": 30},
            ]
        }
    }

    info = sensor_module._compute_fan_schedule(content, "dfan")
    assert info["mode"] == "manual"
    assert info["level"] == 10
    assert info["slot_time"] == "18:00"
    assert info["next_time"] == "06:00"


def test_compute_recipe_device_schedule_auto_humidifier(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 7 * 3600)
    content = {
        "hmdf": {
            "slot": [
                {"time": 0, "mode": 2, "lvOn": 100, "tHumi": 5500},
            ]
        }
    }

    info = sensor_module._compute_recipe_device_schedule(content, "hmdf")
    assert info["state"] == "auto"
    assert info["target_humidity"] == 55.0
    assert sensor_module._recipe_device_label("humidifier", info) == "Auto Humidity-based"


def test_compute_recipe_device_schedule_auto_humidifier_vpd(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 7 * 3600)
    content = {
        "hmdf": {
            "slot": [
                {"time": 0, "mode": 2, "lvOn": 80, "tVpd": 100, "vpdSwit": 1},
            ]
        }
    }

    info = sensor_module._compute_recipe_device_schedule(content, "hmdf")
    assert info["state"] == "auto"
    assert info["control_basis"] == "vpd"
    assert info["target_vpd"] == 1.0
    assert sensor_module._recipe_device_label("humidifier", info) == "Auto VPD-based"


def test_compute_recipe_device_schedule_single_slot_wraps_next_time(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 7 * 3600)
    content = {
        "dhmdf": {
            "slot": [
                {"time": 0, "mode": 2, "state": 1, "tHumi": 6000},
            ]
        }
    }

    info = sensor_module._compute_recipe_device_schedule(content, "dhmdf")
    assert info["state"] == "auto"
    assert info["target_humidity"] == 60.0
    assert info["slot_time"] == "00:00"
    assert info["next_time"] == "00:00"


def test_compute_recipe_device_schedule_cycle_drip(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 90 * 60)
    content = {
        "drip": {
            "slot": [
                {"time": 0, "mode": 0, "level": 0},
                {"time": 3600, "mode": 1, "level": 100, "onDur": 1200, "offDur": 2400},
                {"time": 7200, "mode": 0, "level": 0},
            ]
        }
    }

    info = sensor_module._compute_recipe_device_schedule(content, "drip")
    assert info["state"] == "cycle"
    assert info["on_minutes"] == 20
    assert info["off_minutes"] == 40
    assert sensor_module._recipe_device_label("drip", info) == "20m on / 40m off"


def test_compute_recipe_device_schedule_manual_drip_zero_is_off(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 2 * 3600)
    content = {
        "drip": {
            "slot": [
                {"time": 0, "mode": 0, "level": 0},
            ]
        }
    }

    info = sensor_module._compute_recipe_device_schedule(content, "drip")
    assert info["state"] == "manual"
    assert sensor_module._recipe_device_label("drip", info) == "Off"


def test_compute_recipe_device_schedule_air_conditioner(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_module, "_seconds_from_midnight", lambda: 12 * 3600)
    content = {
        "aircd": {
            "slot": [
                {"time": 0, "state": 1, "func": 1, "tTemp": 2700},
                {"time": 64800, "state": 1, "func": 1, "tTemp": 2400},
            ]
        }
    }

    info = sensor_module._compute_recipe_device_schedule(content, "aircd")
    assert info["state"] == "on"
    assert info["function"] == 1
    assert info["target_temperature"] == 27.0
    assert info["next_time"] == "18:00"
    assert sensor_module._recipe_device_label("air_conditioner", info) == "Cooling"


def test_stringify_plan_attribute_replaces_none_with_not_set() -> None:
    info = {
        "temperature_min": None,
        "temperature_max": 25.56,
        "nested": {"vpd_min": None},
    }

    formatted = sensor_module._stringify_plan_attribute(info)
    assert formatted == {
        "temperature_min": "Not set",
        "temperature_max": 25.56,
        "nested": {"vpd_min": "Not set"},
    }


async def test_sensor_setup_creates_probe_sensors_for_humidifier(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator(device_type="humidifier")
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 5
    assert {entity.unique_id for entity in added} == {
        f"vivosun_growhub_{_DEV_ID}_pTemp",
        f"vivosun_growhub_{_DEV_ID}_pHumi",
        f"vivosun_growhub_{_DEV_ID}_pVpd",
        f"vivosun_growhub_{_DEV_ID}_waterLv",
        f"vivosun_growhub_{_DEV_ID}_coreTemp",
    }


async def test_sensor_setup_creates_probe_sensors_for_dehumidifier(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator(device_type="dehumidifier")
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 3
    assert {entity.unique_id for entity in added} == {
        f"vivosun_growhub_{_DEV_ID}_pTemp",
        f"vivosun_growhub_{_DEV_ID}_pHumi",
        f"vivosun_growhub_{_DEV_ID}_pVpd",
    }


async def test_sensor_water_level_scales_raw_value(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator(device_type="humidifier")
    coordinator.data = {"sensors": {_DEV_ID: {"waterLv": 20000}}}

    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    water_level = next(e for e in added if e.unique_id.endswith("_waterLv"))
    assert water_level.native_value == 20.0
    assert water_level.native_unit_of_measurement == PERCENTAGE

"""Independent app-backed schedule literals and Home Assistant state regressions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from homeassistant.helpers.entity_platform import EntityPlatform
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub import sensor as sensor_module
from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.garden_topology import GardenTopology
from custom_components.vivosun_growhub.gardens import (
    Garden,
    GardenInventory,
    GardenMember,
    GardenPlanRead,
    parse_garden_plans,
)
from custom_components.vivosun_growhub.models import DeviceInfo
from custom_components.vivosun_growhub.sensor import VivosunGardenSensor

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


def _recipe(
    content: dict[str, object], start_time: int, *, time_offset: int = 0, scene_id: int = 10,
) -> GardenPlanRead:
    plan_id = scene_id + 100
    wire = {
        "scene": {"sceneId": scene_id},
        "iotUserPlanList": [{
            "sceneId": scene_id, "userPlanId": plan_id, "name": "Autoflower",
            "needSync": False, "iotDeviceList": [],
            "userPlanStageList": [{
                "userPlanStageId": 1000 + scene_id, "userPlanId": plan_id, "stageFlag": "stage1",
                "stageName": "Vegetative", "sort": 1, "startTime": start_time,
                "timeOffset": time_offset, "contentUpdate": 0, "planMode": 1,
                "planStageContent": json.dumps(content),
            }],
        }],
    }
    return GardenPlanRead(parse_garden_plans(wire, scene_id=scene_id), "current")


def _freeze_now(monkeypatch: MonkeyPatch, instant: datetime) -> None:
    """Set the entity clock without changing zoneinfo or Home Assistant's clock."""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(sensor_module, "datetime", FrozenDateTime)


def _sensor(
    hass: HomeAssistant, entry: MockConfigEntry, content: dict[str, object],
    start: int, *, zone: str = "UTC", time_offset: int = 0, role: str = "plan_light",
    schedule_kind: str = "light",
) -> VivosunGardenSensor:
    coordinator = VivosunCoordinator(hass, cast("object", None), email="synthetic@example.invalid",
                                     password="synthetic")
    coordinator._garden_inventory = GardenInventory((
        Garden(10, 60, "Garden", zone, (GardenMember("device-a", "thing-a"),)),
    ))
    device = DeviceInfo("device-a", "thing-a", "topic/a", "Controller", True, 10, "controller")
    coordinator._all_devices = [device]
    coordinator._devices = [device]
    coordinator._garden_reads[10] = _recipe(content, start, time_offset=time_offset)
    return VivosunGardenSensor(coordinator, entry, 10, role, "Schedule", "mdi:calendar",
                               schedule_kind=schedule_kind)


def test_wire_second_boundaries_and_active_light_interval_are_literal() -> None:
    info = sensor_module._compute_light_schedule({
        "light": {"slot": [{"time": 300, "level": 50}, {"time": 900, "level": 0}]}
    }, now_seconds=600)
    assert {key: info[key] for key in ("state", "level", "on_time", "off_time", "remaining_hours")} == {
        "state": "on", "level": 50, "on_time": "00:05", "off_time": "00:15",
        "remaining_hours": 0.1,
    }

    midday = sensor_module._compute_light_schedule({
        "light": {"slot": [
            {"time": 21600, "level": 25}, {"time": 36000, "level": 75},
            {"time": 64800, "level": 0},
        ]}
    }, now_seconds=43200)
    assert {key: midday[key] for key in ("state", "level", "on_time", "off_time",
                                         "on_hours", "remaining_hours")} == {
        "state": "on", "level": 75, "on_time": "06:00", "off_time": "18:00",
        "on_hours": 12.0, "remaining_hours": 6.0,
    }


def test_light_uses_each_interval_and_total_daily_on_time() -> None:
    slots = [
        {"time": 0, "level": 0},
        {"time": 21600, "level": 25},
        {"time": 36000, "level": 75},
        {"time": 43200, "level": 0},
        {"time": 54000, "level": 50},
        {"time": 64800, "level": 0},
    ]
    morning = sensor_module._compute_light_schedule({"light": {"slot": slots}}, now_seconds=39600)
    afternoon = sensor_module._compute_light_schedule({"light": {"slot": slots}}, now_seconds=46800)
    assert (morning["level"], morning["remaining_hours"], morning["on_hours"]) == (75, 1.0, 9.0)
    assert (afternoon["state"], afternoon["level"], afternoon["remaining_hours"]) == ("off", 0, 2.0)


@pytest.mark.parametrize("kind", ["light", "cfan"])
def test_nonmidnight_first_slot_has_native_default_off_interval(kind: str) -> None:
    content = {kind: {"slot": [{"time": 3600, "mode": 0, "level": 70}]}}
    if kind == "light":
        info = sensor_module._compute_light_schedule(content, now_seconds=1800)
        assert (info["state"], info["level"], info["on_hours"]) == ("off", 0, 23.0)
        assert (info["on_time"], info["off_time"], info["remaining_label"]) == (
            "01:00", "00:00", "0.5h until on",
        )
    else:
        info = sensor_module._compute_fan_schedule(content, "cfan", now_seconds=1800)
        assert (info["mode"], info["level"], info["slot_time"], info["next_time"]) == (
            "manual", 0, "00:00", "01:00",
        )


def test_day_endpoint_does_not_create_a_positive_midnight_command() -> None:
    info = sensor_module._compute_light_schedule({
        "light": {"slot": [{"time": 86400, "level": 70}]},
    }, now_seconds=43200)
    assert (info["state"], info["level"], info["on_hours"]) == ("off", 0, 0.0)


async def test_native_ha_stays_off_before_first_stage_relative_slot(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 10, 29, 6, 30, tzinfo=UTC))
    entry = MockConfigEntry(domain=DOMAIN, title="Garden", data={})
    entry.add_to_hass(hass)
    start = int(datetime(2026, 10, 29, 6, tzinfo=UTC).timestamp())
    sensor = _sensor(hass, entry, {"light": {"slot": [{"time": 3600, "level": 50}]}}, start)
    GardenTopology(hass, entry.entry_id).apply(sensor.coordinator.garden_inventory, sensor.coordinator._all_devices)
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    platform.config_entry = entry
    try:
        await platform.async_add_entities([sensor])
        await hass.async_block_till_done()
        state = hass.states.get(sensor.entity_id)
        assert state is not None and state.state == "0.5h until on"
        assert (state.attributes["state"], state.attributes["level"], state.attributes["on_time"]) == (
            "off", 0, "07:00",
        )
    finally:
        await platform.async_reset()


@pytest.mark.parametrize("slots", [
    [{"time": 0, "level": "bad"}],
    [{"time": 0, "level": True}],
    [{"time": 0, "level": -1}],
    [{"time": 0, "level": 50}, {"time": "06:00", "level": 0}],
    [{"time": 0, "level": 50}, {"time": 90000, "level": 0}],
])
def test_invalid_light_payload_is_unknown_without_property_errors(slots: list[dict[str, object]]) -> None:
    assert sensor_module._compute_light_schedule({"light": {"slot": slots}}, now_seconds=300) == {}


def test_known_native_cfan_default_and_unrecognized_mode() -> None:
    omitted = sensor_module._compute_fan_schedule({"cfan": {"slot": [{"time": 0, "mode": 0}]}},
                                                  "cfan", now_seconds=43200)
    assert omitted["mode"] == "manual" and omitted["level"] == 0
    assert sensor_module._recipe_device_label("fan", omitted) == "Off"
    for invalid in (7, True, "auto"):
        assert sensor_module._compute_fan_schedule({
            "cfan": {"slot": [{"time": 0, "mode": invalid, "level": 100}]}
        }, "cfan", now_seconds=43200) == {}


def test_common_second_phase_shifts_fan_without_changing_modes() -> None:
    result = sensor_module._compute_fan_schedule({
        "cfan": {"slot": [
            {"time": 3600, "mode": 0, "level": 30},
            {"time": 7200, "mode": 0, "level": 0},
        ]}
    }, "cfan", now_seconds=27000, phase_seconds=21600)
    assert (result["mode"], result["slot_time"], result["next_time"]) == ("manual", "07:00", "08:00")


@pytest.mark.parametrize(("zone", "start_hour_utc", "now_hour_utc", "expected"), [
    ("UTC", 6, 7, ("07:00", "08:00")),
    ("Europe/Berlin", 5, 6, ("07:00", "08:00")),
])
async def test_native_ha_schedule_uses_stage_local_phase_and_timezone(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, zone: str,
    start_hour_utc: int, now_hour_utc: int, expected: tuple[str, str],
) -> None:
    instant = datetime(2026, 10, 29, now_hour_utc, 30, tzinfo=UTC)
    _freeze_now(monkeypatch, instant)
    entry = MockConfigEntry(domain=DOMAIN, title="Garden", data={})
    entry.add_to_hass(hass)
    content = {"light": {"slot": [{"time": 3600, "level": 50}, {"time": 7200, "level": 0}]}}
    started = int(datetime(2026, 10, 29, start_hour_utc, tzinfo=UTC).timestamp())
    sensor = _sensor(hass, entry, content, started, zone=zone)
    topology = GardenTopology(hass, entry.entry_id)
    topology.apply(sensor.coordinator.garden_inventory, sensor.coordinator._all_devices)
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    platform.config_entry = entry
    try:
        await platform.async_add_entities([sensor])
        await hass.async_block_till_done()
        state = hass.states.get(sensor.entity_id)
        assert state is not None and state.state == "0.5h until off"
        assert (state.attributes["on_time"], state.attributes["off_time"]) == expected
        assert (state.attributes["level"], state.attributes["stage_status"]) == (50, "running")
    finally:
        await platform.async_reset()


@pytest.mark.parametrize(("start", "offset", "zone", "reason"), [
    (int(datetime(2026, 10, 29, 6, tzinfo=UTC).timestamp()), 3600, "UTC",
     "unverified_time_offset"),
    (int(datetime(2026, 3, 29, 0, 30, tzinfo=UTC).timestamp()), 0, "Europe/Berlin",
     "unverified_dst"),
])
async def test_unverified_stage_phase_is_unknown_not_fabricated(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, start: int, offset: int, zone: str, reason: str,
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 10, 29, 7, 30, tzinfo=UTC))
    entry = MockConfigEntry(domain=DOMAIN, title="Garden", data={})
    entry.add_to_hass(hass)
    sensor = _sensor(hass, entry, {"light": {"slot": [{"time": 0, "level": 50}]}},
                     start, zone=zone, time_offset=offset)
    assert sensor.native_value is None
    attributes = sensor.extra_state_attributes
    assert attributes["schedule_status"] == reason
    assert "on_time" not in attributes and "off_time" not in attributes


async def test_unrepresentable_timestamp_is_unknown_for_only_its_garden(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Garden", data={})
    entry.add_to_hass(hass)
    sensor = _sensor(hass, entry, {"light": {"slot": [{"time": 0, "level": 50}]}}, 10**30)
    assert sensor.native_value is None
    assert "starts_at" not in sensor.extra_state_attributes
    assert sensor.extra_state_attributes["stage_status"] == "unknown"
    assert sensor.coordinator.garden_plan_read(10).status == "current"


async def test_native_ha_invalid_garden_schedule_isolated_and_native_zero_default(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    """An invalid garden property must not poison another garden's real HA state."""
    _freeze_now(monkeypatch, datetime(2026, 10, 29, 7, 30, tzinfo=UTC))
    entry = MockConfigEntry(domain=DOMAIN, title="Gardens", data={})
    entry.add_to_hass(hass)
    coordinator = VivosunCoordinator(hass, cast("object", None), email="synthetic@example.invalid",
                                     password="synthetic")
    coordinator._garden_inventory = GardenInventory((
        Garden(10, 60, "Invalid", "UTC", (GardenMember("device-a", None),)),
        Garden(20, 60, "Valid", "UTC", (GardenMember("device-b", None),)),
    ))
    devices = [
        DeviceInfo("device-a", "thing-a", "topic/a", "Controller A", True, 10, "controller"),
        DeviceInfo("device-b", "thing-b", "topic/b", "Controller B", True, 20, "controller"),
    ]
    coordinator._devices = devices
    coordinator._all_devices = devices
    start = int(datetime(2026, 10, 29, 6, tzinfo=UTC).timestamp())
    coordinator._garden_reads = {
        10: _recipe({
            "light": {"slot": [{"time": 3600, "level": "bad"}]},
            "cfan": {"slot": [{"time": 0, "mode": 7, "level": 100}]},
        }, start),
        20: _recipe({
            "light": {"slot": [{"time": 3600, "level": 50}, {"time": 7200, "level": 0}]},
        }, start, scene_id=20),
    }
    GardenTopology(hass, entry.entry_id).apply(coordinator.garden_inventory, devices)
    invalid_light = VivosunGardenSensor(coordinator, entry, 10, "plan_light", "Light Schedule",
                                        "mdi:lightbulb", schedule_kind="light")
    invalid_fan = VivosunGardenSensor(coordinator, entry, 10, "plan_cfan", "Circulator Schedule",
                                      "mdi:fan", schedule_kind="fan")
    valid_light = VivosunGardenSensor(coordinator, entry, 20, "plan_light", "Light Schedule",
                                      "mdi:lightbulb", schedule_kind="light")
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    platform.config_entry = entry
    try:
        await platform.async_add_entities([invalid_light, invalid_fan, valid_light])
        await hass.async_block_till_done()
        assert hass.states.get(invalid_light.entity_id).state == "unknown"
        assert hass.states.get(invalid_fan.entity_id).state == "unknown"
        healthy = hass.states.get(valid_light.entity_id)
        assert healthy.state == "0.5h until off" and healthy.attributes["level"] == 50

        coordinator._garden_reads[10] = _recipe({
            "light": {"slot": [{"time": 3600, "level": 25}, {"time": 7200, "level": 0}]},
            "cfan": {"slot": [{"time": 0, "mode": 0}]},
        }, start)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        native_fan = hass.states.get(invalid_fan.entity_id)
        assert native_fan.state == "Off"
        assert (native_fan.attributes["mode"], native_fan.attributes["level"]) == ("manual", 0)
        assert hass.states.get(invalid_light.entity_id).attributes["level"] == 25

        coordinator._garden_reads[10] = _recipe({
            "light": {"slot": [{"time": 0, "level": 50}]},
            "cfan": {"slot": [{"time": 0, "mode": 0}]},
        }, 10**30)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        unknown = hass.states.get(invalid_light.entity_id)
        assert unknown.state == "unknown" and unknown.attributes["stage_status"] == "unknown"
        assert "starts_at" not in unknown.attributes
        assert hass.states.get(valid_light.entity_id).state == "0.5h until off"
    finally:
        await platform.async_reset()


async def test_native_ha_cfan_natural_wind_cycle_and_manual_from_parsed_rest(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    """An app-supported recipe preset must survive REST parsing into native HA state."""
    _freeze_now(monkeypatch, datetime(2026, 10, 29, 7, 30, tzinfo=UTC))
    entry = MockConfigEntry(domain=DOMAIN, title="Garden", data={})
    entry.add_to_hass(hass)
    start = int(datetime(2026, 10, 29, tzinfo=UTC).timestamp())
    natural_cycle = {"cfan": {"slot": [{
        "time": 0, "mode": 1, "lvOn": 200, "lvOff": 0, "onDur": 1500, "offDur": 300,
    }]}}
    sensor = _sensor(hass, entry, natural_cycle, start, role="plan_cfan", schedule_kind="fan")
    coordinator = sensor.coordinator
    GardenTopology(hass, entry.entry_id).apply(coordinator.garden_inventory, coordinator._all_devices)
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    platform.config_entry = entry
    try:
        await platform.async_add_entities([sensor])
        await hass.async_block_till_done()
        state = hass.states.get(sensor.entity_id)
        assert state is not None and state.state == "Natural Wind · 25m on / 5m off"
        assert {key: state.attributes[key] for key in (
            "mode", "level_on", "preset_mode", "level_off", "on_minutes", "off_minutes",
            "cycle", "slot_time", "next_time", "schedule_status", "configured_only",
        )} == {
            "mode": "cycle", "level_on": None, "preset_mode": "natural_wind",
            "level_off": 0, "on_minutes": 25, "off_minutes": 5,
            "cycle": "25m on / 5m off", "slot_time": "00:00", "next_time": "00:00",
            "schedule_status": "current", "configured_only": True,
        }

        coordinator._garden_reads[10] = _recipe({"cfan": {"slot": [{
            "time": 0, "mode": 1, "lvOn": 70, "lvOff": 0, "onDur": 1500, "offDur": 300,
        }]}}, start)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        numeric = hass.states.get(sensor.entity_id)
        assert numeric is not None and numeric.state == "25m on / 5m off"
        assert numeric.attributes["level_on"] == 50
        assert numeric.attributes["level_off"] == 0
        assert "preset_mode" not in numeric.attributes

        coordinator._garden_reads[10] = _recipe({"cfan": {"slot": [
            {"time": 0, "mode": 0, "level": 200},
        ]}}, start)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        manual = hass.states.get(sensor.entity_id)
        assert manual is not None and manual.state == "Natural Wind"
        assert manual.attributes["mode"] == "manual"
        assert manual.attributes["level"] is None
        assert manual.attributes["preset_mode"] == "natural_wind"

        coordinator._garden_reads[10] = _recipe({"cfan": {"slot": [
            {"time": 0, "mode": 0, "level": 0},
        ]}}, start)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        off = hass.states.get(sensor.entity_id)
        assert off is not None and off.state == "Off"
        assert off.attributes["level"] == 0 and "preset_mode" not in off.attributes

        coordinator._garden_reads[10] = GardenPlanRead(coordinator._garden_reads[10].plans, "stale")
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        assert hass.states.get(sensor.entity_id).state == "unavailable"
    finally:
        await platform.async_reset()


async def test_native_ha_rejects_bad_fan_recipes_without_poisoning_duct_fan(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    _freeze_now(monkeypatch, datetime(2026, 10, 29, 7, 30, tzinfo=UTC))
    entry = MockConfigEntry(domain=DOMAIN, title="Garden", data={})
    entry.add_to_hass(hass)
    start = int(datetime(2026, 10, 29, tzinfo=UTC).timestamp())
    numeric_duct = {"time": 0, "mode": 1, "lvOn": 70, "lvOff": 0, "onDur": 1500, "offDur": 300}
    natural_cfan = {"time": 0, "mode": 1, "lvOn": 200, "lvOff": 0, "onDur": 1500, "offDur": 300}
    sensor = _sensor(hass, entry, {"cfan": {"slot": [natural_cfan]},
                                  "dfan": {"slot": [numeric_duct]}}, start,
                     role="plan_cfan", schedule_kind="fan")
    duct = VivosunGardenSensor(sensor.coordinator, entry, 10, "plan_dfan", "Duct Fan Schedule",
                               "mdi:fan-auto", schedule_kind="fan")
    coordinator = sensor.coordinator
    GardenTopology(hass, entry.entry_id).apply(coordinator.garden_inventory, coordinator._all_devices)
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    platform.config_entry = entry
    try:
        await platform.async_add_entities([sensor, duct])
        await hass.async_block_till_done()
        duct_state = hass.states.get(duct.entity_id)
        assert duct_state is not None and duct_state.state == "25m on / 5m off"
        assert duct_state.attributes["level_on"] == 60
        assert "preset_mode" not in duct_state.attributes

        coordinator._garden_reads[10] = _recipe({
            "cfan": {"slot": [{**natural_cfan, "lvOff": 200}]},
            "dfan": {"slot": [{"time": 0, "mode": 2, "lvMin": 0, "lvMax": 100}]},
        }, start)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        invalid = hass.states.get(sensor.entity_id)
        valid = hass.states.get(duct.entity_id)
        assert invalid is not None and invalid.state == "unknown"
        assert "level_on" not in invalid.attributes and "preset_mode" not in invalid.attributes
        assert valid is not None and valid.state == "Auto Standby Off"
        assert valid.attributes["standby_status"] == "Off"

        coordinator._garden_reads[10] = _recipe({
            "cfan": {"slot": [natural_cfan]},
            "dfan": {"slot": [{**numeric_duct, "lvOn": 200}]},
        }, start)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        assert hass.states.get(sensor.entity_id).state == "Natural Wind · 25m on / 5m off"
        unsupported = hass.states.get(duct.entity_id)
        assert unsupported is not None and unsupported.state == "unknown"
        assert "preset_mode" not in unsupported.attributes and "level_on" not in unsupported.attributes
    finally:
        await platform.async_reset()


@pytest.mark.parametrize(("fan_key", "slot"), [
    ("cfan", {"time": 0, "mode": 0, "level": 201}),
    ("cfan", {"time": 0, "mode": 0, "level": -6666}),
    ("cfan", {"time": 0, "mode": 0, "level": True}),
    ("cfan", {"time": 0, "mode": 1, "lvOn": "200", "lvOff": 0, "onDur": 1500, "offDur": 300}),
    ("cfan", {"time": 0, "mode": 1, "lvOn": 200, "lvOff": 200, "onDur": 1500, "offDur": 300}),
    ("cfan", {"time": 0, "mode": 1, "level": 200, "lvOff": 0, "onDur": 1500, "offDur": 300}),
    ("cfan", {"time": 0, "mode": 1, "lvOn": -6666, "lvOff": 0, "onDur": 1500, "offDur": 300}),
    ("cfan", {"time": 0, "mode": 1, "lvOn": 200, "lvOff": 0, "onDur": -1, "offDur": 300}),
    ("dfan", {"time": 0, "mode": 0, "level": 200}),
    ("dfan", {"time": 0, "mode": 1, "lvOn": 200, "lvOff": 0, "onDur": 1500, "offDur": 300}),
])
def test_fan_recipe_rejects_unsupported_and_malformed_selections(
    fan_key: str, slot: dict[str, object],
) -> None:
    assert sensor_module._compute_fan_schedule(
        {fan_key: {"slot": [slot]}}, fan_key, now_seconds=3600,
    ) == {}


def test_duct_auto_and_numeric_cycle_remain_separate_from_natural_wind() -> None:
    auto = sensor_module._compute_fan_schedule({"dfan": {"slot": [{
        "time": 0, "mode": 2, "lvMin": 30, "lvMax": 100, "tMax": 2667,
    }]}}, "dfan", now_seconds=3600)
    assert (auto["mode"], auto["standby_status"], auto["standby_speed"],
            auto["trigger_speed"], auto["temperature_max"]) == ("auto", "S1", 10, 100, 26.67)
    assert sensor_module._recipe_device_label("fan", auto) == "Auto Standby S1"

    numeric = sensor_module._compute_fan_schedule({"dfan": {"slot": [{
        "time": 0, "mode": 1, "lvOn": 70, "lvOff": 0, "onDur": 1500, "offDur": 300,
    }]}}, "dfan", now_seconds=3600)
    assert (numeric["mode"], numeric["level_on"], numeric["level_off"],
            numeric["on_minutes"], numeric["off_minutes"]) == ("cycle", 60, 0, 25, 5)
    assert "preset_mode" not in auto and "preset_mode" not in numeric

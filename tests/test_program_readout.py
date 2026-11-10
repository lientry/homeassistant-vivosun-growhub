"""Reported firmware programs remain distinct from physical output and writes."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from test_fan import _controller_device, _StubCoordinator

from custom_components.vivosun_growhub.coordinator import _deep_merge_mapping
from custom_components.vivosun_growhub.entity_helpers import cycle_attributes
from custom_components.vivosun_growhub.fan import VivosunCirculationFanEntity, VivosunDuctFanEntity
from custom_components.vivosun_growhub.light import VivosunLightEntity
from custom_components.vivosun_growhub.shadow import parse_reported_fragment
from tests.test_light_pending import _receive, _setup

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def test_genuine_stored_programs_are_readable_without_guessing_phase() -> None:
    reported = {
        "light": {"mode": 1, "inPlan": 0, "lv": 0, "cycle": {
            "lv": 25, "spec": 20, "rate": 0, "tOffset": 0,
            "onDur": 64800, "offDur": 21600,
        }},
        "cFan": {"mode": 1, "lv": 0, "cycle": {
            "lvOn": 44, "lvOff": 0, "tOffset": 0, "onDur": 3000, "offDur": 600,
        }},
        "dFan": {"mode": 1, "lv": 0, "manu": {"lv": 35}, "cycle": {
            "lvOn": 30, "lvOff": 0, "tOffset": 0, "onDur": 3000, "offDur": 600,
        }, "auto": {"tMax": 2800, "hMax": 7800, "lvMin": 30, "lvMax": 100}},
    }
    coordinator = _StubCoordinator()
    coordinator._reported_modes = {("dev-1", "light"): 1, ("dev-1", "cFan"): 1}
    coordinator.data = {"shadows": {"dev-1": parse_reported_fragment(reported)}}
    light = VivosunLightEntity(cast("object", coordinator), "dev-1")
    circulation = VivosunCirculationFanEntity(cast("object", coordinator), "dev-1")
    duct = VivosunDuctFanEntity(cast("object", coordinator), "dev-1")

    assert light.extra_state_attributes == {
        "operation_mode": "cycle", "spectrum": None,
        "cycle_settings": {"on_minutes": 1080, "off_minutes": 360,
                           "on_brightness_percent": 25, "on_spectrum": 20,
                           "sunrise_sunset_enabled": False},
    }
    assert circulation.extra_state_attributes == {
        "operation_mode": "cycle",
        "cycle_settings": {"on_minutes": 50, "off_minutes": 10,
                           "on_speed_percent": 10, "off_speed_percent": 0},
    }
    assert duct.extra_state_attributes == {
        "operation_mode": "cycle",
        "cycle_settings": {"on_minutes": 50, "off_minutes": 10,
                           "on_speed_percent": 10, "off_speed_percent": 0},
        "auto_settings": {"max_temperature_c": 28, "max_humidity_percent": 78,
                          "min_speed_percent": 10, "max_speed_percent": 100},
    }
    assert not light.is_on
    assert not circulation.is_on
    assert not duct.is_on
    assert duct.percentage == 0


@pytest.mark.parametrize(("rate", "enabled"), [
    (0, False), (600, True), (300, None), (-6666, None),
    (None, None), ("600", None), (True, None),
])
def test_sunrise_sunset_readout_uses_only_verified_reported_rates(
    rate: object, enabled: bool | None,
) -> None:
    state = parse_reported_fragment({"light": {"cycle": {"onDur": 3000, "rate": rate}}})["light"]
    settings = cast("dict[str, object]", cycle_attributes("light", state)["cycle_settings"])
    assert settings["on_minutes"] == 50
    if enabled is None:
        assert "sunrise_sunset_enabled" not in settings
    else:
        assert settings["sunrise_sunset_enabled"] is enabled


def test_sunrise_sunset_readout_preserves_sparse_updates_and_null_clearing() -> None:
    state = parse_reported_fragment({"light": {"cycle": {"rate": 600, "onDur": 3000}}})
    _deep_merge_mapping(state, parse_reported_fragment({"light": {"cycle": {"lv": 25}}}))
    assert cycle_attributes("light", state["light"]) == {"cycle_settings": {
        "on_minutes": 50, "on_brightness_percent": 25, "sunrise_sunset_enabled": True,
    }}
    _deep_merge_mapping(state, parse_reported_fragment({"light": {"cycle": {"rate": None}}}))
    assert cycle_attributes("light", state["light"]) == {"cycle_settings": {
        "on_minutes": 50, "on_brightness_percent": 25,
    }}
    _deep_merge_mapping(state, parse_reported_fragment({"light": {"cycle": None}}))
    assert cycle_attributes("light", state["light"]) == {}


@pytest.mark.parametrize("channel", ["light", "cFan", "dFan"])
def test_sparse_cycle_leaves_clear_invalid_values(channel: str) -> None:
    full = parse_reported_fragment({channel: {"cycle": {
        "onDur": 3000, "offDur": 600, "lvOn": 30, "lvOff": 0,
    }}})
    _deep_merge_mapping(full, parse_reported_fragment({channel: {"cycle": {
        "onDur": None, "offDur": -6666, "lvOn": "invalid", "lvOff": True,
    }}}))
    assert cycle_attributes(channel, full[channel]) == {}


@pytest.mark.parametrize("channel", ["light", "cFan", "dFan"])
@pytest.mark.parametrize("invalid", [None, "invalid", 42, []])
def test_explicit_invalid_cycle_block_clears_stored_settings(channel: str, invalid: object) -> None:
    state = parse_reported_fragment({channel: {"cycle": {"onDur": 3000, "offDur": 600}}})
    assert cycle_attributes(channel, state[channel]) == {"cycle_settings": {
        "on_minutes": 50, "off_minutes": 10,
    }}
    _deep_merge_mapping(state, parse_reported_fragment({channel: {"cycle": invalid}}))
    assert cycle_attributes(channel, state[channel]) == {}
    _deep_merge_mapping(state, parse_reported_fragment({channel: {"cycle": {"onDur": 120}}}))
    assert cycle_attributes(channel, state[channel]) == {"cycle_settings": {"on_minutes": 2}}


@pytest.mark.parametrize("channel", ["light", "cFan", "dFan"])
def test_empty_cycle_block_and_absent_cycle_preserve_sparse_settings(channel: str) -> None:
    state = parse_reported_fragment({channel: {"cycle": {"onDur": 3000, "offDur": 600}}})
    _deep_merge_mapping(state, parse_reported_fragment({channel: {"cycle": {}}}))
    _deep_merge_mapping(state, parse_reported_fragment({channel: {"mode": 1}}))
    assert cycle_attributes(channel, state[channel]) == {"cycle_settings": {
        "on_minutes": 50, "off_minutes": 10,
    }}


async def test_readable_modes_require_genuine_reports_not_desired_fallback(hass: HomeAssistant) -> None:
    coordinator, _, _ = _setup(hass)
    coordinator._devices = [replace(coordinator._devices[0], client_id="vivosun-VSCTLE42A-test")]
    coordinator._build_topic_maps()
    light = VivosunLightEntity(coordinator, "light-0")
    circulation = VivosunCirculationFanEntity(coordinator, "light-0")
    try:
        await _receive(coordinator, "vivosun-VSCTLE42A-test", "get/accepted", {
            "state": {"desired": {"light": {"mode": 1, "cycle": {"rate": 600}},
                                  "cFan": {"mode": 1}}},
        })
        assert light.extra_state_attributes["operation_mode"] is None
        assert "cycle_settings" not in light.extra_state_attributes
        assert circulation.extra_state_attributes["operation_mode"] is None
        await _receive(coordinator, "vivosun-VSCTLE42A-test", "get/accepted", {
            "version": 101, "state": {"reported": {"light": {"mode": 1}, "cFan": {"mode": 0}}},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 1000}},
                                      "cFan": {"mode": {"timestamp": 1000}}}},
        })
        assert light.extra_state_attributes["operation_mode"] == "cycle"
        assert circulation.extra_state_attributes["operation_mode"] == "manual"
        await _receive(coordinator, "vivosun-VSCTLE42A-test", "get/accepted", {
            "version": 102, "state": {"desired": {"light": {"mode": 0}, "cFan": {"mode": 1}}},
        })
        assert light.extra_state_attributes["operation_mode"] == "cycle"
        assert circulation.extra_state_attributes["operation_mode"] == "manual"
    finally:
        await coordinator.async_shutdown()


def test_legacy_light_keeps_numeric_mode_without_unverified_label() -> None:
    legacy = replace(_controller_device("dev-1"), client_id="vivosun-VSCTL001-acc-dev-1")
    coordinator = _StubCoordinator([legacy])
    coordinator.data = {"shadows": {"dev-1": parse_reported_fragment({"light": {"mode": 1}})}}
    attributes = VivosunLightEntity(cast("object", coordinator), "dev-1").extra_state_attributes
    assert attributes == {"mode": 1, "spectrum": None}


@pytest.mark.parametrize("field,value", [
    ("lvMin", 101), ("lvMax", -1), ("hMax", 20000), ("hMin", -1),
    ("vpdMax", -10), ("vpdMin", 2001), ("tMax", -6666), ("tMax", 12600),
    ("tMin", -4100), ("lvMin", None), ("hMax", True),
])
def test_invalid_auto_threshold_is_not_displayed(field: str, value: object) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {"dev-1": parse_reported_fragment({
        "dFan": {"auto": {field: value}},
    })}}
    duct = VivosunDuctFanEntity(cast("object", coordinator), "dev-1")
    assert "auto_settings" not in duct.extra_state_attributes


def test_valid_auto_zero_negative_temperature_and_vpd_units() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {"dev-1": parse_reported_fragment({
        "dFan": {"auto": {"lvMin": 0, "hMax": 10000, "tMin": -510,
                           "vpdMin": 0, "vpdMax": 180}},
    })}}
    duct = VivosunDuctFanEntity(cast("object", coordinator), "dev-1")
    assert duct.extra_state_attributes["auto_settings"] == {
        "min_speed_percent": 0, "max_humidity_percent": 100,
        "min_temperature_c": -5.1, "min_vpd_kpa": 0, "max_vpd_kpa": 1.8,
    }


def test_readout_omits_unknown_and_unproved_wire_fields() -> None:
    state = parse_reported_fragment({"dFan": {"cycle": {
        "onDur": 61, "offDur": -1, "tOffset": 0, "lvOn": 200,
    }}})["dFan"]
    assert cycle_attributes("dFan", state) == {}


async def test_legacy_auto_collision_and_mode_only_cycle_selection() -> None:
    legacy = replace(_controller_device("dev-1"), client_id="vivosun-VSCTL001-acc-dev-1")
    coordinator = _StubCoordinator([legacy])
    coordinator.data = {"shadows": {"dev-1": parse_reported_fragment({
        "dFan": {"mode": 1, "lv": 30, "cycle": {"onDur": 3000, "offDur": 600}},
    })}}
    duct = VivosunDuctFanEntity(cast("object", coordinator), "dev-1")
    assert duct.preset_mode == "auto"
    assert duct.preset_modes == ["manual", "auto"]
    with pytest.raises(ValueError):
        await duct.async_set_preset_mode("cycle")
    coordinator.async_publish_shadow_update.assert_not_awaited()

    coordinator = _StubCoordinator()
    duct = VivosunDuctFanEntity(cast("object", coordinator), "dev-1")
    await duct.async_turn_on(preset_mode="cycle")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"dFan": {"mode": 1}}}}, device_id="dev-1",
    )
    coordinator.async_publish_shadow_update.reset_mock()
    await duct.async_set_preset_mode("manual")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"dFan": {"mode": 0}}}}, device_id="dev-1",
    )
    coordinator.async_publish_shadow_update.reset_mock()
    with pytest.raises(ValueError, match="without a manual speed"):
        await duct.async_turn_on(preset_mode="cycle", percentage=50)
    coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_legacy_circulation_cycle_remains_read_only() -> None:
    legacy = replace(_controller_device("dev-1"), client_id="vivosun-VSCTL001-legacy")
    coordinator = _StubCoordinator([legacy])
    fan = VivosunCirculationFanEntity(cast("object", coordinator), "dev-1")
    assert fan.preset_modes == ["natural_wind"]
    with pytest.raises(ValueError):
        await fan.async_set_preset_mode("cycle")
    coordinator.async_publish_shadow_update.assert_not_awaited()

"""A22-only Cycle/Auto settings and commands using independent #49 capture literals."""

from __future__ import annotations

from datetime import UTC, time

import pytest

from custom_components.vivosun_growhub.a22 import (
    a22_offset_to_time,
    a22_temperature_from_raw,
    a22_temperature_to_raw,
    a22_time_to_offset,
    build_a22_auto_setting,
    build_a22_cycle_setting,
    finish_a22_manual_change,
    make_a22_mode_change,
    read_a22_settings,
)
from custom_components.vivosun_growhub.coordinator import _deep_merge_mapping
from custom_components.vivosun_growhub.shadow import parse_shadow_document


@pytest.fixture
def observed() -> dict[str, object]:
    # Event 117's reported USB1 block, minus unrelated device IDs and accessory codes.
    return {
        "mode": 2, "inPlan": 0, "lv": 100, "manu": {"lv": 0},
        "cycle": {"tOffset": 61200, "onDur": 300, "offDur": 300},
        "auto": {
            "tMin": 0, "tMax": 2110, "tHys": 150,
            "hMin": 5000, "hMax": 0, "hHys": 400,
            "vMin": 0, "vMax": 0, "vHys": 10,
            "cMin": 0, "cMax": 0, "cHys": 5000,
        },
    }


def test_reported_settings_preserve_zero_and_unknown(observed: dict[str, object]) -> None:
    settings = read_a22_settings("VSPLUGA22", observed)
    assert (settings.status.mode, settings.status.in_plan, settings.status.is_on) == (2, 0, True)
    assert (settings.cycle.start, settings.cycle.on, settings.cycle.off) == (61200, 300, 300)
    assert (settings.auto.temp_low, settings.auto.temp_high) == (0, 2110)
    assert (settings.auto.humidity_low, settings.auto.humidity_high) == (5000, 0)

    sparse = read_a22_settings("VSPLUGA22", {"auto": {"hMin": 0}, "cycle": {"onDur": True}})
    assert sparse.auto.humidity_low == 0
    assert sparse.auto.humidity_high is None
    assert sparse.cycle.on is None
    assert sparse.status.mode is None
    assert sparse.status.is_on is None


def test_desired_only_is_not_reported_and_sparse_reports_merge(observed: dict[str, object]) -> None:
    state: dict[str, object] = {}
    desired_only = {"state": {"desired": {"usb1": observed}}}
    assert "ports" not in parse_shadow_document(desired_only)
    _deep_merge_mapping(state, dict(parse_shadow_document({"state": {"reported": {"usb1": observed}}})))
    _deep_merge_mapping(state, dict(parse_shadow_document({"state": {"reported": {
        "usb1": {"cycle": {"onDur": 600}, "auto": {"tMin": -500}}
    }}})))
    raw_ports = state["ports"]
    assert isinstance(raw_ports, dict)
    settings = read_a22_settings("VSPLUGA22", raw_ports["usb1"])
    assert (settings.cycle.start, settings.cycle.on, settings.cycle.off) == (61200, 600, 300)
    assert (settings.auto.temp_low, settings.auto.temp_high) == (-500, 2110)
    assert settings.auto.humidity_low == 5000
    assert settings.status.is_on is True
    assert observed["cycle"] == {"tOffset": 61200, "onDur": 300, "offDur": 300}


@pytest.mark.parametrize("port", ["usb1", "usb2", "plug1", "plug2"])
def test_all_four_a22_ports_use_same_native_wire_schema(port: str, observed: dict[str, object]) -> None:
    settings = read_a22_settings("VSPLUGA22", observed)
    assert build_a22_cycle_setting("VSPLUGA22", port, "tOffset", 0, settings, fresh=True) == {
        "state": {"desired": {port: {"cycle": {"tOffset": 0}}}}
    }
    assert build_a22_auto_setting("VSPLUGA22", port, "hMax", 6200, settings, fresh=True) == {
        "state": {"desired": {port: {"auto": {"hMin": 5000, "hMax": 6200}}}}
    }
    assert make_a22_mode_change("VSPLUGA22", port, 1, settings, fresh=True).command == {
        "state": {"desired": {port: {"mode": 1}}}
    }


def test_cycle_pairs_and_auto_pairs_preserve_reported_counterparts(observed: dict[str, object]) -> None:
    settings = read_a22_settings("VSPLUGA22", observed)
    assert build_a22_cycle_setting("VSPLUGA22", "usb1", "onDur", 600, settings, fresh=True) == {
        "state": {"desired": {"usb1": {"cycle": {"onDur": 600, "offDur": 300}}}}
    }
    assert build_a22_cycle_setting("VSPLUGA22", "usb1", "offDur", 120, settings, fresh=True) == {
        "state": {"desired": {"usb1": {"cycle": {"onDur": 300, "offDur": 120}}}}
    }
    assert build_a22_auto_setting("VSPLUGA22", "usb1", "tMin", -500, settings, fresh=True) == {
        "state": {"desired": {"usb1": {"auto": {"tMin": -500, "tMax": 2110}}}}
    }
    assert build_a22_auto_setting("VSPLUGA22", "usb1", "tMax", 2300, settings, fresh=True) == {
        "state": {"desired": {"usb1": {"auto": {"tMin": 0, "tMax": 2300}}}}
    }
    assert build_a22_auto_setting("VSPLUGA22", "usb1", "hMin", 4000, settings, fresh=True) == {
        "state": {"desired": {"usb1": {"auto": {"hMin": 4000, "hMax": 0}}}}
    }
    assert build_a22_auto_setting("VSPLUGA22", "usb1", "hMax", 0, settings, fresh=True) == {
        "state": {"desired": {"usb1": {"auto": {"hMin": 5000, "hMax": 0}}}}
    }


def test_manual_exit_can_finish_before_device_reports_manual(observed: dict[str, object]) -> None:
    """Capture 122/123 sends both writes before firmware reports the new mode."""
    state: dict[str, object] = {}
    _deep_merge_mapping(state, dict(parse_shadow_document({"state": {"reported": {"usb1": observed}}})))
    before = read_a22_settings("VSPLUGA22", state["ports"]["usb1"])
    change = make_a22_mode_change("VSPLUGA22", "usb1", 0, before, fresh=True)
    assert change.command == {"state": {"desired": {"usb1": {"mode": 0}}}}

    _deep_merge_mapping(state, dict(parse_shadow_document({"state": {"desired": {"usb1": {"mode": 0}}}})))
    after_ack = read_a22_settings("VSPLUGA22", state["ports"]["usb1"])
    assert after_ack.status.mode == 2
    assert finish_a22_manual_change("VSPLUGA22", "usb1", change, after_ack, fresh=True) == {
        "state": {"desired": {"usb1": {"manu": {"lv": 100}}}}
    }


def test_manual_exit_rechecks_mode_output_and_port(observed: dict[str, object]) -> None:
    settings = read_a22_settings("VSPLUGA22", observed)
    change = make_a22_mode_change("VSPLUGA22", "usb1", 0, settings, fresh=True)
    assert change.command == {"state": {"desired": {"usb1": {"mode": 0}}}}
    assert change.preserved_level == 100  # real output, not reported manu.lv=0
    assert finish_a22_manual_change("VSPLUGA22", "usb1", change, settings, fresh=True) == {
        "state": {"desired": {"usb1": {"manu": {"lv": 100}}}}
    }
    after_manual = read_a22_settings("VSPLUGA22", {"mode": 0, "inPlan": 0, "lv": 100})
    assert finish_a22_manual_change("VSPLUGA22", "usb1", change, after_manual, fresh=True) == {
        "state": {"desired": {"usb1": {"manu": {"lv": 100}}}}
    }
    with pytest.raises(ValueError, match="changed"):
        finish_a22_manual_change("VSPLUGA22", "usb1", change,
                                 read_a22_settings("VSPLUGA22", {"mode": 0, "inPlan": 0, "lv": 0}),
                                 fresh=True)
    with pytest.raises(ValueError, match="mode changed"):
        finish_a22_manual_change("VSPLUGA22", "usb1", change,
                                 read_a22_settings("VSPLUGA22", {"mode": 1, "inPlan": 0, "lv": 100}),
                                 fresh=True)
    with pytest.raises(ValueError, match="same port"):
        finish_a22_manual_change("VSPLUGA22", "plug1", change, after_manual, fresh=True)


def test_manual_exit_preserves_off_without_older_manual_target() -> None:
    initial = read_a22_settings("VSPLUGA22", {"mode": 1, "inPlan": 0, "lv": 0, "manu": {"lv": 100}})
    change = make_a22_mode_change("VSPLUGA22", "plug1", 0, initial, fresh=True)
    assert change.preserved_level == 0
    confirmed = read_a22_settings("VSPLUGA22", {"mode": 0, "inPlan": 0, "lv": 0})
    assert finish_a22_manual_change("VSPLUGA22", "plug1", change, confirmed, fresh=True) == {
        "state": {"desired": {"plug1": {"manu": {"lv": 0}}}}
    }


def test_unknown_stale_or_planned_facts_never_authorize_commands(observed: dict[str, object]) -> None:
    settings = read_a22_settings("VSPLUGA22", observed)
    with pytest.raises(ValueError, match="fresh"):
        build_a22_auto_setting("VSPLUGA22", "usb1", "hMin", 5000, settings, fresh=False)
    with pytest.raises(ValueError, match="fresh"):
        make_a22_mode_change("VSPLUGA22", "usb1", 0, settings, fresh=False)
    with pytest.raises(ValueError, match="fresh"):
        finish_a22_manual_change("VSPLUGA22", "usb1",
                                 make_a22_mode_change("VSPLUGA22", "usb1", 0, settings, fresh=True),
                                 read_a22_settings("VSPLUGA22", {"mode": 0, "inPlan": 0, "lv": 100}),
                                 fresh=False)
    for raw in ({"mode": 2, "lv": 100}, {"mode": 2, "inPlan": 1, "lv": 100},
                {"inPlan": 0, "lv": 100}, {"mode": True, "inPlan": False, "lv": 100}):
        unknown = read_a22_settings("VSPLUGA22", raw)
        with pytest.raises(ValueError, match=r"mode.*plan"):
            build_a22_cycle_setting("VSPLUGA22", "usb1", "tOffset", 61200, unknown, fresh=True)
        with pytest.raises(ValueError, match=r"mode.*plan"):
            make_a22_mode_change("VSPLUGA22", "usb1", 1, unknown, fresh=True)
    with pytest.raises(ValueError, match="output"):
        make_a22_mode_change("VSPLUGA22", "usb1", 0,
                             read_a22_settings("VSPLUGA22", {"mode": 2, "inPlan": 0}), fresh=True)


def test_missing_paired_values_reject_instead_of_defaulting() -> None:
    sparse = read_a22_settings("VSPLUGA22", {
        "mode": 0, "inPlan": 0, "lv": 100,
        "cycle": {"onDur": 300}, "auto": {"tMax": 2110, "hMax": 0},
    })
    with pytest.raises(ValueError, match="both"):
        build_a22_cycle_setting("VSPLUGA22", "plug2", "onDur", 500, sparse, fresh=True)
    with pytest.raises(ValueError, match="both"):
        build_a22_auto_setting("VSPLUGA22", "plug2", "tMax", 2000, sparse, fresh=True)
    with pytest.raises(ValueError, match="both"):
        build_a22_auto_setting("VSPLUGA22", "plug2", "hMin", 5000, sparse, fresh=True)


def test_bounded_inputs_and_exact_model_reject_unproven_commands(observed: dict[str, object]) -> None:
    settings = read_a22_settings("VSPLUGA22", observed)
    for model, port in (("VSCTLY18", "usb1"), ("VSPLUGA10", "usb1"), ("VSPLUGA22", "dFan")):
        with pytest.raises(ValueError, match="A22"):
            build_a22_cycle_setting(model, port, "tOffset", 61200, settings, fresh=True)
        with pytest.raises(ValueError, match="A22"):
            make_a22_mode_change(model, port, 1, settings, fresh=True)
    with pytest.raises(ValueError, match="A22"):
        read_a22_settings("VSCTLY18", observed)
    for field, value in (("tOffset", 86400), ("tOffset", -1), ("tOffset", True),
                         ("onDur", 0), ("offDur", 86401), ("onDur", -1), ("mode", 1)):
        with pytest.raises(ValueError):
            build_a22_cycle_setting("VSPLUGA22", "usb1", field, value, settings, fresh=True)
    for field, value in (("tHys", 150), ("vMin", 20), ("tMax", None),
                         ("hMax", -1), ("hMin", 10001), ("tMax", True), ("tMax", 12600)):
        with pytest.raises(ValueError):
            build_a22_auto_setting("VSPLUGA22", "usb1", field, value, settings, fresh=True)
    for mode in (-1, 3, True, "1"):
        with pytest.raises(ValueError):
            make_a22_mode_change("VSPLUGA22", "usb1", mode, settings, fresh=True)


def test_a22_clock_is_unzoned_wallclock_seconds_of_day() -> None:
    assert a22_time_to_offset(time(17)) == 61200
    assert a22_time_to_offset(time(0, 0, 0)) == 0
    assert a22_offset_to_time(61200) == time(17)
    assert a22_offset_to_time(86399) == time(23, 59, 59)
    assert a22_time_to_offset(a22_offset_to_time(45296)) == 45296
    for value in (-1, 86400, 86401, True, "61200"):
        with pytest.raises(ValueError):
            a22_offset_to_time(value)
    with pytest.raises(ValueError, match="timezone"):
        a22_time_to_offset(time(17, tzinfo=UTC))
    with pytest.raises(ValueError):
        a22_time_to_offset(time(17, 0, 0, 1))


def test_a22_temperature_scales_celsius_and_fahrenheit() -> None:
    assert a22_temperature_to_raw(21.1, "°C") == 2110
    assert a22_temperature_to_raw(70, "°F") == 2110
    assert a22_temperature_to_raw(-5, "°C") == -500
    assert a22_temperature_from_raw(2110, "°C") == 21.1
    assert a22_temperature_from_raw(2110, "°F") == 70.0
    assert a22_temperature_from_raw(a22_temperature_to_raw(70, "°F"), "°F") == 70.0
    for value in (float("nan"), float("inf"), True, -50, 200):
        with pytest.raises(ValueError):
            a22_temperature_to_raw(value, "°C")
    with pytest.raises(ValueError):
        a22_temperature_to_raw(20, "K")

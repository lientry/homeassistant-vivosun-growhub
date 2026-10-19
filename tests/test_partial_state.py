"""Sparse device reports preserve fields omitted by later updates."""

from __future__ import annotations

import pytest

from custom_components.vivosun_growhub.coordinator import _deep_merge_mapping
from custom_components.vivosun_growhub.shadow import parse_reported_fragment, parse_shadow_document


@pytest.mark.parametrize(
    ("component", "full", "partial", "expected"),
    [
        (
            "light",
            {"mode": 0, "lv": 65, "manu": {"lv": 60, "spec": 50}, "spec": 50, "inPlan": 1},
            {"mode": 2},
            {"mode": 2, "level": 65, "manual_level": 60, "spectrum": 50, "in_plan": True},
        ),
        (
            "cFan",
            {"mode": 0, "lv": 75, "manu": {"lv": 70}, "osc": 1, "nw": 1},
            {"osc": 0},
            {"mode": 0, "level": 75, "manual_level": 70, "oscillating": False},
        ),
        (
            "heat",
            {"on": 1, "lv": 6, "manu": {"lv": 5}, "mode": 1, "state": 1, "targetTemp": 2450},
            {"state": 0},
            {"on": True, "level": 6, "mode": 1, "state": 0, "target_temp": 2450},
        ),
    ],
)
def test_sparse_report_preserves_other_fields(
    component: str, full: dict[str, object], partial: dict[str, object], expected: dict[str, object]
) -> None:
    state = parse_shadow_document({"state": {"reported": {component: full}}})
    delta = parse_shadow_document({"current": {"state": {"reported": {component: partial}}}})
    _deep_merge_mapping(state, delta)
    assert state[component] == expected


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ({"light": {"manu": {"spec": 20}}}, {"light": {"spectrum": 20}}),
        ({"light": {"manu": {"lv": 0}}}, {"light": {"manual_level": 0, "level": 0}}),
        ({"light": {"lv": None, "manu": {"lv": 40}}}, {"light": {"manual_level": 40, "level": 40}}),
        ({"light": {"spec": -6666, "manu": {"spec": 20}}}, {"light": {"spectrum": None}}),
        ({"light": {"inPlan": 0}}, {"light": {"in_plan": False}}),
        ({"cFan": {"manu": {"lv": 0}, "nw": None}}, {"cFan": {"manual_level": 0, "level": 0}}),
        ({"cFan": {"lv": None, "manu": {"lv": 80}}}, {"cFan": {"manual_level": 80, "level": 80}}),
        ({"cFan": {"mode": "invalid", "osc": 0}}, {"cFan": {"mode": None, "oscillating": False}}),
        ({"heat": {"on": 0, "targetTemp": -6666}}, {"heat": {"on": False, "target_temp": None}}),
        ({"heat": {"on": None, "lv": None}}, {"heat": {"on": False, "level": None}}),
        ({"heat": {"manu": {"lv": 0}}}, {"heat": {"level": 0}}),
    ],
)
def test_present_leaf_semantics(fragment: dict[str, object], expected: dict[str, object]) -> None:
    actual = parse_reported_fragment(fragment)
    assert {key: value for key, value in actual.items() if key != "reported_supported"} == expected

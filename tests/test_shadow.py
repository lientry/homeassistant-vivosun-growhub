"""Unit tests for WP-5 shadow/channel parsing and payload builders."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from custom_components.vivosun_growhub.mqtt_client import ReceivedPublish
from custom_components.vivosun_growhub.shadow import (
    ShadowParseError,
    build_cfan_level_payload,
    build_cfan_night_mode_payload,
    build_cfan_oscillate_payload,
    build_dfan_auto_mode_payload,
    build_dfan_auto_threshold_payload,
    build_dfan_level_payload,
    build_light_level_payload,
    build_light_mode_payload,
    build_light_spectrum_payload,
    cfan_percentage_to_shadow,
    cfan_shadow_to_percentage,
    clamp_light_level,
    dfan_percentage_to_shadow,
    dfan_shadow_to_percentage,
    parse_channel_publish,
    parse_channel_sensor_payload,
    parse_reported_fragment,
    parse_shadow_delta_payload,
    parse_shadow_document,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_parse_shadow_document_full_reported_state() -> None:
    payload: dict[str, object] = {
        "state": {
            "reported": {
                "light": {
                    "mode": 0,
                    "inPlan": 1,
                    "lv": 55,
                    "spec": 20,
                    "manu": {"lv": 60, "spec": 22},
                },
                "cFan": {
                    "mode": 0,
                    "lv": 40,
                    "manu": {"lv": 35},
                    "osc": 1,
                    "nw": 0,
                },
                "dFan": {
                    "mode": 1,
                    "lv": 45,
                    "manu": {"lv": 20},
                    "auto": {
                        "lvMin": 30,
                        "lvMax": 90,
                        "tMax": 2800,
                        "tMin": -6666,
                        "hMax": 6500,
                        "hMin": -6666,
                        "vpdMax": -6666,
                        "vpdMin": 10,
                        "tStep": 80,
                        "hStep": 60,
                        "vpdStep": 2,
                        "exChk": 1,
                    },
                },
                "connected": True,
                "plan": {"stage1": {"startT": 0, "contId": "a+b"}},
                "netVer": "1.8.1",
                "unknownRoot": "ignored",
            }
        }
    }

    parsed = parse_shadow_document(payload)

    assert parsed["light"] == {
        "mode": 0,
        "level": 55,
        "manual_level": 60,
        "spectrum": 20,
        "in_plan": True,
    }
    assert parsed["cFan"] == {
        "mode": 0,
        "level": 40,
        "manual_level": 35,
        "oscillating": True,
        "night_mode": False,
    }
    assert parsed["dFan"]["auto_enabled"] is True
    assert parsed["dFan"]["auto"]["tMin"] is None
    assert parsed["dFan"]["auto"]["hMin"] is None
    assert parsed["dFan"]["auto"]["vpdMax"] is None
    assert parsed["dFan"]["auto"]["tMax"] == 2800
    assert parsed["connection"] == {"connected": True}
    assert "plan" in parsed["reported_supported"]
    assert "netVer" in parsed["reported_supported"]
    assert "unknownRoot" not in parsed["reported_supported"]


def test_parse_shadow_document_accepts_update_documents_shape() -> None:
    payload: dict[str, object] = {
        "current": {
            "state": {
                "reported": {
                    "light": {
                        "mode": 0,
                        "manu": {"lv": 33, "spec": 17},
                        "lv": 33,
                        "inPlan": 0,
                    }
                }
            }
        }
    }

    parsed = parse_shadow_document(payload)
    assert parsed["light"]["level"] == 33
    assert parsed["light"]["manual_level"] == 33
    assert parsed["light"]["spectrum"] == 17


def test_parse_shadow_delta_fragment_for_partial_updates() -> None:
    delta_payload: dict[str, object] = {
        "state": {
            "light": {"mode": 0, "manu": {"lv": 15}, "inPlan": 0},
            "connected": 0,
        }
    }

    parsed = parse_shadow_delta_payload(delta_payload)
    assert parsed["light"]["level"] == 15
    assert parsed["connection"]["connected"] is False


def test_parse_reported_fragment_tolerates_missing_and_wrong_types() -> None:
    fragment: dict[str, object] = {
        "light": {"mode": "bad", "manu": {"lv": 30}, "inPlan": "x"},
        "connected": "truthy",
    }

    parsed = parse_reported_fragment(fragment)
    assert parsed["light"]["mode"] is None
    assert parsed["light"]["level"] == 30
    assert parsed["light"]["in_plan"] is False
    assert parsed["connection"]["connected"] is False


def test_parse_channel_sensor_payload_handles_supported_keys_and_sentinel() -> None:
    payload = {
        "inTemp": 2500,
        "inHumi": 4500,
        "inVpd": -6666,
        "outTemp": 1700,
        "outHumi": -6666,
        "outVpd": 22,
        "ignored": 123,
    }

    parsed = parse_channel_sensor_payload(json.dumps(payload).encode("utf-8"))

    assert parsed == {
        "inTemp": 2500,
        "inHumi": 4500,
        "inVpd": None,
        "outTemp": 1700,
        "outHumi": None,
        "outVpd": 22,
    }


def test_parse_channel_sensor_payload_partial_payload_does_not_crash() -> None:
    parsed = parse_channel_sensor_payload(b'{"inTemp": 2200}')
    assert parsed == {"inTemp": 2200}


def test_parse_channel_sensor_payload_rejects_malformed_json() -> None:
    with pytest.raises(ShadowParseError):
        parse_channel_sensor_payload(b"{bad-json")


def test_parse_channel_sensor_payload_rejects_non_object_json() -> None:
    with pytest.raises(ShadowParseError):
        parse_channel_sensor_payload(b"[]")


def test_parse_channel_publish_requires_exact_channel_topic() -> None:
    message = ReceivedPublish(
        topic="prefix/device/channel/app",
        payload=b'{"inTemp": 1100}',
        qos=0,
        retain=False,
        dup=False,
        packet_id=None,
    )
    wrong_topic = ReceivedPublish(
        topic="prefix/device/other",
        payload=b'{"inTemp": 1100}',
        qos=0,
        retain=False,
        dup=False,
        packet_id=None,
    )

    assert parse_channel_publish(message, topic_prefix="prefix/device") == {"inTemp": 1100}
    assert parse_channel_publish(wrong_topic, topic_prefix="prefix/device") is None


@pytest.mark.parametrize(
    ("builder", "field_key"),
    [
        (build_light_level_payload, "light"),
        (build_cfan_level_payload, "cFan"),
        (build_dfan_level_payload, "dFan"),
    ],
)
def test_level_payload_builders_write_to_manu_lv(
    builder: Callable[[int], dict[str, object]],
    field_key: str,
) -> None:
    payload = builder(75)
    desired = payload["state"]["desired"]
    control = desired[field_key]

    assert control == {"mode": 0, "manu": {"lv": 75}}
    assert "lv" not in control


def test_level_payload_builder_validates_bounds() -> None:
    assert build_light_level_payload(-1) == {"state": {"desired": {"light": {"mode": 0, "manu": {"lv": 0}}}}}
    with pytest.raises(ValueError):
        build_cfan_level_payload(101)
    with pytest.raises(ValueError):
        build_cfan_level_payload(201)
    with pytest.raises(ValueError):
        build_dfan_level_payload(101)


def test_non_level_payload_builders_match_required_structure() -> None:
    assert build_light_mode_payload(2) == {"state": {"desired": {"light": {"mode": 2}}}}
    assert build_light_spectrum_payload(42) == {"state": {"desired": {"light": {"manu": {"spec": 42}}}}}
    assert build_cfan_oscillate_payload(True) == {"state": {"desired": {"cFan": {"osc": 1}}}}
    assert build_cfan_night_mode_payload(False) == {"state": {"desired": {"cFan": {"nw": 0}}}}
    assert build_dfan_auto_mode_payload(True) == {"state": {"desired": {"dFan": {"mode": 1}}}}
    assert build_dfan_auto_threshold_payload("tMax", 2800) == {
        "state": {"desired": {"dFan": {"auto": {"tMax": 2800}}}}
    }
    assert build_dfan_auto_threshold_payload("tMin", None) == {
        "state": {"desired": {"dFan": {"auto": {"tMin": -6666}}}}
    }


def test_dfan_auto_threshold_builder_rejects_unknown_field() -> None:
    with pytest.raises(ValueError):
        build_dfan_auto_threshold_payload("invalid", 1)


def test_fan_level_mapping_helpers_match_device_spec() -> None:
    assert cfan_percentage_to_shadow(0) == 0
    assert cfan_percentage_to_shadow(30) == 60
    assert cfan_percentage_to_shadow(70) == 80
    assert cfan_shadow_to_percentage(70) == 50
    assert cfan_shadow_to_percentage(200) is None

    assert dfan_percentage_to_shadow(0) == 0
    assert dfan_percentage_to_shadow(40) == 50
    assert dfan_percentage_to_shadow(100) == 100
    assert dfan_shadow_to_percentage(60) == 50


def test_light_level_clamp_enforces_minimum() -> None:
    assert clamp_light_level(0) == 0
    assert clamp_light_level(1) == 25
    assert clamp_light_level(24) == 25
    assert clamp_light_level(25) == 25
    assert clamp_light_level(80) == 80

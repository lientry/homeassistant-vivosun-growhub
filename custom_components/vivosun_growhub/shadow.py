"""Shadow and channel payload parsing plus desired-state payload builders."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, cast

from .const import (
    CFAN_LEVEL_MAP,
    CFAN_NATURAL_WIND_VALUE,
    DFAN_LEVEL_MAP,
    LIGHT_MIN_BRIGHTNESS,
    MODE_AUTO,
    SENSOR_UNAVAILABLE_SENTINEL,
    SHADOW_KEY_AUTO,
    SHADOW_KEY_CIRCULATOR_FAN,
    SHADOW_KEY_CONNECTED,
    SHADOW_KEY_DUCT_FAN,
    SHADOW_KEY_HEATER,
    SHADOW_KEY_HUMIDIFIER,
    SHADOW_KEY_LEVEL,
    SHADOW_KEY_LIGHT,
    SHADOW_KEY_MANU,
    SHADOW_KEY_MODE,
    SHADOW_KEY_NW,
    SHADOW_KEY_OSC,
    SHADOW_ROOT_REPORTED,
    SHADOW_ROOT_STATE,
    TOPIC_CHANNEL_APP,
)

if TYPE_CHECKING:
    from .mqtt_client import ReceivedPublish

_SUPPORTED_REPORTED_ROOT_KEYS: frozenset[str] = frozenset(
    {
        "light",
        "cFan",
        "dFan",
        "hmdf",
        "heat",
        "plan",
        "cali",
        "btDev",
        "snsExt",
        "connected",
        "netVer",
        "tZone",
        "location",
        "tUnit",
        "ctrlTyp",
        "keyBuz",
        "lcdBl",
        "blTime",
        "portMod",
        "openApi",
    }
)
_DFAN_AUTO_FIELDS: frozenset[str] = frozenset(
    {
        "lvMin",
        "lvMax",
        "tMax",
        "tMin",
        "hMax",
        "hMin",
        "vpdMax",
        "vpdMin",
        "tStep",
        "hStep",
        "vpdStep",
        "exChk",
    }
)


class ShadowParseError(ValueError):
    """Raised when inbound shadow/channel payloads cannot be parsed."""


class LightState(TypedDict):
    """Normalized light state slice from shadow.reported."""

    mode: int | None
    level: int | None
    manual_level: int | None
    spectrum: int | None
    in_plan: bool


class CirculatorFanState(TypedDict):
    """Normalized circulator fan state slice from shadow.reported."""

    mode: int | None
    level: int | None
    manual_level: int | None
    oscillating: bool
    night_mode: bool


class DuctFanAutoState(TypedDict):
    """Normalized dFan auto block from shadow.reported.dFan.auto."""

    lvMin: int | None
    lvMax: int | None
    tMax: int | None
    tMin: int | None
    hMax: int | None
    hMin: int | None
    vpdMax: int | None
    vpdMin: int | None
    tStep: int | None
    hStep: int | None
    vpdStep: int | None
    exChk: int | None


class DuctFanState(TypedDict):
    """Normalized duct fan state slice from shadow.reported."""

    mode: int | None
    level: int | None
    manual_level: int | None
    auto_enabled: bool
    auto: DuctFanAutoState


class HumidifierState(TypedDict, total=False):
    """Normalized humidifier state slice from shadow.reported."""

    on: bool
    level: int | None
    mode: int | None
    water_warning: bool
    target_humidity: int | None


class HeaterState(TypedDict, total=False):
    """Normalized heater state slice from shadow.reported."""

    on: bool
    level: int | None
    mode: int | None
    state: int | None
    target_temp: int | None


class ConnectionState(TypedDict):
    """Normalized root connectivity state from shadow.reported."""

    connected: bool


class ShadowV1State(TypedDict, total=False):
    """HA-oriented shadow slice plus normalized metadata for diagnostics."""

    light: LightState
    cFan: CirculatorFanState
    dFan: DuctFanState
    hmdf: HumidifierState
    heat: HeaterState
    connection: ConnectionState
    reported_supported: dict[str, object]


class ChannelSensorState(TypedDict, total=False):
    """Normalized channel/app sensor values for v1 sensor entities."""

    inTemp: int | None
    inHumi: int | None
    inVpd: int | None
    outTemp: int | None
    outHumi: int | None
    outVpd: int | None
    pTemp: int | None
    pHumi: int | None
    pVpd: int | None
    waterLv: int | None
    coreTemp: int | None
    rssi: int | None


def parse_shadow_document(document: dict[str, object]) -> ShadowV1State:
    """Parse a full shadow payload (`get/accepted` or `update/documents`)."""
    reported = _extract_reported(document)
    return parse_reported_fragment(reported)


def parse_reported_fragment(reported_fragment: dict[str, object]) -> ShadowV1State:
    """Parse partial/full reported-like fragments into normalized v1 state."""
    parsed: ShadowV1State = {
        "reported_supported": {
            key: value for key, value in reported_fragment.items() if key in _SUPPORTED_REPORTED_ROOT_KEYS
        }
    }

    light_raw = _as_dict(reported_fragment.get(SHADOW_KEY_LIGHT))
    if light_raw is not None:
        parsed["light"] = _parse_light_state(light_raw)

    cfan_raw = _as_dict(reported_fragment.get(SHADOW_KEY_CIRCULATOR_FAN))
    if cfan_raw is not None:
        parsed["cFan"] = _parse_cfan_state(cfan_raw)

    dfan_raw = _as_dict(reported_fragment.get(SHADOW_KEY_DUCT_FAN))
    if dfan_raw is not None:
        parsed["dFan"] = _parse_dfan_state(dfan_raw)

    hmdf_raw = _as_dict(reported_fragment.get(SHADOW_KEY_HUMIDIFIER))
    if hmdf_raw is not None:
        parsed["hmdf"] = _parse_hmdf_state(hmdf_raw)

    heat_raw = _as_dict(reported_fragment.get(SHADOW_KEY_HEATER))
    if heat_raw is not None:
        parsed["heat"] = _parse_heat_state(heat_raw)

    if SHADOW_KEY_CONNECTED in reported_fragment:
        parsed["connection"] = ConnectionState(connected=_as_bool(reported_fragment.get(SHADOW_KEY_CONNECTED)))

    return parsed


def parse_shadow_delta_payload(delta_payload: dict[str, object]) -> ShadowV1State:
    """Parse update delta payloads where fields are under the root `state` key."""
    state = _as_dict(delta_payload.get(SHADOW_ROOT_STATE))
    if state is None:
        return {}
    return parse_reported_fragment(state)


def parse_channel_sensor_payload(payload: bytes) -> ChannelSensorState:
    """Parse channel/app payload bytes and return supported sensor keys only."""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as err:
        raise ShadowParseError("Channel payload contains invalid JSON") from err
    if not isinstance(decoded, dict):
        raise ShadowParseError("Channel payload root must be an object")
    return _parse_channel_sensor_object(decoded)


def parse_channel_publish(message: ReceivedPublish, *, topic_prefix: str) -> ChannelSensorState | None:
    """Parse a channel/app publish when topic matches the expected topic prefix."""
    expected_topic = TOPIC_CHANNEL_APP.format(topic_prefix=topic_prefix)
    if message.topic != expected_topic:
        return None
    return parse_channel_sensor_payload(message.payload)


def build_light_level_payload(level: int) -> dict[str, object]:
    """Build desired light level payload for `desired.light.manu.lv`."""
    return _build_level_payload(SHADOW_KEY_LIGHT, clamp_light_level(level))


def build_cfan_level_payload(level: int) -> dict[str, object]:
    """Build desired cFan level payload for `desired.cFan.manu.lv`."""
    if level < 0 or (level > 100 and level != CFAN_NATURAL_WIND_VALUE):
        raise ValueError(f"cFan level must be in range 0..100 or {CFAN_NATURAL_WIND_VALUE}")
    return _build_desired_payload(
        SHADOW_KEY_CIRCULATOR_FAN,
        {SHADOW_KEY_MODE: 0, SHADOW_KEY_MANU: {SHADOW_KEY_LEVEL: level}},
    )


def build_dfan_level_payload(level: int) -> dict[str, object]:
    """Build desired dFan level payload for `desired.dFan.manu.lv`."""
    return _build_level_payload(SHADOW_KEY_DUCT_FAN, level)


def clamp_light_level(level: int) -> int:
    """Clamp light brightness to device-supported range."""
    if level <= 0:
        return 0
    return max(LIGHT_MIN_BRIGHTNESS, min(100, level))


def cfan_percentage_to_shadow(percentage: int) -> int:
    """Convert HA percentage (0-100) to cFan shadow level."""
    level = _percentage_to_app_level(percentage)
    return CFAN_LEVEL_MAP[level]


def dfan_percentage_to_shadow(percentage: int) -> int:
    """Convert HA percentage (0-100) to dFan shadow level."""
    level = _percentage_to_app_level(percentage)
    return DFAN_LEVEL_MAP[level]


def cfan_shadow_to_percentage(shadow_level: int | None) -> int | None:
    """Convert cFan shadow level to HA percentage."""
    if shadow_level is None:
        return None
    if shadow_level == CFAN_NATURAL_WIND_VALUE:
        return None
    return _shadow_level_to_percentage(shadow_level, CFAN_LEVEL_MAP)


def dfan_shadow_to_percentage(shadow_level: int | None) -> int | None:
    """Convert dFan shadow level to HA percentage."""
    if shadow_level is None:
        return None
    return _shadow_level_to_percentage(shadow_level, DFAN_LEVEL_MAP)


def build_light_mode_payload(mode: int) -> dict[str, object]:
    """Build desired light mode payload for `desired.light.mode`."""
    return _build_desired_payload(SHADOW_KEY_LIGHT, {SHADOW_KEY_MODE: mode})


def build_light_spectrum_payload(spectrum: int) -> dict[str, object]:
    """Build desired light spectrum payload for `desired.light.manu.spec`."""
    return _build_desired_payload(SHADOW_KEY_LIGHT, {SHADOW_KEY_MANU: {"spec": spectrum}})


def build_cfan_oscillate_payload(enabled: bool) -> dict[str, object]:
    """Build desired oscillation payload for `desired.cFan.osc`."""
    return _build_desired_payload(SHADOW_KEY_CIRCULATOR_FAN, {SHADOW_KEY_OSC: int(enabled)})


def build_cfan_night_mode_payload(enabled: bool) -> dict[str, object]:
    """Build desired night-mode payload for `desired.cFan.nw`."""
    return _build_desired_payload(SHADOW_KEY_CIRCULATOR_FAN, {SHADOW_KEY_NW: int(enabled)})


def build_dfan_auto_mode_payload(enabled: bool) -> dict[str, object]:
    """Build desired dFan mode payload for `desired.dFan.mode` (0/1)."""
    return _build_desired_payload(SHADOW_KEY_DUCT_FAN, {SHADOW_KEY_MODE: MODE_AUTO if enabled else 0})


def build_dfan_auto_threshold_payload(field: str, value: int | None) -> dict[str, object]:
    """Build desired dFan auto-threshold payload for `desired.dFan.auto.<field>`."""
    if field not in _DFAN_AUTO_FIELDS:
        raise ValueError(f"Unsupported dFan auto field: {field}")
    normalized_value = SENSOR_UNAVAILABLE_SENTINEL if value is None else value
    return _build_desired_payload(SHADOW_KEY_DUCT_FAN, {SHADOW_KEY_AUTO: {field: normalized_value}})


def build_hmdf_on_payload(on: bool) -> dict[str, object]:
    """Build desired humidifier on/off payload."""
    return _build_desired_payload(SHADOW_KEY_HUMIDIFIER, {"on": int(on)})


def build_hmdf_level_payload(level: int) -> dict[str, object]:
    """Build desired humidifier manual level payload."""
    if level < 0 or level > 10:
        raise ValueError("Humidifier level must be in range 0..10")
    return _build_desired_payload(
        SHADOW_KEY_HUMIDIFIER,
        {SHADOW_KEY_MODE: 0, SHADOW_KEY_MANU: {SHADOW_KEY_LEVEL: level}},
    )


def build_hmdf_mode_payload(mode: int) -> dict[str, object]:
    """Build desired humidifier mode payload (0=manual, 1=auto)."""
    return _build_desired_payload(SHADOW_KEY_HUMIDIFIER, {SHADOW_KEY_MODE: mode})


def build_hmdf_target_payload(target_humidity: int) -> dict[str, object]:
    """Build desired humidifier auto target humidity (raw, scaled by 100)."""
    return _build_desired_payload(SHADOW_KEY_HUMIDIFIER, {"targetHumi": target_humidity})


def build_heat_on_payload(on: bool) -> dict[str, object]:
    """Build desired heater on/off payload."""
    return _build_desired_payload(SHADOW_KEY_HEATER, {"on": int(on)})


def build_heat_level_payload(level: int) -> dict[str, object]:
    """Build desired heater manual level payload."""
    if level < 0 or level > 10:
        raise ValueError("Heater level must be in range 0..10")
    return _build_desired_payload(
        SHADOW_KEY_HEATER,
        {SHADOW_KEY_MODE: 0, SHADOW_KEY_MANU: {SHADOW_KEY_LEVEL: level}},
    )


def build_heat_mode_payload(mode: int) -> dict[str, object]:
    """Build desired heater mode payload (0=manual, 1=auto)."""
    return _build_desired_payload(SHADOW_KEY_HEATER, {SHADOW_KEY_MODE: mode})


def build_heat_target_payload(target_temp: int) -> dict[str, object]:
    """Build desired heater auto target temperature (raw, scaled by 100)."""
    return _build_desired_payload(SHADOW_KEY_HEATER, {"targetTemp": target_temp})


def _extract_reported(document: dict[str, object]) -> dict[str, object]:
    state = _as_dict(document.get(SHADOW_ROOT_STATE))
    if state is not None:
        reported = _as_dict(state.get(SHADOW_ROOT_REPORTED))
        if reported is not None:
            return reported

    current = _as_dict(document.get("current"))
    if current is not None:
        current_state = _as_dict(current.get(SHADOW_ROOT_STATE))
        if current_state is not None:
            reported = _as_dict(current_state.get(SHADOW_ROOT_REPORTED))
            if reported is not None:
                return reported

    raise ShadowParseError("Shadow payload does not include state.reported")


def _parse_light_state(light: dict[str, object]) -> LightState:
    mode = _as_int(light.get(SHADOW_KEY_MODE))
    manu = _as_dict(light.get(SHADOW_KEY_MANU))
    manual_level = _as_int(manu.get(SHADOW_KEY_LEVEL)) if manu is not None else None
    level = _as_int(light.get(SHADOW_KEY_LEVEL))
    if level is None:
        level = manual_level

    spectrum = _as_int(light.get("spec"))
    if spectrum is None and manu is not None:
        spectrum = _as_int(manu.get("spec"))

    return LightState(
        mode=mode,
        level=level,
        manual_level=manual_level,
        spectrum=_normalize_sentinel_int(spectrum),
        in_plan=_as_bool(light.get("inPlan")),
    )


def _parse_cfan_state(cfan: dict[str, object]) -> CirculatorFanState:
    manu = _as_dict(cfan.get(SHADOW_KEY_MANU))
    manual_level = _as_int(manu.get(SHADOW_KEY_LEVEL)) if manu is not None else None
    level = _as_int(cfan.get(SHADOW_KEY_LEVEL))
    if level is None:
        level = manual_level

    return CirculatorFanState(
        mode=_as_int(cfan.get(SHADOW_KEY_MODE)),
        level=level,
        manual_level=manual_level,
        oscillating=_as_bool(cfan.get(SHADOW_KEY_OSC)),
        night_mode=_as_bool(cfan.get(SHADOW_KEY_NW)),
    )


def _parse_dfan_state(dfan: dict[str, object]) -> DuctFanState:
    mode = _as_int(dfan.get(SHADOW_KEY_MODE))
    manu = _as_dict(dfan.get(SHADOW_KEY_MANU))
    manual_level = _as_int(manu.get(SHADOW_KEY_LEVEL)) if manu is not None else None
    level = _as_int(dfan.get(SHADOW_KEY_LEVEL))
    if level is None:
        level = manual_level
    auto = _as_dict(dfan.get(SHADOW_KEY_AUTO)) or {}

    auto_state = DuctFanAutoState(
        lvMin=_normalize_sentinel_int(_as_int(auto.get("lvMin"))),
        lvMax=_normalize_sentinel_int(_as_int(auto.get("lvMax"))),
        tMax=_normalize_sentinel_int(_as_int(auto.get("tMax"))),
        tMin=_normalize_sentinel_int(_as_int(auto.get("tMin"))),
        hMax=_normalize_sentinel_int(_as_int(auto.get("hMax"))),
        hMin=_normalize_sentinel_int(_as_int(auto.get("hMin"))),
        vpdMax=_normalize_sentinel_int(_as_int(auto.get("vpdMax"))),
        vpdMin=_normalize_sentinel_int(_as_int(auto.get("vpdMin"))),
        tStep=_normalize_sentinel_int(_as_int(auto.get("tStep"))),
        hStep=_normalize_sentinel_int(_as_int(auto.get("hStep"))),
        vpdStep=_normalize_sentinel_int(_as_int(auto.get("vpdStep"))),
        exChk=_normalize_sentinel_int(_as_int(auto.get("exChk"))),
    )

    return DuctFanState(
        mode=mode,
        level=level,
        manual_level=manual_level,
        auto_enabled=(mode == MODE_AUTO),
        auto=auto_state,
    )


def _parse_hmdf_state(hmdf: dict[str, object]) -> HumidifierState:
    manu = _as_dict(hmdf.get(SHADOW_KEY_MANU))
    manual_level = _as_int(manu.get(SHADOW_KEY_LEVEL)) if manu is not None else None
    level = _as_int(hmdf.get(SHADOW_KEY_LEVEL))
    if level is None:
        level = manual_level

    return HumidifierState(
        on=_as_bool(hmdf.get("on")),
        level=level,
        mode=_as_int(hmdf.get(SHADOW_KEY_MODE)),
        water_warning=_as_bool(hmdf.get("waterWarn")),
        target_humidity=_normalize_sentinel_int(_as_int(hmdf.get("targetHumi"))),
    )


def _parse_heat_state(heat: dict[str, object]) -> HeaterState:
    manu = _as_dict(heat.get(SHADOW_KEY_MANU))
    manual_level = _as_int(manu.get(SHADOW_KEY_LEVEL)) if manu is not None else None
    level = _as_int(heat.get(SHADOW_KEY_LEVEL))
    if level is None:
        level = manual_level

    return HeaterState(
        on=_as_bool(heat.get("on")),
        level=level,
        mode=_as_int(heat.get(SHADOW_KEY_MODE)),
        state=_as_int(heat.get("state")),
        target_temp=_normalize_sentinel_int(_as_int(heat.get("targetTemp"))),
    )


def _parse_channel_sensor_object(payload: dict[str, object]) -> ChannelSensorState:
    sensors: ChannelSensorState = {}
    sensor_values = cast("dict[str, int | None]", sensors)

    for key in (
        "inTemp", "inHumi", "inVpd",
        "outTemp", "outHumi", "outVpd",
        "pTemp", "pHumi", "pVpd",
        "waterLv", "coreTemp", "rssi",
    ):
        if key in payload:
            sensor_values[key] = _normalize_sentinel_int(_as_int(payload.get(key)))

    return sensors


def _build_level_payload(key: str, level: int) -> dict[str, object]:
    if level < 0 or level > 100:
        raise ValueError("Level must be in range 0..100")
    return _build_desired_payload(key, {SHADOW_KEY_MODE: 0, SHADOW_KEY_MANU: {SHADOW_KEY_LEVEL: level}})


def _build_desired_payload(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"state": {"desired": {key: value}}}


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return False


def _normalize_sentinel_int(value: int | None) -> int | None:
    if value == SENSOR_UNAVAILABLE_SENTINEL:
        return None
    return value


def _percentage_to_app_level(percentage: int) -> int:
    if percentage <= 0:
        return 0
    return max(1, min(10, round(percentage / 10)))


def _shadow_level_to_percentage(shadow_level: int, level_map: tuple[int, ...]) -> int:
    if shadow_level <= 0:
        return 0

    best_level = 1
    best_diff = abs(level_map[1] - shadow_level)
    for level in range(2, len(level_map)):
        diff = abs(level_map[level] - shadow_level)
        if diff < best_diff:
            best_diff = diff
            best_level = level
    return best_level * 10

"""VSeed S40 reported state and restricted native command mappings."""

from __future__ import annotations

import math
from collections.abc import Mapping

STAGES = ("Standby", "Germination", "Rooting", "Seedling")
POINT_LOG_KEYS = ("rTemp", "pTemp", "pHumi", "pVpd", "rssi")
SETTING_LIMITS: dict[str, tuple[int, int, int]] = {
    "tTemp": (2000, 3200, 10),
    "tHumi": (100, 9900, 100),
    "lgtLv": (25, 100, 1),
    "lgtOnDur": (0, 86400, 1),
    "lgtEn": (0, 1, 1),
    "pumpEn": (0, 1, 1),
    "pumpMode": (1, 2, 1),
}
_POINT_LOG_RANGES: dict[str, tuple[int, int]] = {
    "rTemp": (-5000, 10000),
    "pTemp": (-5000, 10000),
    "pHumi": (0, 10000),
    "pVpd": (0, 2000),
    "rssi": (-150, 0),
}


def parse_point_log(rows: list[object], *, now: int) -> dict[str, int | None]:
    """Return a complete S40 poll, with missing readings explicitly invalidated."""
    valid_rows: list[tuple[int, Mapping[str, object]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        timestamp = row.get("time")
        try:
            timestamp_number = float(timestamp)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(timestamp_number) or not 0 < timestamp_number <= now + 60:
            continue
        valid_rows.append((int(timestamp_number), row))
    values: dict[str, int | None] = dict.fromkeys(POINT_LOG_KEYS)
    if not valid_rows:
        return values
    latest = max(valid_rows, key=lambda item: item[0])[1]
    for key, (minimum, maximum) in _POINT_LOG_RANGES.items():
        raw = latest.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            continue
        try:
            number = float(raw)
        except (ValueError, OverflowError):
            continue
        if number == -6666 or not math.isfinite(number) or not minimum <= number <= maximum:
            continue
        if number.is_integer():
            values[key] = int(number)
    return values


def stage_name(state: int) -> str | None:
    """Map confirmed application stage codes without inferring active hardware."""
    return STAGES[state] if type(state) is int and 0 <= state < len(STAGES) else None


def reported_seed(coordinator: object, device_id: str) -> Mapping[str, object]:
    """Read only the reported seed subtree, never a desired acknowledgement."""
    from .entity_helpers import shadow_slice

    seed = shadow_slice(coordinator, device_id, "reported_supported").get("seed")  # type: ignore[arg-type]
    return seed if isinstance(seed, Mapping) else {}


def command(path: tuple[str, ...], value: int) -> dict[str, object]:
    """Build a single allowlisted VSSBS40 desired update, without retries."""
    if type(value) is not int:
        raise ValueError("VSeed settings require integer protocol values")
    if path == ("state",):
        if not 0 <= value <= 3:
            raise ValueError("Unsupported VSeed stage")
        seed: dict[str, object] = {"state": value}
    elif path == ("quickAct", "waterTime"):
        if value not in (0, 600):
            raise ValueError("Quick Irrigation only supports 600 seconds or stop")
        seed = {"quickAct": {"waterTime": value}}
    elif len(path) == 2 and path[0] in ("stage1", "stage2", "stage3") and path[1] in SETTING_LIMITS:
        low, high, step = SETTING_LIMITS[path[1]]
        if not low <= value <= high or value % step:
            raise ValueError("VSeed setting out of supported range")
        seed = {path[0]: {path[1]: value}}
    else:
        raise ValueError("Unsupported VSeed command path")
    return {"state": {"desired": {"seed": seed}}}

"""Pure A22 Cycle/Auto settings and guarded, per-port shadow command plans.

Consumers must supply state originating in genuine reported shadow messages and
establish freshness independently. A desired ACK is not execution confirmation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .ports import A22_PORTS, PortReport, reported_port

_MODEL = "VSPLUGA22"
_MODES = (0, 1, 2)  # Manual, Cycle, Auto; Recipe is not part of this API.
_MAX_DURATION_SECONDS = 86_400
_TEMP_MIN_C = Decimal("-40")
_TEMP_MAX_C = Decimal("125")
_TENTH = Decimal("0.1")


@dataclass(frozen=True)
class A22Cycle:
    """Observed native seconds-of-day and Cycle durations; None means unknown."""

    start: int | None
    on: int | None
    off: int | None


@dataclass(frozen=True)
class A22Auto:
    """Observed thresholds in centi-Celsius and hundredths of percent."""

    temp_low: int | None
    temp_high: int | None
    humidity_low: int | None
    humidity_high: int | None


@dataclass(frozen=True)
class A22Settings:
    """Sparse genuine reported port settings without default or inferred values."""

    status: PortReport
    cycle: A22Cycle
    auto: A22Auto


@dataclass(frozen=True)
class A22ModeChange:
    """First mode command and captured current output for optional Manual completion."""

    port: str
    command: dict[str, object]
    preserved_level: int | None
    original_mode: int | None


def read_a22_settings(model: str, raw: object) -> A22Settings:
    """Read a single A22 port from the reported-only shadow port projection.

    Sparse messages must be merged only with other genuine reports before use.
    Missing and malformed fields stay unknown, including missing plan flags.
    """
    if model != _MODEL:
        raise ValueError("A22 settings require exact VSPLUGA22 model")
    entry = raw if isinstance(raw, Mapping) else {}
    cycle = entry.get("cycle")
    cycle = cycle if isinstance(cycle, Mapping) else {}
    auto = entry.get("auto")
    auto = auto if isinstance(auto, Mapping) else {}
    return A22Settings(
        status=reported_port(entry),
        cycle=A22Cycle(_integer(cycle, "tOffset"), _integer(cycle, "onDur"), _integer(cycle, "offDur")),
        auto=A22Auto(_integer(auto, "tMin"), _integer(auto, "tMax"),
                     _integer(auto, "hMin"), _integer(auto, "hMax")),
    )


def make_a22_mode_change(
    model: str, port: str, mode: int, settings: A22Settings, *, fresh: bool,
) -> A22ModeChange:
    """Request Manual=0, Cycle=1 or Auto=2, retaining the true output on Manual exit.

    For an automated-to-Manual change, publish `command` successfully first,
    then recheck genuine guard/output facts before finish_a22_manual_change.
    The app sends both writes before its first reported Manual confirmation.
    Callers own send ordering and failure handling; no retries or optimism.
    """
    _guard(model, port, settings, fresh=fresh)
    if type(mode) is not int or mode not in _MODES:
        raise ValueError("Unsupported A22 mode (only Manual=0, Cycle=1, Auto=2)")
    preserved: int | None = None
    if mode == 0 and settings.status.mode != 0:
        if settings.status.is_on is None:
            raise ValueError("A22 Manual exit needs genuinely reported binary output")
        preserved = 100 if settings.status.is_on else 0
    return A22ModeChange(port, _desired(port, {"mode": mode}), preserved, settings.status.mode)


def finish_a22_manual_change(
    model: str, port: str, change: A22ModeChange, settings: A22Settings, *, fresh: bool,
) -> dict[str, object]:
    """Build the second write after the first publish succeeds and facts are rechecked.

    Reported mode may still be the original automated mode. Waiting for Manual
    before sending this write adds a dependency not present in the app capture.
    Reject another mode or output change rather than reverting newer state.
    """
    _guard(model, port, settings, fresh=fresh)
    if change.port != port or change.preserved_level is None:
        raise ValueError("A22 Manual completion needs a pending transition on the same port")
    if settings.status.mode not in (change.original_mode, 0):
        raise ValueError("A22 reported mode changed during Manual transition")
    if settings.status.is_on is None or (100 if settings.status.is_on else 0) != change.preserved_level:
        raise ValueError("A22 reported output changed during Manual transition")
    return _desired(port, {"manu": {"lv": change.preserved_level}})


def build_a22_cycle_setting(
    model: str, port: str, field: str, value: int, settings: A22Settings, *, fresh: bool,
) -> dict[str, object]:
    """Edit native Cycle seconds without modifying the mode or unrelated blocks."""
    _guard(model, port, settings, fresh=fresh)
    if field == "tOffset":
        _validate_start(value)
        cycle = {"tOffset": value}
    elif field in ("onDur", "offDur"):
        _validate_duration(value)
        on, off = settings.cycle.on, settings.cycle.off
        if on is None or off is None:
            raise ValueError("A22 Cycle duration writes need both genuinely reported durations")
        _validate_duration(on)
        _validate_duration(off)
        cycle = {"onDur": value if field == "onDur" else on,
                 "offDur": value if field == "offDur" else off}
    else:
        raise ValueError("Unsupported A22 Cycle field")
    return _desired(port, {"cycle": cycle})


def build_a22_auto_setting(
    model: str, port: str, field: str, value: int, settings: A22Settings, *, fresh: bool,
) -> dict[str, object]:
    """Write the selected temperature/humidity pair, keeping its reported mate.

    Zero is a literal protocol value; no claim is made that it clears a trigger.
    The app sent pairs, but did not demonstrate every counterpart transition.
    """
    _guard(model, port, settings, fresh=fresh)
    if field in ("tMin", "tMax"):
        low, high = settings.auto.temp_low, settings.auto.temp_high
        keys = ("tMin", "tMax")
    elif field in ("hMin", "hMax"):
        low, high = settings.auto.humidity_low, settings.auto.humidity_high
        keys = ("hMin", "hMax")
    else:
        raise ValueError("Unsupported A22 Auto threshold")
    _validate_threshold(field, value)
    if low is None or high is None:
        raise ValueError("A22 threshold writes need both genuinely reported paired values")
    _validate_threshold(keys[0], low)
    _validate_threshold(keys[1], high)
    return _desired(port, {"auto": {keys[0]: value if field == keys[0] else low,
                                    keys[1]: value if field == keys[1] else high}})


def a22_time_to_offset(value: time) -> int:
    """Pass a naive device-wallclock time as seconds since midnight, with no UTC/DST conversion."""
    if value.tzinfo is not None:
        raise ValueError("A22 Cycle time must have no timezone")
    if value.microsecond:
        raise ValueError("A22 Cycle time must have whole-second precision")
    return value.hour * 3600 + value.minute * 60 + value.second


def a22_offset_to_time(seconds: int) -> time:
    """Convert device-wallclock seconds-of-day directly into a naive time."""
    _validate_start(seconds)
    return time(seconds // 3600, seconds // 60 % 60, seconds % 60)


def a22_temperature_to_raw(value: int | float | Decimal, unit: str) -> int:
    """Convert UI temperature into captured 0.1°C steps stored as centi-Celsius.

    The -40..125°C input envelope is a conservative sensor-based UI safeguard,
    not a verified device firmware threshold-setter range.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Invalid A22 temperature")
    try:
        degrees = Decimal(str(value))
        if not degrees.is_finite():
            raise ValueError("Invalid A22 temperature")
        celsius = degrees if unit == "°C" else (degrees - 32) * 5 / 9 if unit == "°F" else None
        if celsius is None or not _TEMP_MIN_C <= celsius <= _TEMP_MAX_C:
            raise ValueError("A22 temperature outside conservative supported display range")
        return int(celsius.quantize(_TENTH, rounding=ROUND_HALF_UP) * 100)
    except InvalidOperation as exc:
        raise ValueError("Invalid A22 temperature") from exc


def a22_temperature_from_raw(raw: int, unit: str) -> float:
    """Display genuine raw centi-Celsius thresholds in HA Celsius or Fahrenheit."""
    _validate_threshold("tMax", raw)
    celsius = Decimal(raw) / 100
    degrees = celsius if unit == "°C" else celsius * 9 / 5 + 32 if unit == "°F" else None
    if degrees is None:
        raise ValueError("Unsupported A22 temperature unit")
    return float(degrees.quantize(_TENTH, rounding=ROUND_HALF_UP))


def _integer(mapping: Mapping[str, object], field: str) -> int | None:
    value = mapping.get(field)
    return value if type(value) is int else None


def _guard(model: str, port: str, settings: A22Settings, *, fresh: bool) -> None:
    if model != _MODEL or port not in A22_PORTS:
        raise ValueError("A22 settings only apply to VSPLUGA22 physical ports")
    if fresh is not True:
        raise ValueError("A22 write requires fresh genuine reported facts")
    if settings.status.mode not in _MODES or settings.status.in_plan != 0:
        raise ValueError("A22 write requires known reported mode and no active plan")


def _validate_start(value: int) -> None:
    if type(value) is not int or not 0 <= value < 86_400:
        raise ValueError("A22 Cycle start must be seconds-of-day (0..86399)")


def _validate_duration(value: int) -> None:
    # Conservative local command limit, not a manufacturer's stated API bound.
    if type(value) is not int or not 1 <= value <= _MAX_DURATION_SECONDS:
        raise ValueError("A22 Cycle duration must be 1..86400 seconds")


def _validate_threshold(field: str, raw: int) -> None:
    if type(raw) is not int:
        raise ValueError("A22 threshold requires an integer")
    if field in ("tMin", "tMax"):
        if not int(_TEMP_MIN_C * 100) <= raw <= int(_TEMP_MAX_C * 100):
            raise ValueError("A22 temperature outside conservative sensor envelope")
    elif field in ("hMin", "hMax"):
        if not 0 <= raw <= 10_000:
            raise ValueError("A22 humidity must be 0..100 percent in hundredths")
    else:
        raise ValueError("Unsupported A22 threshold")


def _desired(port: str, changes: dict[str, object]) -> dict[str, object]:
    return {"state": {"desired": {port: changes}}}

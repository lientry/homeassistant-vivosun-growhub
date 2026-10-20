"""Physical A22 port reports and confirmed manual power commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

A22_PORTS = ("usb1", "usb2", "plug1", "plug2")


@dataclass(frozen=True)
class PortReport:
    """Actual output and observed metadata, independent of manual write eligibility."""

    is_on: bool | None
    mode: int | None
    in_plan: int | None
    accessory_code: int | None
    can_write: bool


def reported_port(raw: object) -> PortReport:
    """Read actual reported output, even during automatic modes or active plans."""
    if not isinstance(raw, Mapping):
        return PortReport(None, None, None, None, False)
    mode = raw.get("mode")
    mode = mode if type(mode) is int else None
    in_plan = raw.get("inPlan")
    in_plan = in_plan if type(in_plan) is int else None
    dev = raw.get("dev")
    dev = dev if type(dev) is int else None
    lv = raw.get("lv")
    is_on = bool(lv) if type(lv) is int and lv in (0, 100) else None
    can_write = mode == 0 and in_plan == 0
    return PortReport(is_on, mode, in_plan, dev, can_write)


def build_manual_port_payload(port: str, on: bool) -> dict[str, object]:
    """Send only the observed manual 0/100 target, without changing mode or type."""
    if port not in A22_PORTS:
        raise ValueError(f"Unknown A22 physical port: {port}")
    return {"state": {"desired": {port: {"manu": {"lv": 100 if on else 0}}}}}

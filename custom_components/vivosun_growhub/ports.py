"""Shared physical-port reports and confirmed manual power commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from .devices import MODEL_A10, MODEL_A22

A10_PORTS = ("plug1",)
A22_PORTS = ("usb1", "usb2", "plug1", "plug2")
VGROW_PORTS = ("usb1", "usb2")
PORTS_BY_MODEL = {MODEL_A10: A10_PORTS, MODEL_A22: A22_PORTS, "VSCTLY18": VGROW_PORTS}

PORT_DEVICE_NAMES: dict[str, str] = {
    "sgsaDev": "USB A",
    "sgsbDev": "USB B",
    "sgscDev": "USB C",
    "sgsdDev": "USB D",
    "sgseDev": "USB E",
    "sgslDev": "USB Light",
    "ctrlTyp": "USB Port",
    "usb1" : "USB 1",
    "usb2" : "USB 2",
    "plug1" : "Plug 1",
    "plug2" : "Plug 2",
}

PORT_OPTION = 1
SHADOW_KEY_PORT_MOD = 'portMod'
PORT_OPTION_TYPES: dict[int, str] = {
    0: "AeroZesh",
    1: "AeroWave",
}

PORT_DEVICE_TYPES: dict[int, str] = {
    0: "Not Connected",
    PORT_OPTION: "AeroZesh / AeroWave",
    2: "AeroLight",
    4: "4",
    5: "AeroZesh",
    11: "AeroWave",
    12: "12",
}

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
        raise ValueError(f"Unknown physical port: {port}")
    return {"state": {"desired": {port: {"manu": {"lv": 100 if on else 0}}}}}

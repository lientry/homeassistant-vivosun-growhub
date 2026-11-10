"""Exact-model terminal eligibility for the E25's selected physical port."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from homeassistant.exceptions import ServiceValidationError

from .models import client_model_token

if TYPE_CHECKING:
    from .coordinator import VivosunCoordinator
    from .models import DeviceInfo

Control = Literal["light", "cFan", "dFan"]

# VSCTL002 Port1 metadata: one selected accessory may expose two terminals.
E25_TERMINALS: dict[int, frozenset[Control]] = {
    2: frozenset({"light", "cFan"}),
    4: frozenset({"cFan"}),
    5: frozenset({"dFan"}),
}


def selected_controls(device: DeviceInfo, selection: int | None) -> frozenset[Control]:
    """Keep other controller models' existing controls; E25 needs reported proof."""
    if device.device_type != "controller":
        return frozenset()
    if client_model_token(device.client_id) == "VSCTL002":
        return E25_TERMINALS.get(selection, frozenset()) if type(selection) is int else frozenset()
    return frozenset({"light", "cFan", "dFan"})


def control_is_selected(coordinator: VivosunCoordinator, device_id: str, control: Control) -> bool:
    """Retain other controllers' behavior while gating exact E25 terminals."""
    device = coordinator.get_device(device_id)
    if device is None or device.device_type != "controller":
        return False
    if client_model_token(device.client_id) != "VSCTL002":
        return True
    return control in coordinator.selected_controls(device_id)


def supports_local_program_mode(coordinator: VivosunCoordinator, device_id: str, control: Control) -> bool:
    """Allow only independently proven E42A light/circulation mode writes."""
    device = coordinator.get_device(device_id)
    return bool(control in ("light", "cFan") and device
                and client_model_token(device.client_id) == "VSCTLE42A"
                and control_is_selected(coordinator, device_id, control))


def require_selected_control(coordinator: VivosunCoordinator, device_id: str, control: Control) -> None:
    """Block direct entity service calls for deselected or unconfigured E25 terminals."""
    if not control_is_selected(coordinator, device_id, control):
        raise ServiceValidationError(f"{control} is not selected on this controller")

"""Shared entity helper utilities for Vivosun GrowHub platforms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import VivosunCoordinator


def build_device_info(coordinator: VivosunCoordinator) -> DeviceInfo:
    """Build a shared device registry descriptor for GrowHub entities."""
    device = coordinator.device
    return DeviceInfo(
        identifiers={(DOMAIN, device.device_id)},
        name=device.name,
        manufacturer="VIVOSUN",
        model=_model_from_client_id(device.client_id),
    )


def is_entity_available(coordinator: VivosunCoordinator) -> bool:
    """Return availability based on MQTT state and shadow connectivity."""
    if not coordinator.is_mqtt_connected:
        return False

    data = coordinator.data
    if not isinstance(data, Mapping):
        return True

    shadow = data.get("shadow")
    if not isinstance(shadow, Mapping):
        return True

    connection = shadow.get("connection")
    if not isinstance(connection, Mapping):
        return True

    connected = connection.get("connected")
    if connected is None:
        return True
    return bool(connected)


def _model_from_client_id(client_id: str) -> str:
    parts = client_id.split("-")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return client_id

"""Shared entity helper utilities for Vivosun GrowHub platforms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import VivosunCoordinator


def build_device_info(coordinator: VivosunCoordinator, device_id: str) -> DeviceInfo:
    """Build a device registry descriptor for a specific Vivosun device."""
    device = coordinator.get_device(device_id)
    if device is None:
        device = coordinator.device
    return DeviceInfo(
        identifiers={(DOMAIN, device.device_id)},
        name=device.name,
        manufacturer="VIVOSUN",
        model=_model_from_client_id(device.client_id),
    )


def is_entity_available(coordinator: VivosunCoordinator, device_id: str) -> bool:
    """Return availability based on MQTT state and per-device shadow connectivity."""
    if not coordinator.is_mqtt_connected:
        return False

    data = coordinator.data
    if not isinstance(data, Mapping):
        return True

    shadows = data.get("shadows")
    if not isinstance(shadows, Mapping):
        return True

    device_shadow = shadows.get(device_id)
    if not isinstance(device_shadow, Mapping):
        return True

    connection = device_shadow.get("connection")
    if not isinstance(connection, Mapping):
        return True

    connected = connection.get("connected")
    if connected is None:
        return True
    return bool(connected)


def shadow_slice(coordinator: VivosunCoordinator, device_id: str, key: str) -> Mapping[str, object]:
    """Extract a shadow sub-key for a specific device."""
    data = coordinator.data
    if not isinstance(data, Mapping):
        return {}

    shadows = data.get("shadows")
    if not isinstance(shadows, Mapping):
        return {}

    device_shadow = shadows.get(device_id)
    if not isinstance(device_shadow, Mapping):
        return {}

    value = device_shadow.get(key)
    if not isinstance(value, Mapping):
        return {}

    return cast("Mapping[str, object]", value)


def sensor_slice(coordinator: VivosunCoordinator, device_id: str) -> Mapping[str, object]:
    """Extract sensor state for a specific device."""
    data = coordinator.data
    if not isinstance(data, Mapping):
        return {}

    sensors = data.get("sensors")
    if not isinstance(sensors, Mapping):
        return {}

    device_sensors = sensors.get(device_id)
    if not isinstance(device_sensors, Mapping):
        return {}

    return cast("Mapping[str, object]", device_sensors)


def _model_from_client_id(client_id: str) -> str:
    parts = client_id.split("-")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return client_id

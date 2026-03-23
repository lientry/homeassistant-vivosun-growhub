"""Diagnostics support for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.diagnostics import async_redact_data

from .const import DOMAIN
from .redaction import redact_identifier, sanitize_mapping_for_debug

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .models import RuntimeData

_DIAGNOSTICS_REDACT = {
    "password",
    "access_token",
    "login_token",
    "refresh_token",
    "aws_open_id_token",
    "aws_openid_token",
    "authorization",
    "auth_header",
    "headers",
    "credentials",
    "secret_access_key",
    "session_token",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry with sensitive details redacted."""
    entry_summary: dict[str, Any] = {
        "entry_id": config_entry.entry_id,
        "title": _redact_entry_identifier(config_entry.title),
        "unique_id": _redact_entry_identifier(config_entry.unique_id),
        "data": dict(config_entry.data),
        "options": dict(config_entry.options),
    }
    redacted_entry = sanitize_mapping_for_debug(async_redact_data(entry_summary, _DIAGNOSTICS_REDACT))

    domain_data = cast("Mapping[str, RuntimeData]", hass.data.get(DOMAIN, {}))
    runtime = domain_data.get(config_entry.entry_id)
    if runtime is None or runtime.coordinator is None:
        return {
            "config_entry": redacted_entry,
            "runtime_available": False,
            "device": None,
            "coordinator": None,
        }

    coordinator = runtime.coordinator
    try:
        device = coordinator.device
    except RuntimeError:
        device = None
    snapshot = coordinator.data if isinstance(coordinator.data, dict) else {}
    shadow = None
    sensors = snapshot.get("sensors")
    if device is not None:
        shadows = snapshot.get("shadows")
        if isinstance(shadows, dict):
            candidate_shadow = shadows.get(device.device_id)
            if isinstance(candidate_shadow, dict):
                shadow = candidate_shadow
        sensor_map = snapshot.get("sensors")
        if isinstance(sensor_map, dict):
            candidate_sensors = sensor_map.get(device.device_id)
            if isinstance(candidate_sensors, dict):
                sensors = candidate_sensors

    shadow_keys = sorted(shadow.keys()) if isinstance(shadow, dict) else []
    sensor_keys = sorted(sensors.keys()) if isinstance(sensors, dict) else []
    mqtt_connected = snapshot.get("mqtt_connected")
    if not isinstance(mqtt_connected, bool):
        mqtt_connected = coordinator.is_mqtt_connected

    last_update = coordinator.last_update_success_time
    last_update_iso = _as_iso(last_update)

    diagnostics_payload: dict[str, Any] = {
        "config_entry": redacted_entry,
        "runtime_available": True,
        "device": None
        if device is None
        else {
            "name": device.name,
            "online": device.online,
            "device_id": device.device_id,
            "client_id": device.client_id,
            "topic_prefix": device.topic_prefix,
        },
        "coordinator": {
            "mqtt_connected": mqtt_connected,
            "shadow_keys": shadow_keys,
            "sensor_keys": sensor_keys,
            "last_update_success_time": last_update_iso,
        },
    }
    redacted_diagnostics = async_redact_data(diagnostics_payload, _DIAGNOSTICS_REDACT)
    return sanitize_mapping_for_debug(redacted_diagnostics)


def _as_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _redact_entry_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return redact_identifier(value)

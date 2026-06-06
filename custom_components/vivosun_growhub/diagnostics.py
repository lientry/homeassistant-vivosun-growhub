"""Diagnostics support for the Vivosun GrowHub integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.diagnostics import async_redact_data

from .camera_config import camera_ips_from_options
from .const import CONF_CAMERA_IP, CONF_CAMERA_IPS, DOMAIN, OPTION_SUPPORT_CAPTURE_ENABLED
from .redaction import redact_identifier, sanitize_mapping_for_debug

if TYPE_CHECKING:
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
    CONF_CAMERA_IP,
    CONF_CAMERA_IPS,
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
    discovered_devices = _build_discovered_device_inventory(
        coordinator,
        primary_device_id=device.device_id if device else None,
    )
    camera_devices = getattr(coordinator, "camera_devices", [])
    if not isinstance(camera_devices, list):
        camera_devices = []
    camera_configuration = _build_camera_configuration(config_entry.options, camera_devices)
    identifier_collisions = _build_identifier_collisions(coordinator)
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

    last_update = getattr(coordinator, "last_update_success_time", None)
    last_update_iso = _as_iso(last_update) if isinstance(last_update, datetime) else None
    last_update_success = getattr(coordinator, "last_update_success", None)
    if not isinstance(last_update_success, bool):
        last_update_success = None

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
        "discovered_devices": discovered_devices,
        "camera_configuration": camera_configuration,
        "identifier_collisions": identifier_collisions,
        "support_capture": coordinator.support_capture_snapshot(),
        "coordinator": {
            "mqtt_connected": mqtt_connected,
            "support_capture_enabled": bool(config_entry.options.get(OPTION_SUPPORT_CAPTURE_ENABLED, False)),
            "support_capture_active": coordinator.support_capture_active,
            "last_update_success": last_update_success,
            "shadow_keys": shadow_keys,
            "sensor_keys": sensor_keys,
            "last_update_success_time": last_update_iso,
        },
    }
    redacted_diagnostics = async_redact_data(diagnostics_payload, _DIAGNOSTICS_REDACT)
    sanitized_diagnostics = sanitize_mapping_for_debug(redacted_diagnostics)
    return cast("dict[str, Any]", _json_safe_value(sanitized_diagnostics))


def _as_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _redact_entry_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return redact_identifier(value)


def _json_safe_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(nested_value) for key, nested_value in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    return f"<{type(value).__name__}>"


def _build_discovered_device_inventory(
    coordinator: object,
    *,
    primary_device_id: str | None,
) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for collection_name in ("devices", "camera_devices"):
        devices = getattr(coordinator, collection_name, ())
        if not isinstance(devices, list):
            continue
        for collection_index, candidate in enumerate(devices):
            device_id = getattr(candidate, "device_id", "")
            client_id = getattr(candidate, "client_id", "")
            topic_prefix = getattr(candidate, "topic_prefix", "")
            inventory.append(
                {
                    "collection": collection_name,
                    "collection_index": collection_index,
                    "name": getattr(candidate, "name", ""),
                    "online": getattr(candidate, "online", False),
                    "device_type": getattr(candidate, "device_type", "unknown"),
                    "scene_id": getattr(candidate, "scene_id", 0),
                    "device_id": device_id,
                    "client_id": client_id,
                    "topic_prefix": topic_prefix,
                    "is_primary": bool(primary_device_id and device_id == primary_device_id),
                }
            )

    return inventory


def _build_camera_configuration(
    options: Mapping[str, object],
    camera_devices: list[object],
) -> dict[str, object]:
    """Return diagnostics-safe camera configuration coverage."""
    typed_camera_devices = [device for device in camera_devices if hasattr(device, "device_id")]
    configured_ips = camera_ips_from_options(options, cast("list[Any]", typed_camera_devices))
    return {
        "discovered_count": len(typed_camera_devices),
        "configured_count": sum(
            1 for device in typed_camera_devices if getattr(device, "device_id", "") in configured_ips
        ),
        "uses_legacy_single_ip": isinstance(options.get(CONF_CAMERA_IP), str),
        "cameras": [
            {
                "name": getattr(device, "name", ""),
                "device_id": getattr(device, "device_id", ""),
                "scene_id": getattr(device, "scene_id", 0),
                "online": getattr(device, "online", False),
                "ip_configured": getattr(device, "device_id", "") in configured_ips,
                "lan_username_present": bool(getattr(device, "camera_username", None)),
                "lan_password_present": bool(getattr(device, "camera_password", None)),
            }
            for device in typed_camera_devices
        ],
    }


def _build_identifier_collisions(coordinator: object) -> list[dict[str, object]]:
    """Return duplicate cloud identifiers that can break routing or entity identity."""
    devices: list[object] = []
    for collection_name in ("devices", "camera_devices"):
        collection = getattr(coordinator, collection_name, ())
        if isinstance(collection, list):
            devices.extend(collection)

    collisions: list[dict[str, object]] = []
    for attribute in ("device_id", "client_id", "topic_prefix"):
        grouped: dict[str, list[object]] = {}
        for device in devices:
            value = getattr(device, attribute, "")
            if isinstance(value, str) and value:
                grouped.setdefault(value, []).append(device)
        for value, matches in grouped.items():
            if len(matches) < 2:
                continue
            collisions.append(
                {
                    "identifier_type": attribute,
                    "identifier_value_redacted": redact_identifier(value),
                    "count": len(matches),
                    "devices": [
                        {
                            "name": getattr(device, "name", ""),
                            "device_type": getattr(device, "device_type", "unknown"),
                            "scene_id": getattr(device, "scene_id", 0),
                        }
                        for device in matches
                    ],
                }
            )
    return collisions

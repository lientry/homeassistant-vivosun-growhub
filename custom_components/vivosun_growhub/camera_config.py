"""Shared GrowCam configuration helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from .const import CONF_CAMERA_IP, CONF_CAMERA_IPS

if TYPE_CHECKING:
    from .models import DeviceInfo


def camera_ips_from_options(
    options: Mapping[str, object],
    camera_devices: Sequence[DeviceInfo],
) -> dict[str, str]:
    """Return configured camera IPs, including the legacy single-camera option."""
    camera_ips: dict[str, str] = {}
    configured = options.get(CONF_CAMERA_IPS)
    if isinstance(configured, Mapping):
        for device_id, value in configured.items():
            if isinstance(device_id, str) and isinstance(value, str) and value.strip():
                camera_ips[device_id] = value.strip()

    legacy_ip = options.get(CONF_CAMERA_IP)
    if camera_devices and isinstance(legacy_ip, str) and legacy_ip.strip():
        camera_ips.setdefault(camera_devices[0].device_id, legacy_ip.strip())
    return camera_ips

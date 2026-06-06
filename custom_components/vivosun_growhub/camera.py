"""Camera platform for the Vivosun GrowHub integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from homeassistant.components.camera import Camera, CameraEntityFeature

from .camera_config import camera_ips_from_options
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo as HADeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import DeviceInfo, RuntimeData

_LOGGER = logging.getLogger(__name__)


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    """Return integration runtime data for the config entry."""
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun camera entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    camera_devices = coordinator.camera_devices
    if not camera_devices:
        return
    camera_ips = camera_ips_from_options(entry.options, camera_devices)

    entities: list[VivosunGrowCamEntity] = []
    for camera_device in camera_devices:
        camera_ip = camera_ips.get(camera_device.device_id)
        if not camera_ip:
            continue
        if not camera_device.camera_username or not camera_device.camera_password:
            _LOGGER.warning(
                "Skipping Vivosun camera %s because LAN credentials are missing",
                camera_device.name,
            )
            continue
        entities.append(VivosunGrowCamEntity(device=camera_device, camera_ip=camera_ip))

    if entities:
        async_add_entities(entities)


class VivosunGrowCamEntity(Camera):  # type: ignore[misc]
    """Representation of a Vivosun GrowCam RTSP stream."""

    _attr_has_entity_name = True
    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, *, device: DeviceInfo, camera_ip: str) -> None:
        """Initialize camera entity."""
        super().__init__()
        self._device = device
        self._camera_ip = camera_ip
        self._attr_unique_id = f"vivosun_growhub_{device.device_id}_camera"
        self._attr_brand = "VIVOSUN"
        self._attr_model = device.name

    @property
    def available(self) -> bool:
        """Return True when the camera has the required connection details."""
        return bool(self._camera_ip and self._device.camera_username and self._device.camera_password)

    @property
    def use_stream_for_stills(self) -> bool:
        """Use the stream as the still-image source."""
        return True

    @property
    def device_info(self) -> HADeviceInfo:
        """Return device info for the camera."""
        from homeassistant.helpers.device_registry import DeviceInfo as HADeviceInfo

        return HADeviceInfo(
            identifiers={(DOMAIN, self._device.device_id)},
            name=self._device.name,
            manufacturer="VIVOSUN",
            model=self._device.name,
        )

    async def stream_source(self) -> str | None:
        """Return the RTSP source for Home Assistant stream handling."""
        username = self._device.camera_username
        password = self._device.camera_password
        if not username or not password:
            return None
        return f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}@{self._camera_ip}:554/"

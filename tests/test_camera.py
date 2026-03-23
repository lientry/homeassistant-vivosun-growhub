"""Tests for Vivosun camera platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.camera import VivosunGrowCamEntity, async_setup_entry
from custom_components.vivosun_growhub.const import CONF_CAMERA_IP, DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _StubCoordinator:
    def __init__(self) -> None:
        self.camera_devices = [
            DeviceInfo(
                device_id="camera-1",
                client_id="",
                topic_prefix="",
                name="GrowCam C4",
                online=True,
                scene_id=1001,
                device_type="camera",
                camera_username="abjd",
                camera_password="4kt5em",
            )
        ]


async def test_camera_setup_creates_camera_entity_when_ip_is_configured(
    hass: HomeAssistant,
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={CONF_CAMERA_IP: "10.0.15.202"})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunGrowCamEntity] = []

    def _add(entities: list[VivosunGrowCamEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 1
    assert added[0].unique_id == "vivosun_growhub_camera-1_camera"
    assert added[0].available is True
    assert await added[0].stream_source() == "rtsp://abjd:4kt5em@10.0.15.202:554/"


async def test_camera_setup_skips_when_ip_not_configured(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunGrowCamEntity] = []

    def _add(entities: list[VivosunGrowCamEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert added == []


async def test_camera_stream_source_quotes_credentials() -> None:
    entity = VivosunGrowCamEntity(
        device=DeviceInfo(
            device_id="camera-1",
            client_id="",
            topic_prefix="",
            name="GrowCam C4",
            online=True,
            scene_id=1001,
            device_type="camera",
            camera_username="user:name",
            camera_password="pw@rd/1",
        ),
        camera_ip="10.0.15.202",
    )

    assert await entity.stream_source() == "rtsp://user%3Aname:pw%40rd%2F1@10.0.15.202:554/"


async def test_camera_entity_unavailable_without_credentials() -> None:
    entity = VivosunGrowCamEntity(
        device=DeviceInfo(
            device_id="camera-1",
            client_id="",
            topic_prefix="",
            name="GrowCam C4",
            online=True,
            scene_id=1001,
            device_type="camera",
            camera_username=None,
            camera_password=None,
        ),
        camera_ip="10.0.15.202",
    )

    assert entity.available is False
    assert await entity.stream_source() is None

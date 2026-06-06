"""Diagnostics tests for Vivosun GrowHub."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import CONF_CAMERA_IPS, DOMAIN, OPTION_SUPPORT_CAPTURE_ENABLED
from custom_components.vivosun_growhub.diagnostics import async_get_config_entry_diagnostics
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _CoordinatorStub:
    def __init__(self) -> None:
        self.device = DeviceInfo(
            device_id="device-123456",
            client_id="vivosun-VSCTLE42A-account-device-123456",
            topic_prefix="vivosun/user/123456/device/123456",
            name="GrowHub A",
            online=True,
            scene_id=66078,
            device_type="controller",
        )
        self.data: dict[str, object] = {
            "mqtt_connected": True,
            "devices": {self.device.device_id: self.device},
            "shadows": {self.device.device_id: {"light": {"level": 42}, "connection": {"connected": True}}},
            "sensors": {self.device.device_id: {"inTemp": 2500, "inHumi": 5123}},
        }
        self.camera_devices = [
            DeviceInfo(
                device_id="camera-654321",
                client_id="",
                topic_prefix="",
                name="GrowCam C4",
                online=True,
                scene_id=66079,
                device_type="camera",
            )
        ]
        self.is_mqtt_connected = True
        self.last_update_success = True
        self.last_update_success_time = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)
        self.support_capture_active = True

    @property
    def devices(self) -> list[DeviceInfo]:
        return getattr(self, "_devices", [self.device])

    def support_capture_snapshot(self) -> dict[str, object]:
        return {
            "active": True,
            "started_at": "2026-03-05T11:55:00+00:00",
            "stopped_at": None,
            "max_events": 500,
            "dropped_events": 0,
            "subscription_topics": ["$aws/things/device/shadow/get/rejected"],
            "subscription_results": [
                {
                    "topic": "$aws/things/device/shadow/get/rejected",
                    "status": "accepted",
                }
            ],
            "model_metadata_results": [
                {
                    "model_code": "VSHUMH05",
                    "matched": True,
                    "default_name": "AeroStream H05",
                    "comm_mode_list": ["MQTT"],
                }
            ],
            "devices": [{"device_id": "device-123456", "device_type": "controller"}],
            "events": [{"ts": "2026-03-05T11:55:01+00:00", "kind": "capture_started"}],
        }


async def test_diagnostics_redacts_sensitive_values(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user-998877",
        data={
            "email": "user@example.com",
            "password": "super-secret",
            "access_token": "token-123",
            "login_token": "token-456",
            "aws_identity_id": "us-east-2:identity-raw",
        },
        options={
            "temp_unit": "celsius",
            OPTION_SUPPORT_CAPTURE_ENABLED: True,
            CONF_CAMERA_IPS: {"camera-654321": "10.0.15.202"},
        },
    )
    entry.add_to_hass(hass)
    coordinator = _CoordinatorStub()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    config_entry = cast("dict[str, object]", result["config_entry"])
    assert config_entry["title"] != "user@example.com"
    assert config_entry["unique_id"] != "user-998877"
    data = cast("dict[str, object]", config_entry["data"])
    assert data["password"] == "***"
    assert data["access_token"] == "***"
    assert data["login_token"] == "***"
    assert data["email"] != "user@example.com"
    assert data["aws_identity_id"] != "us-east-2:identity-raw"

    device = cast("dict[str, object]", result["device"])
    assert device["device_id"] != "device-123456"
    assert device["client_id"] != "vivosun-VSCTLE42A-account-device-123456"
    assert device["topic_prefix"] != "vivosun/user/123456/device/123456"

    discovered_devices = cast("list[dict[str, object]]", result["discovered_devices"])
    assert len(discovered_devices) == 2
    assert discovered_devices[0]["device_type"] == "controller"
    assert discovered_devices[0]["is_primary"] is True
    assert discovered_devices[0]["device_id"] != "device-123456"
    assert discovered_devices[1]["device_type"] == "camera"
    assert discovered_devices[1]["is_primary"] is False
    camera_configuration = cast("dict[str, object]", result["camera_configuration"])
    assert camera_configuration["discovered_count"] == 1
    assert camera_configuration["configured_count"] == 1
    cameras = cast("list[dict[str, object]]", camera_configuration["cameras"])
    assert cameras[0]["ip_configured"] is True
    assert "10.0.15.202" not in json.dumps(result)
    assert result["identifier_collisions"] == []

    support_capture = cast("dict[str, object]", result["support_capture"])
    assert support_capture["active"] is True
    assert support_capture["max_events"] == 500
    assert support_capture["dropped_events"] == 0

    coordinator_result = cast("dict[str, object]", result["coordinator"])
    assert coordinator_result["mqtt_connected"] is True
    assert coordinator_result["support_capture_enabled"] is True
    assert coordinator_result["support_capture_active"] is True
    assert coordinator_result["last_update_success"] is True
    assert coordinator_result["shadow_keys"] == ["connection", "light"]
    assert coordinator_result["sensor_keys"] == ["inHumi", "inTemp"]
    assert coordinator_result["last_update_success_time"] == "2026-03-05T12:00:00+00:00"
    json.dumps(result)


async def test_diagnostics_reports_identifier_collisions(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user-998877",
        data={"email": "user@example.com", "password": "super-secret"},
    )
    entry.add_to_hass(hass)
    coordinator = _CoordinatorStub()
    duplicate = DeviceInfo(
        device_id="device-other",
        client_id=coordinator.device.client_id,
        topic_prefix=coordinator.device.topic_prefix,
        name="GrowHub Duplicate",
        online=True,
        scene_id=66080,
        device_type="controller",
    )
    coordinator._devices = [coordinator.device, duplicate]
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    collisions = cast("list[dict[str, object]]", result["identifier_collisions"])
    assert {collision["identifier_type"] for collision in collisions} == {
        "client_id",
        "topic_prefix",
    }
    assert all(collision["count"] == 2 for collision in collisions)


async def test_diagnostics_handles_missing_runtime(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user-998877",
        data={"email": "user@example.com", "password": "super-secret"},
    )
    entry.add_to_hass(hass)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["runtime_available"] is False
    assert result["device"] is None
    assert result["coordinator"] is None
    config_entry = cast("dict[str, object]", result["config_entry"])
    assert config_entry["title"] != "user@example.com"
    assert config_entry["unique_id"] != "user-998877"
    data = cast("dict[str, object]", config_entry["data"])
    assert data["password"] == "***"
    assert data["email"] != "user@example.com"


async def test_diagnostics_coerces_non_serializable_support_capture_values(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user-998877",
        data={"email": "user@example.com", "password": "super-secret"},
    )
    entry.add_to_hass(hass)
    coordinator = _CoordinatorStub()

    def _snapshot_with_object() -> dict[str, object]:
        return {
            "active": False,
            "started_at": None,
            "stopped_at": None,
            "max_events": 0,
            "dropped_events": 0,
            "subscription_topics": [],
            "subscription_results": [],
            "model_metadata_results": [],
            "devices": [],
            "events": [{"raw": object()}],
        }

    coordinator.support_capture_snapshot = _snapshot_with_object  # type: ignore[method-assign]
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    support_capture = cast("dict[str, object]", result["support_capture"])
    events = cast("list[dict[str, object]]", support_capture["events"])
    assert events[0]["raw"] == "<object>"
    json.dumps(result)


async def test_diagnostics_handles_missing_last_update_success_time(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user-998877",
        data={"email": "user@example.com", "password": "super-secret"},
    )
    entry.add_to_hass(hass)
    coordinator = _CoordinatorStub()
    del coordinator.last_update_success_time
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    coordinator_result = cast("dict[str, object]", result["coordinator"])
    assert coordinator_result["last_update_success"] is True
    assert coordinator_result["last_update_success_time"] is None

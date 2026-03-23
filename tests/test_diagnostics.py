"""Diagnostics tests for Vivosun GrowHub."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
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
            "shadows": {
                self.device.device_id: {"light": {"level": 42}, "connection": {"connected": True}}
            },
            "sensors": {self.device.device_id: {"inTemp": 2500, "inHumi": 5123}},
        }
        self.is_mqtt_connected = True
        self.last_update_success_time = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)


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
        options={"temp_unit": "celsius"},
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

    coordinator_result = cast("dict[str, object]", result["coordinator"])
    assert coordinator_result["mqtt_connected"] is True
    assert coordinator_result["shadow_keys"] == ["connection", "light"]
    assert coordinator_result["sensor_keys"] == ["inHumi", "inTemp"]
    assert coordinator_result["last_update_success_time"] == "2026-03-05T12:00:00+00:00"


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

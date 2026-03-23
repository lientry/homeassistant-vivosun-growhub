"""Smoke tests for full mocked bootstrap and lifecycle behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from tests.test_coordinator import _ApiStub, _AwsAuthStub, _credentials, _MqttStub, _patch_coordinator_deps

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


async def test_smoke_full_bootstrap_chain_and_control_roundtrip(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="smoke@example.com", password="secret")
    await coordinator.async_start()
    mqtt = _MqttStub.instances[0]

    assert api.calls == ["login", "get_devices", "get_aws_identity", "get_point_log", "get_point_log"]
    assert aws_auth.calls[:2] == ["get_credentials_for_identity", "sigv4_sign_mqtt_url"]
    assert mqtt.connected is True
    assert mqtt.published[0][0].endswith("/get")

    device_id = coordinator.device.device_id
    await coordinator.async_publish_shadow_update({"state": {"desired": {"light": {"manu": {"lv": 66}}}}})
    await mqtt.emit(
        f"$aws/things/{coordinator.device.client_id}/shadow/update/accepted",
        b'{"state":{"reported":{"light":{"mode":0,"lv":66,"manu":{"lv":66,"spec":20}}}}}',
    )

    shadows = coordinator.data["shadows"]
    assert isinstance(shadows, dict)
    device_shadow = shadows[device_id]
    assert isinstance(device_shadow, dict)
    assert device_shadow["light"]["level"] == 66
    # Last published message is the shadow update control payload
    last_published = mqtt.published[-1]
    assert last_published[1] == b'{"state":{"desired":{"light":{"manu":{"lv":66}}}}}'

    await coordinator.async_shutdown()


async def test_smoke_unload_reload_lifecycle_restarts_cleanly(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=2)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="smoke@example.com", password="secret")
    await coordinator.async_start()

    first_refresh_task = coordinator._refresh_task
    first_reconnect_task = coordinator._reconnect_task
    first_mqtt = _MqttStub.instances[0]

    await coordinator.async_shutdown()
    assert first_mqtt.disconnect_calls == 1
    assert first_refresh_task is None or first_refresh_task.done()
    assert first_reconnect_task is None or first_reconnect_task.done()

    await coordinator.async_start()
    assert len(_MqttStub.instances) == 2
    assert coordinator.is_mqtt_connected is True

    await coordinator.async_shutdown()

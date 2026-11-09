"""A10 single-outlet behavior from sanitized MQTT traffic shaped like a real A10 shadow."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vivosun_growhub.api import VivosunApiClient
from custom_components.vivosun_growhub.binary_sensor import async_setup_entry as setup_binary_sensors
from custom_components.vivosun_growhub.climate import async_setup_entry as setup_climate
from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.fan import async_setup_entry as setup_fans
from custom_components.vivosun_growhub.humidifier import async_setup_entry as setup_humidifiers
from custom_components.vivosun_growhub.light import async_setup_entry as setup_lights
from custom_components.vivosun_growhub.model_metadata import support_capture_model_metadata
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData, infer_device_type
from custom_components.vivosun_growhub.number import async_setup_entry as setup_numbers
from custom_components.vivosun_growhub.ports import A10_PORTS, PORTS_BY_MODEL, build_manual_port_payload
from custom_components.vivosun_growhub.select import async_setup_entry as setup_selects
from custom_components.vivosun_growhub.sensor import async_setup_entry as setup_sensors
from custom_components.vivosun_growhub.switch import _PORT_MODEL_NAMES
from custom_components.vivosun_growhub.switch import async_setup_entry as setup_switches
from tests.test_api import _MockResponse, _MockSession, _valid_tokens

if TYPE_CHECKING:
    import aiohttp
    from homeassistant.core import HomeAssistant

_CLIENT_ID = "vivosun-VSPLUGA10-account-a10"


def test_a10_model_token_wins_over_growhub_name() -> None:
    assert infer_device_type("GrowHub A10", _CLIENT_ID) == "a10"
    assert infer_device_type("GrowHub controller", _CLIENT_ID) == "a10"
    assert infer_device_type("GrowHub E42A", "vivosun-VSCTLE42A-acct-id") == "controller"


def test_a10_metadata_and_ports() -> None:
    metadata = support_capture_model_metadata(_CLIENT_ID)
    assert metadata["default_name"] == "GrowHub A10"
    assert metadata["device_type"] == "a10"
    assert metadata["data_upload_groups"] == []
    assert A10_PORTS == ("plug1",)
    assert PORTS_BY_MODEL["VSPLUGA10"] == A10_PORTS
    assert set(_PORT_MODEL_NAMES) == set(PORTS_BY_MODEL)


async def _setup(hass: object) -> tuple[object, MockConfigEntry, DeviceInfo]:
    from custom_components.vivosun_growhub.coordinator import VivosunCoordinator

    device_type = infer_device_type("GrowHub A10", _CLIENT_ID)
    device = DeviceInfo(
        "a10-1", _CLIENT_ID, "topic/a10", "GrowHub A10", True, 0, device_type, supports_point_log=False
    )
    coordinator = VivosunCoordinator(hass, object(), email="offline@example.com", password="unused")
    coordinator._devices = [device]
    coordinator._build_topic_maps()
    coordinator._support_capture = type("Capture", (), {"record": lambda *a, **kw: None})()
    coordinator.async_update_listeners = lambda: None
    coordinator._refresh_plan_stages = AsyncMock()
    coordinator.async_publish_shadow_update = AsyncMock()
    coordinator._mqtt_client = type("ConnectedMqtt", (), {"is_connected": True})()
    coordinator.data = coordinator._build_state_snapshot()
    entry = MockConfigEntry(domain=DOMAIN, title="a10", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry_id=entry.entry_id, coordinator=coordinator)
    return coordinator, entry, device


async def test_a10_creates_one_outlet_and_no_controller_entities(hass: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.fan.async_get_current_platform", MagicMock)
    _, entry, _ = await _setup(hass)
    switches: list = []
    await setup_switches(hass, entry, switches.extend)
    assert [(entity.unique_id, entity.name) for entity in switches] == [("vivosun_growhub_a10-1_plug1", "Outlet 1")]

    # sensor, light and fan are the checks that would fail if the A10 were classified as a
    # controller; the other platforms gate on options or device types an A10 never has.
    for setup in (
        setup_sensors,
        setup_lights,
        setup_fans,
        setup_selects,
        setup_climate,
        setup_humidifiers,
        setup_numbers,
    ):
        added: list = []
        await setup(hass, entry, added.extend)
        assert added == [], setup.__module__

    binary_sensors: list = []
    await setup_binary_sensors(hass, entry, binary_sensors.extend)
    assert [entity.unique_id for entity in binary_sensors] == ["vivosun_growhub_a10-1_connected"]


async def test_a10_reported_outlet_and_guarded_writes(hass: object) -> None:
    coordinator, entry, device = await _setup(hass)
    switches: list = []
    await setup_switches(hass, entry, switches.extend)
    outlet = switches[0]
    topic = f"$aws/things/{device.client_id}/shadow/update/accepted"
    get_topic = f"$aws/things/{device.client_id}/shadow/get/accepted"

    async def deliver(document: dict[str, object], on_topic: str = topic) -> None:
        await coordinator._handle_mqtt_publish(on_topic, json.dumps(document).encode(), 1)

    assert outlet.is_on is None
    assert outlet.available is False
    await deliver({"state": {"desired": {"plug1": {"manu": {"lv": 100}}}}})
    assert outlet.is_on is None
    assert outlet.available is False

    full_report = {
        "state": {
            "desired": {"plug1": {"manu": {"lv": 100}, "dev": 0}, "btDev": {"snsTH": []}},
            "reported": {
                "connected": True,
                "openApi": 0,
                "snsExt": {"ch": 0},
                "btDev": {"snsTH": []},
                "plug1": {
                    "dev": 0,
                    "mode": 0,
                    "inPlan": 0,
                    "lv": 100,
                    "manu": {"lv": 100},
                    "cycle": {"tOffset": 0, "onDur": 3000, "offDur": 600},
                    "auto": {
                        "tMin": 0,
                        "tMax": 0,
                        "tHys": 100,
                        "hMin": 0,
                        "hMax": 0,
                        "hHys": 400,
                        "vMin": 0,
                        "vMax": 0,
                        "vHys": 10,
                        "cMin": 0,
                        "cMax": 0,
                        "cHys": 5000,
                    },
                },
            },
        }
    }
    await deliver(full_report, get_topic)
    assert outlet.is_on is True
    assert outlet.available is True
    assert outlet.extra_state_attributes == {"accessory_code": 0, "mode_code": 0, "in_plan": 0}

    await deliver({"state": {"reported": {"connected": False}}})
    assert outlet.available is False
    with pytest.raises(HomeAssistantError, match="GrowHub A10 Outlet 1 is unavailable"):
        await outlet.async_turn_on()
    await deliver({"state": {"reported": {"connected": True}}})
    assert outlet.available is True

    await deliver({"state": {"reported": {"plug1": {"lv": 50}}}})
    assert outlet.is_on is None
    assert outlet.available is False
    with pytest.raises(HomeAssistantError, match="GrowHub A10 Outlet 1 has no reported output level"):
        await outlet.async_turn_on()
    await deliver({"state": {"reported": {"plug1": {"lv": 100}}}})
    assert outlet.is_on is True
    assert outlet.available is True
    coordinator.async_publish_shadow_update.assert_not_awaited()
    await deliver({"state": {"reported": {"plug1": {"lv": 0, "manu": {"lv": 0}}}}})
    assert outlet.is_on is False
    await deliver({"state": {"desired": {"plug1": {"manu": {"lv": 100}}}}})
    assert outlet.is_on is False

    await outlet.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"plug1": {"manu": {"lv": 100}}}}}, device_id="a10-1", qos=1
    )
    coordinator.async_publish_shadow_update.reset_mock()
    await outlet.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_manual_port_payload("plug1", False), device_id="a10-1", qos=1
    )
    assert build_manual_port_payload("plug1", False) == {"state": {"desired": {"plug1": {"manu": {"lv": 0}}}}}
    coordinator.async_publish_shadow_update.reset_mock()

    for guard in ({"mode": 1, "inPlan": 0}, {"mode": 0, "inPlan": 1}):
        await deliver({"state": {"reported": {"plug1": guard}}})
        assert outlet.available is True
        with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
            await outlet.async_turn_on()
        with pytest.raises(ServiceValidationError, match="manual mode and no active plan"):
            await outlet.async_turn_off()
    coordinator.async_publish_shadow_update.assert_not_awaited()


def _a10_device_list(scene: object) -> dict[str, object]:
    return {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "a10-1",
                        "clientId": _CLIENT_ID,
                        "topicPrefix": "vivosun/topic/a10",
                        "name": "GrowHub A10",
                        "onlineStatus": 1,
                        "scene": scene,
                    }
                ]
            }
        },
    }


async def _parse_a10(scene: object) -> tuple[VivosunApiClient, list[DeviceInfo]]:
    payload = _a10_device_list(scene)
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)
    return client, await client.get_devices(_valid_tokens())


@pytest.mark.parametrize("scene", [None, {"sceneId": 1001}])
async def test_a10_parsing_keeps_device_without_point_log(scene: object) -> None:
    client, devices = await _parse_a10(scene)

    assert len(devices) == 1
    assert devices[0].device_type == "a10"
    assert devices[0].supports_point_log is False
    assert client.skipped_devices == []


async def test_a10_parsed_with_scene_skips_point_log_polling(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
    from tests.test_coordinator import _ApiStub, _AwsAuthStub, _credentials, _patch_coordinator_deps

    _, devices = await _parse_a10({"sceneId": 1001})
    api = _ApiStub()
    api.devices = devices
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    assert api.calls == ["login", "get_devices", "get_aws_identity"]

    await coordinator.async_shutdown()

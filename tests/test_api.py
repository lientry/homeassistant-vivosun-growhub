"""Unit tests for WP-2 REST bootstrap API client."""

from __future__ import annotations

import json
from typing import cast

import aiohttp
import pytest

from custom_components.vivosun_growhub.api import VivosunApiClient
from custom_components.vivosun_growhub.exceptions import (
    VivosunAuthError,
    VivosunConnectionError,
    VivosunResponseError,
)
from custom_components.vivosun_growhub.models import AuthTokens
from custom_components.vivosun_growhub.support_capture import SupportCaptureManager


class _MockResponse:
    """Minimal async response context manager used by unit tests."""

    def __init__(self, *, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> _MockResponse:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = (exc_type, exc, tb)
        return None

    async def json(self, *, content_type: str | None = None) -> object:
        _ = content_type
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _MockSession:
    """Minimal request dispatcher for API client tests."""

    def __init__(self, responses: list[_MockResponse] | None = None, *, request_error: Exception | None = None) -> None:
        self._responses = responses or []
        self._request_error = request_error
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _MockResponse:
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self._request_error is not None:
            raise self._request_error
        if not self._responses:
            raise AssertionError("No mock responses left")
        return self._responses.pop(0)


def _valid_tokens() -> AuthTokens:
    return AuthTokens(
        access_token="access-token-value",
        login_token="login-token-value",
        refresh_token="refresh-token-value",
        user_id="153685567990966911",
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_stage_info_capture_records_existing_success_only_when_enabled(enabled: bool) -> None:
    payload = {"code": 0, "success": True, "message": "success", "data": {
        "stageName": "Dry", "icon": "dry", "planStageContent": json.dumps({
            "duration": 480, "temperature": "18", "hwId": "raw-hardware",
            "nested": {"hardwareId": "other-hardware", "userPlanId": "recipe-1"},
        }),
    }}
    session = _MockSession(responses=[_MockResponse(status=200, payload=payload)])
    client = VivosunApiClient(cast("aiohttp.ClientSession", session))
    capture = SupportCaptureManager()
    client.set_support_capture(capture)
    if enabled:
        capture.start(max_events=10, devices=[], subscription_topics=[])

    result = await client.get_plan_stage_info(_valid_tokens(), "stage-1")

    assert result is not None and result.stage_name == "Dry"
    assert result.icon == "dry"
    assert result.content["hwId"] == "raw-hardware"
    assert len(session.calls) == 1
    assert session.calls[0]["url"].endswith("/iot/plan/stageInfo")
    saved = capture.snapshot()["stage_info_results"]
    if not enabled:
        assert saved == []
        return
    assert len(saved) == 1
    assert saved[0]["stage_id"] == "stage-1"
    assert saved[0]["status"] == "success"
    assert saved[0]["stage_name"] == "Dry"
    assert saved[0]["icon"] == "dry"
    assert saved[0]["content_status"] == "parsed"
    assert saved[0]["content"]["duration"] == 480
    assert saved[0]["content"]["temperature"] == "18"
    assert saved[0]["content"]["hwId"] == "***"
    assert saved[0]["content"]["nested"] == {"hardwareId": "***", "userPlanId": "recipe-1"}
    assert "raw-hardware" not in str(saved)


@pytest.mark.parametrize("content", ["not-json", "[1, 2]", "{}"])
async def test_stage_info_capture_retains_empty_or_unparsed_outcome(content: str) -> None:
    session = _MockSession(responses=[_MockResponse(status=200, payload={
        "success": True, "message": "success",
        "data": {"stageName": "Cure", "icon": "", "planStageContent": content},
    })])
    client = VivosunApiClient(cast("aiohttp.ClientSession", session))
    capture = SupportCaptureManager()
    capture.start(max_events=2, devices=[], subscription_topics=[])
    client.set_support_capture(capture)

    result = await client.get_plan_stage_info(_valid_tokens(), "stage-3")

    assert result is not None and result.content == {}
    assert len(session.calls) == 1
    assert capture.snapshot()["stage_info_results"] == [{
        "stage_id": "stage-3", "status": "success", "stage_name": "Cure", "icon": "",
        "content": {}, "content_status": "empty_or_unparsed", "truncated": False,
    }]


async def test_stage_info_capture_failure_preserves_original_return_and_request_count() -> None:
    session = _MockSession(responses=[_MockResponse(status=200, payload={
        "code": 1, "success": False, "message": "not found", "data": {},
    })])
    client = VivosunApiClient(cast("aiohttp.ClientSession", session))
    capture = SupportCaptureManager()
    capture.start(max_events=10, devices=[], subscription_topics=[])
    client.set_support_capture(capture)

    assert await client.get_plan_stage_info(_valid_tokens(), "stage-2") is None
    assert len(session.calls) == 1
    assert capture.snapshot()["stage_info_results"] == [{
        "stage_id": "stage-2", "status": "error", "error_type": "VivosunResponseError", "truncated": False,
    }]


async def test_stage_info_capture_connection_error_preserves_return() -> None:
    session = _MockSession(request_error=aiohttp.ClientConnectionError("offline"))
    client = VivosunApiClient(cast("aiohttp.ClientSession", session))
    capture = SupportCaptureManager()
    capture.start(max_events=2, devices=[], subscription_topics=[])
    client.set_support_capture(capture)

    assert await client.get_plan_stage_info(_valid_tokens(), "stage-4") is None
    assert len(session.calls) == 1
    assert capture.snapshot()["stage_info_results"] == [{
        "stage_id": "stage-4", "status": "error", "error_type": "VivosunConnectionError", "truncated": False,
    }]


async def test_login_success_returns_auth_tokens(caplog: pytest.LogCaptureFixture) -> None:
    """login() should parse envelope and return AuthTokens dataclass."""
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "accessToken": "access-123",
            "loginToken": "login-123",
            "refreshToken": "refresh-123",
            "userId": "1536",
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    caplog.set_level("INFO")
    tokens = await client.login("user@example.com", "secret")

    assert tokens.access_token == "access-123"
    assert tokens.login_token == "login-123"
    assert tokens.refresh_token == "refresh-123"
    assert tokens.user_id == "1536"
    assert "access-123" not in caplog.text
    assert "login-123" not in caplog.text
    assert "refresh-123" not in caplog.text


async def test_discovery_accepts_vseed_without_scene_and_retains_safe_capture_routing() -> None:
    entries = [
        {
            "name": "VSeed S40",
            "deviceId": "seed-device",
            "clientId": "vivosun-VSSBS40-account-seed",
            "topicPrefix": "vivosun/seed",
            "setting": {"password": "never-copy"},
        },
        {"name": "No routing", "deviceId": "unrouted"},
        {"name": "Invalid routing", "clientId": "other/+/thing"},
        {
            "name": "GrowCam C4",
            "clientId": "camera-thing",
            "deviceId": "cam",
            "setting": {"jf": {"devUser": "camera-user", "devPass": "camera-secret"}},
        },
    ]
    session = cast(
        "aiohttp.ClientSession",
        _MockSession(
            responses=[
                _MockResponse(
                    status=200,
                    payload={"success": True, "message": "success", "data": {"deviceGroup": {"STORE": entries}}},
                )
            ]
        ),
    )
    api = VivosunApiClient(session)

    devices = await api.get_devices(_valid_tokens())

    vseed = next(device for device in devices if device.name == "VSeed S40")
    assert vseed.device_type == "vseed"
    assert vseed.supports_point_log is True
    assert vseed.scene_id == 0
    candidates = api.capture_devices
    assert len(candidates) == 1
    assert candidates[0].client_id == "vivosun-VSSBS40-account-seed"
    assert candidates[0].missing_fields == ()
    assert candidates[0].topic_prefix == "vivosun/seed"
    assert "never-copy" not in repr(candidates)
    assert "camera-secret" not in repr(candidates)


async def test_login_auth_failure_message_raises_auth_error() -> None:
    """login() should classify auth-style envelope failures as VivosunAuthError."""
    payload = {
        "code": 1,
        "success": False,
        "message": "Invalid credentials",
        "data": {},
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    with pytest.raises(VivosunAuthError):
        await client.login("user@example.com", "wrong")


async def test_get_devices_maps_grow_group_to_device_info() -> None:
    """get_devices() should parse supported entries and infer device type."""
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "device-1",
                        "clientId": "vivosun-VSCTLE42A-account-device-1",
                        "topicPrefix": "vivosun/topic/1",
                        "name": "GrowHub A",
                        "onlineStatus": 1,
                        "scene": {"sceneId": 1001},
                    },
                    {
                        "deviceId": "device-2",
                        "clientId": "vivosun-VSHUMH19-account-device-2",
                        "topicPrefix": "vivosun/topic/2",
                        "name": "AeroStream H19",
                        "onlineStatus": 0,
                        "scene": {"sceneId": 1002},
                    },
                ]
            }
        },
    }
    mock_session = _MockSession(responses=[_MockResponse(status=200, payload=payload)])
    session = cast("aiohttp.ClientSession", mock_session)
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert len(devices) == 2
    assert devices[0].device_id == "device-1"
    assert devices[0].online is True
    assert devices[0].scene_id == 1001
    assert devices[0].device_type == "controller"
    assert devices[1].device_id == "device-2"
    assert devices[1].online is False
    assert devices[1].scene_id == 1002
    assert devices[1].device_type == "humidifier"

    headers = cast("dict[str, str]", mock_session.calls[0]["kwargs"]["headers"])
    assert headers["login-token"] == "login-token-value"
    assert headers["access-token"] == "access-token-value"


async def test_get_devices_maps_vsctl_controller_token_to_controller() -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "device-1",
                        "clientId": "vivosun-VSCTL002-account-device-1",
                        "topicPrefix": "vivosun/topic/1",
                        "name": "E25 Controller",
                        "onlineStatus": 1,
                        "scene": {"sceneId": 1001},
                    }
                ]
            }
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert len(devices) == 1
    assert devices[0].device_type == "controller"


async def test_get_devices_maps_vcure_without_scene_to_curing_box() -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "STORE": [
                    {
                        "deviceId": "vcure-1",
                        "clientId": "vivosun-VSCBC80-account-device-1",
                        "topicPrefix": "vivosun/topic/vcure",
                        "name": "VCure C80",
                        "onlineStatus": 1,
                        "scene": None,
                    }
                ]
            }
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert len(devices) == 1
    assert devices[0].device_type == "curing_box"
    assert devices[0].scene_id == 0
    assert devices[0].supports_point_log is True  # VSCBC80 serves point_log despite missing sceneId
    assert client.skipped_devices == []


async def test_get_devices_coerces_string_online_status() -> None:
    """get_devices() should coerce string onlineStatus to int (API inconsistency)."""
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "device-1",
                        "clientId": "vivosun-VSCTLE42A-account-device-1",
                        "topicPrefix": "vivosun/topic/1",
                        "name": "GrowHub A",
                        "onlineStatus": "1",
                        "scene": {"sceneId": 1001},
                    },
                ]
            }
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert len(devices) == 1
    assert devices[0].online is True


async def test_get_devices_handles_missing_online_status() -> None:
    """get_devices() should default to offline when onlineStatus is absent."""
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "device-1",
                        "clientId": "vivosun-VSCTLE42A-account-device-1",
                        "topicPrefix": "vivosun/topic/1",
                        "name": "GrowHub A",
                        "scene": {"sceneId": 1001},
                    },
                ]
            }
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert len(devices) == 1
    assert devices[0].online is False


async def test_get_devices_skips_non_iot_entries_without_required_mqtt_fields() -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "device-1",
                        "clientId": "vivosun-VSCTLE42A-account-device-1",
                        "topicPrefix": "vivosun/topic/1",
                        "name": "Custom Tent Name",
                        "scene": {"sceneId": 1001},
                    }
                ],
                "DHMDF": [
                    {
                        "deviceId": "dumb-1",
                        "name": "Dehumidifier",
                    }
                ],
            }
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert len(devices) == 1
    assert devices[0].device_id == "device-1"
    assert devices[0].device_type == "controller"
    assert client.skipped_devices[0]["device_group"] == "DHMDF"
    assert client.skipped_devices[0]["missing_fields"] == ["sceneId", "clientId", "topicPrefix"]


async def test_get_devices_tracks_skipped_missing_scene_entry() -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "device-1",
                        "clientId": "vivosun-VSCTLE42A-account-device-1",
                        "topicPrefix": "vivosun/topic/1",
                        "name": "GrowHub A",
                        "onlineStatus": 1,
                        "scene": None,
                    }
                ]
            }
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert devices == []
    assert client.skipped_devices == [
        {
            "device_group": "GROW",
            "index": 0,
            "available_keys": ["clientId", "deviceId", "name", "onlineStatus", "scene", "topicPrefix"],
            "model_token": "VSCTLE42A",
            "missing_fields": ["sceneId"],
            "raw": {
                "deviceId": "dev...03204de9",
                "clientId": "viv...fecc5306",
                "topicPrefix": "viv...66ce0025",
                "name": "GrowHub A",
                "onlineStatus": 1,
                "scene": None,
            },
        }
    ]


async def test_get_devices_extracts_camera_lan_credentials() -> None:
    """get_devices() should parse camera LAN credentials from setting.jf."""
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "camera-1",
                        "clientId": "",
                        "topicPrefix": "",
                        "name": "GrowCam C4",
                        "onlineStatus": 1,
                        "scene": {"sceneId": 1001},
                        "setting": {"jf": {"devUser": "abjd", "devPass": "4kt5em"}},
                    },
                ]
            }
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert len(devices) == 1
    assert devices[0].device_type == "camera"
    assert devices[0].camera_username == "abjd"
    assert devices[0].camera_password == "4kt5em"


async def test_get_devices_detects_camera_even_when_renamed() -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "camera-1",
                        "clientId": "",
                        "topicPrefix": "",
                        "name": "Cam",
                        "onlineStatus": 1,
                        "scene": {"sceneId": 1001},
                        "setting": {"jf": {"devUser": "abjd", "devPass": "4kt5em"}},
                    },
                ]
            }
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    devices = await client.get_devices(_valid_tokens())

    assert len(devices) == 1
    assert devices[0].device_type == "camera"
    assert devices[0].camera_username == "abjd"
    assert devices[0].camera_password == "4kt5em"


async def test_get_point_log_returns_latest_sensor_snapshot() -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "iotDataLogList": [
                {"inTemp": 1900, "time": 100},
                {
                    "inTemp": 2004,
                    "inHumi": 5508,
                    "inVpd": 105,
                    "outTemp": 1985,
                    "outHumi": 5449,
                    "outVpd": 105,
                    "coreTemp": 3839,
                    "rssi": -35,
                    "waterT": -6666,
                    "time": 200,
                },
            ]
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    from custom_components.vivosun_growhub.models import DeviceInfo

    snapshot = await client.get_point_log(
        _valid_tokens(),
        DeviceInfo(
            device_id="device-1",
            client_id="vivosun-device-1",
            topic_prefix="topic/1",
            name="GrowHub A",
            online=True,
            scene_id=66078,
        ),
        start_time=100,
        end_time=200,
    )

    assert snapshot == {
        "inTemp": 2004,
        "inHumi": 5508,
        "inVpd": 105,
        "outTemp": 1985,
        "outHumi": 5449,
        "outVpd": 105,
        "bTemp": None,
        "bHumi": None,
        "bVpd": None,
        "pTemp": None,
        "pHumi": None,
        "pVpd": None,
        "waterLv": None,
        "waterT": None,
        "coreTemp": 3839,
        "rssi": -35,
    }


@pytest.mark.parametrize("capture_active", [False, True])
async def test_get_point_log_support_capture_keeps_telemetry_without_changing_sensor_parsing(
    capture_active: bool,
) -> None:
    from custom_components.vivosun_growhub.models import DeviceInfo
    from custom_components.vivosun_growhub.support_capture import SupportCaptureManager

    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "iotDataLogList": [{"inTemp": 1000, "oldOnly": 9876}, {
                "inTemp": 2250,
                "usb1.mode": 2,
                "usb1.lv": 40,
                "usb1.inPlan": 0,
                "waterLv": 7500,
                "futureSensor": 42.5,
                "probe": {"wet": True, "reading": None, "userId": 91234567},
                "deviceId": "raw-device-id",
                "sceneId": 66078,
                "mode": "automatic",
                "futureStringReading": "42.5",
                "accessToken": "must-not-appear",
            }],
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)
    capture = SupportCaptureManager()
    client.set_support_capture(capture)
    device = DeviceInfo(
        device_id="dwc-1",
        client_id="vivosun-VSCTLY18-account-device-1",
        topic_prefix="topic/1",
        name="VGrow",
        online=True,
        scene_id=66078,
    )
    if capture_active:
        capture.start(max_events=10, devices=[], subscription_topics=[])

    snapshot = await client.get_point_log(_valid_tokens(), device, start_time=100, end_time=200)

    assert snapshot["inTemp"] == 2250
    assert snapshot["waterLv"] == 7500
    assert "usb1.mode" not in snapshot
    assert "accessToken" not in snapshot
    assert "futureSensor" not in snapshot
    events = capture.snapshot()["events"]
    if not capture_active:
        assert events == []
        assert capture.snapshot()["rest_telemetry_results"] == []
        assert capture.snapshot()["rest_samples"] == []
        return
    assert len(events) == 1
    assert events[0]["kind"] == "rest_point_log"
    assert events[0]["data"]["model_token"] == "VSCTLY18"
    telemetry = events[0]["data"]["telemetry"]
    assert telemetry["usb1.mode"] == 2
    assert telemetry["usb1.lv"] == 40
    assert telemetry["usb1.inPlan"] == 0
    assert telemetry["waterLv"] == 7500
    assert telemetry["futureSensor"] == 42.5
    assert telemetry["probe"] == {"wet": True, "reading": None, "userId": "***"}
    assert telemetry["sceneId"] == 66078
    assert telemetry["deviceId"] == "raw-device-id"
    assert telemetry["mode"] == "automatic"
    assert telemetry["futureStringReading"] == "42.5"
    assert "oldOnly" not in telemetry
    assert events[0]["data"]["telemetry_truncated"] is False
    assert capture.snapshot()["rest_telemetry_results"][0]["telemetry"] == telemetry
    assert "must-not-appear" not in str(capture.snapshot())
    assert capture.snapshot()["rest_telemetry_results"][0]["device_id"] == device.device_id
    samples = capture.snapshot()["rest_samples"][0]["samples"]
    assert [row["telemetry"]["inTemp"] for row in samples] == [1000, 2250]
    assert samples[0]["telemetry"]["oldOnly"] == 9876
    assert "must-not-appear" not in str(samples)


async def test_get_point_log_capture_records_empty_response_without_stale_telemetry() -> None:
    from custom_components.vivosun_growhub.models import DeviceInfo
    from custom_components.vivosun_growhub.support_capture import SupportCaptureManager

    payload = {"success": True, "message": "success", "data": {"iotDataLogList": []}}
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)
    capture = SupportCaptureManager()
    capture.start(max_events=10, devices=[], subscription_topics=[])
    client.set_support_capture(capture)
    device = DeviceInfo("device-1", "vivosun-VSCTLE42A-account-device", "topic/1", "GrowHub", True, 66078)
    capture.record_rest_telemetry_result(device.device_id, {"status": "captured", "telemetry": {"inTemp": 2200}})

    assert await client.get_point_log(_valid_tokens(), device, start_time=100, end_time=200) == {}

    result = capture.snapshot()["rest_telemetry_results"][0]
    assert result["status"] == "empty"
    assert result["model_token"] == "VSCTLE42A"
    assert "telemetry" not in result


async def test_get_point_log_returns_e42a_plus_sensor_keys() -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "iotDataLogList": [
                {
                    "bTemp": 2100,
                    "bHumi": 6100,
                    "bVpd": 123,
                    "pTemp": 1900,
                    "pHumi": 5500,
                    "pVpd": 105,
                    "time": 200,
                },
            ]
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    from custom_components.vivosun_growhub.models import DeviceInfo

    snapshot = await client.get_point_log(
        _valid_tokens(),
        DeviceInfo(
            device_id="device-1",
            client_id="vivosun-device-1",
            topic_prefix="topic/1",
            name="GrowHub A",
            online=True,
            scene_id=66078,
        ),
        start_time=100,
        end_time=200,
    )

    assert snapshot["bTemp"] == 2100
    assert snapshot["bHumi"] == 6100
    assert snapshot["bVpd"] == 123
    assert snapshot["pTemp"] == 1900
    assert snapshot["pHumi"] == 5500
    assert snapshot["pVpd"] == 105
async def test_get_point_log_empty_list_returns_empty_snapshot() -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {"iotDataLogList": []},
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)
    from custom_components.vivosun_growhub.models import DeviceInfo

    snapshot = await client.get_point_log(
        _valid_tokens(),
        DeviceInfo(
            device_id="device-1",
            client_id="vivosun-device-1",
            topic_prefix="topic/1",
            name="GrowHub A",
            online=True,
            scene_id=66078,
        ),
        start_time=100,
        end_time=200,
    )

    assert snapshot == {}


async def test_get_aws_identity_returns_typed_payload(caplog: pytest.LogCaptureFixture) -> None:
    """get_aws_identity() should parse required AWS identity keys."""
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "awsHost": "example.iot.us-east-2.amazonaws.com",
            "awsRegion": "us-east-2",
            "awsIdentityId": "us-east-2:abcd",
            "awsOpenIdToken": "aws-open-id-token",
            "awsPort": 443,
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    caplog.set_level("INFO")
    aws_identity = await client.get_aws_identity(_valid_tokens())

    assert aws_identity.aws_host == "example.iot.us-east-2.amazonaws.com"
    assert aws_identity.aws_region == "us-east-2"
    assert aws_identity.aws_identity_id == "us-east-2:abcd"
    assert aws_identity.aws_open_id_token == "aws-open-id-token"
    assert aws_identity.aws_port == 443
    assert "aws-open-id-token" not in caplog.text


async def test_debug_logs_redact_identifiers_and_tokens(caplog: pytest.LogCaptureFixture) -> None:
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "accessToken": "access-123",
            "loginToken": "login-123",
            "refreshToken": "refresh-123",
            "userId": "153685567990966911",
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    caplog.set_level("DEBUG")
    await client.login("user@example.com", "secret")

    assert "access-123" not in caplog.text
    assert "login-123" not in caplog.text
    assert "refresh-123" not in caplog.text
    assert "153685567990966911" not in caplog.text


async def test_network_error_maps_to_connection_error() -> None:
    """Transport failures should map to VivosunConnectionError."""
    session = cast(
        "aiohttp.ClientSession",
        _MockSession(request_error=aiohttp.ClientError("network down")),
    )
    client = VivosunApiClient(session)

    with pytest.raises(VivosunConnectionError):
        await client.login("user@example.com", "secret")


async def test_http_401_maps_to_auth_error() -> None:
    """HTTP 401 should map to VivosunAuthError."""
    payload = {"code": 1, "success": False, "message": "Unauthorized", "data": {}}
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=401, payload=payload)]))
    client = VivosunApiClient(session)

    with pytest.raises(VivosunAuthError):
        await client.get_devices(_valid_tokens())


async def test_http_401_malformed_payload_maps_to_auth_error() -> None:
    """HTTP 401 should remain auth-classified even with malformed payload shape."""
    payload = {"unexpected": True}
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=401, payload=payload)]))
    client = VivosunApiClient(session)

    with pytest.raises(VivosunAuthError):
        await client.get_devices(_valid_tokens())


async def test_malformed_payload_raises_response_error() -> None:
    """Missing required keys should map to VivosunResponseError."""
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "awsHost": "example.iot.us-east-2.amazonaws.com",
            "awsRegion": "us-east-2",
            "awsIdentityId": "us-east-2:abcd",
            "awsPort": 443,
        },
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = VivosunApiClient(session)

    with pytest.raises(VivosunResponseError):
        await client.get_aws_identity(_valid_tokens())

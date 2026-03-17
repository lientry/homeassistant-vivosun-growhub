"""Unit tests for WP-2 REST bootstrap API client."""

from __future__ import annotations

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
    """get_devices() should parse deviceGroup.GROW and map onlineStatus to bool."""
    payload = {
        "code": 0,
        "success": True,
        "message": "success",
        "data": {
            "deviceGroup": {
                "GROW": [
                    {
                        "deviceId": "device-1",
                        "clientId": "vivosun-device-1",
                        "topicPrefix": "vivosun/topic/1",
                        "name": "GrowHub A",
                        "onlineStatus": 1,
                        "scene": {"sceneId": 1001},
                    },
                    {
                        "deviceId": "device-2",
                        "clientId": "vivosun-device-2",
                        "topicPrefix": "vivosun/topic/2",
                        "name": "GrowHub B",
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
    assert devices[1].device_id == "device-2"
    assert devices[1].online is False
    assert devices[1].scene_id == 1002

    headers = cast("dict[str, str]", mock_session.calls[0]["kwargs"]["headers"])
    assert headers["login-token"] == "login-token-value"
    assert headers["access-token"] == "access-token-value"


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
                        "clientId": "vivosun-device-1",
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
                        "clientId": "vivosun-device-1",
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
        "pTemp": None,
        "pHumi": None,
        "pVpd": None,
        "waterLv": None,
        "coreTemp": 3839,
        "rssi": -35,
    }


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

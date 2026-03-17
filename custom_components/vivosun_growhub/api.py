"""Vivosun REST API bootstrap client."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

import aiohttp

from .const import (
    API_AWS_IDENTITY_PATH,
    API_BASE_URL,
    API_DEVICE_LIST_PATH,
    API_LOGIN_PATH,
    API_POINT_LOG_PATH,
    API_REQUEST_TIMEOUT_SECONDS,
    SENSOR_KEY_CORE_TEMP,
    SENSOR_KEY_INSIDE_HUMI,
    SENSOR_KEY_INSIDE_TEMP,
    SENSOR_KEY_INSIDE_VPD,
    SENSOR_KEY_OUTSIDE_HUMI,
    SENSOR_KEY_OUTSIDE_TEMP,
    SENSOR_KEY_OUTSIDE_VPD,
    SENSOR_KEY_PROBE_HUMI,
    SENSOR_KEY_PROBE_TEMP,
    SENSOR_KEY_PROBE_VPD,
    SENSOR_KEY_RSSI,
    SENSOR_KEY_WATER_LEVEL,
    SENSOR_UNAVAILABLE_SENTINEL,
)
from .exceptions import VivosunAuthError, VivosunConnectionError, VivosunResponseError
from .models import AuthTokens, AwsIdentity, DeviceInfo, infer_device_type
from .redaction import redact_identifier, sanitize_mapping_for_debug

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_LOGGER = logging.getLogger(__name__)
_AUTH_MESSAGE_MARKERS = ("auth", "credential", "forbidden", "invalid", "login", "password", "token", "unauthorized")
_SP_APP_ID = "com.vivosun.android"


class VivosunApiClient:
    """aiohttp-based REST API client for Vivosun cloud bootstrap calls."""

    def __init__(self, session: aiohttp.ClientSession, *, base_url: str = API_BASE_URL) -> None:
        """Initialize API client with shared aiohttp session."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT_SECONDS)

    async def login(self, email: str, password: str) -> AuthTokens:
        """Authenticate with Vivosun and return account tokens."""
        _LOGGER.info("Logging in to Vivosun API")
        payload: dict[str, str] = {
            "email": email,
            "password": password,
            "spAppId": _SP_APP_ID,
            "spClientId": str(uuid4()),
            "spSessionId": str(uuid4()),
        }
        data = await self._request_json("POST", API_LOGIN_PATH, json_body=payload)

        tokens = AuthTokens(
            access_token=self._expect_str(data, "accessToken"),
            login_token=self._expect_str(data, "loginToken"),
            refresh_token=self._expect_str(data, "refreshToken"),
            user_id=self._expect_str(data, "userId"),
        )
        _LOGGER.debug("Login succeeded for user_id=%s", redact_identifier(tokens.user_id))
        return tokens

    async def get_devices(self, tokens: AuthTokens) -> list[DeviceInfo]:
        """Fetch account devices from getTotalList (all categories)."""
        _LOGGER.info("Fetching Vivosun devices")
        data = await self._request_json("GET", API_DEVICE_LIST_PATH, headers=self._auth_headers(tokens))
        device_group = self._expect_mapping(data, "deviceGroup")

        devices: list[DeviceInfo] = []
        for category_key, category_devices in device_group.items():
            if not isinstance(category_devices, list):
                continue
            for index, item in enumerate(category_devices):
                device = self._expect_mapping_item(item, f"deviceGroup.{category_key}[{index}]")
                name = self._expect_str(device, "name")
                client_id = self._expect_str(device, "clientId")
                device_info = DeviceInfo(
                    device_id=self._expect_str(device, "deviceId"),
                    client_id=client_id,
                    topic_prefix=self._expect_str(device, "topicPrefix"),
                    name=name,
                    online=self._optional_int(device, "onlineStatus", default=0) == 1,
                    scene_id=self._expect_scene_id(device),
                    device_type=infer_device_type(name, client_id),
                )
                devices.append(device_info)

        _LOGGER.debug("Fetched %d devices", len(devices))
        return devices

    async def get_aws_identity(self, tokens: AuthTokens, aws_identity_id: str = "") -> AwsIdentity:
        """Fetch AWS identity payload used for Cognito exchange in later phases."""
        _LOGGER.info("Fetching Vivosun AWS identity")
        payload = {"awsIdentityId": aws_identity_id, "attachPolicy": True}
        data = await self._request_json(
            "POST",
            API_AWS_IDENTITY_PATH,
            headers=self._auth_headers(tokens),
            json_body=payload,
        )

        aws_identity = AwsIdentity(
            aws_host=self._expect_str(data, "awsHost"),
            aws_region=self._expect_str(data, "awsRegion"),
            aws_identity_id=self._expect_str(data, "awsIdentityId"),
            aws_open_id_token=self._expect_str(data, "awsOpenIdToken"),
            aws_port=self._expect_int(data, "awsPort"),
        )
        _LOGGER.debug(
            "Fetched AWS identity payload: %s",
            sanitize_mapping_for_debug(
                {
                    "awsHost": aws_identity.aws_host,
                    "awsRegion": aws_identity.aws_region,
                    "awsIdentityId": aws_identity.aws_identity_id,
                    "awsOpenIdToken": aws_identity.aws_open_id_token,
                    "awsPort": aws_identity.aws_port,
                }
            ),
        )
        return aws_identity

    async def get_point_log(
        self,
        tokens: AuthTokens,
        device: DeviceInfo,
        *,
        start_time: int,
        end_time: int,
    ) -> dict[str, int | None]:
        """Fetch recent point-log entries and return the latest sensor snapshot."""
        payload = {
            "sceneId": device.scene_id,
            "deviceId": device.device_id,
            "startTime": start_time,
            "endTime": end_time,
            "reportType": 0,
            "orderBy": "asc",
            "timeLevel": "ONE_MINUTE",
        }
        data = await self._request_json(
            "POST",
            API_POINT_LOG_PATH,
            headers=self._auth_headers(tokens),
            json_body=payload,
        )
        entries = self._expect_sequence(data, "iotDataLogList")
        if not entries:
            return {}

        latest = self._expect_mapping_item(entries[-1], "iotDataLogList[-1]")
        snapshot: dict[str, int | None] = {}
        for key in (
            SENSOR_KEY_INSIDE_TEMP,
            SENSOR_KEY_INSIDE_HUMI,
            SENSOR_KEY_INSIDE_VPD,
            SENSOR_KEY_OUTSIDE_TEMP,
            SENSOR_KEY_OUTSIDE_HUMI,
            SENSOR_KEY_OUTSIDE_VPD,
            SENSOR_KEY_PROBE_TEMP,
            SENSOR_KEY_PROBE_HUMI,
            SENSOR_KEY_PROBE_VPD,
            SENSOR_KEY_WATER_LEVEL,
            SENSOR_KEY_CORE_TEMP,
            SENSOR_KEY_RSSI,
        ):
            snapshot[key] = self._optional_sensor_int(latest, key)
        return snapshot

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        """Call endpoint and return validated envelope data payload."""
        url = f"{self._base_url}{path}"
        request_kwargs: dict[str, object] = {"timeout": self._timeout}
        if headers is not None:
            request_kwargs["headers"] = dict(headers)
        if json_body is not None:
            request_kwargs["json"] = dict(json_body)

        try:
            async with self._session.request(method, url, **request_kwargs) as response:
                payload = await self._read_json_payload(response)
                return self._parse_envelope(payload, status=response.status)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VivosunConnectionError(f"Vivosun API request failed for {path}") from err

    async def _read_json_payload(self, response: aiohttp.ClientResponse) -> Mapping[str, object]:
        """Read and validate JSON body as a mapping."""
        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as err:
            if response.status in (401, 403):
                raise VivosunAuthError(f"Authentication failed with HTTP {response.status}") from err
            raise VivosunResponseError("Response body is not valid JSON") from err

        if not isinstance(payload, dict):
            raise VivosunResponseError("Response JSON root must be an object")
        return payload

    def _parse_envelope(self, payload: Mapping[str, object], *, status: int) -> Mapping[str, object]:
        """Parse standard Vivosun response envelope and return data mapping."""
        if status in (401, 403):
            raise VivosunAuthError(f"Authentication failed with HTTP {status}")

        success = self._expect_bool(payload, "success")
        message = self._expect_str(payload, "message")

        if not success:
            if self._is_auth_failure(message):
                raise VivosunAuthError(f"Authentication failed: {message}")
            raise VivosunResponseError(f"Vivosun API error: {message}")

        data = self._expect_mapping(payload, "data")
        return data

    def _auth_headers(self, tokens: AuthTokens) -> dict[str, str]:
        """Build auth headers for authenticated endpoints."""
        return {
            "login-token": tokens.login_token,
            "access-token": tokens.access_token,
        }

    def _is_auth_failure(self, message: str) -> bool:
        """Best-effort auth failure detection from envelope message content."""
        lowered = message.lower()
        return any(marker in lowered for marker in _AUTH_MESSAGE_MARKERS)

    def _expect_mapping(self, payload: Mapping[str, object], key: str) -> Mapping[str, object]:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise VivosunResponseError(f"Expected object at '{key}'")
        return value

    def _expect_mapping_item(self, payload: object, context: str) -> Mapping[str, object]:
        if not isinstance(payload, dict):
            raise VivosunResponseError(f"Expected object at '{context}'")
        return payload

    def _expect_sequence(self, payload: Mapping[str, object], key: str) -> Sequence[object]:
        value = payload.get(key)
        if not isinstance(value, list):
            raise VivosunResponseError(f"Expected array at '{key}'")
        return value

    def _expect_str(self, payload: Mapping[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise VivosunResponseError(f"Expected string at '{key}'")
        return value

    def _expect_int(self, payload: Mapping[str, object], key: str) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise VivosunResponseError(f"Expected integer at '{key}'")
        return value

    def _optional_int(self, payload: Mapping[str, object], key: str, *, default: int) -> int:
        """Extract an int field, returning *default* when the key is absent or null."""
        value = payload.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                pass
        return default

    def _expect_bool(self, payload: Mapping[str, object], key: str) -> bool:
        value = payload.get(key)
        if not isinstance(value, bool):
            raise VivosunResponseError(f"Expected boolean at '{key}'")
        return value

    def _expect_scene_id(self, payload: Mapping[str, object]) -> int:
        scene = payload.get("scene")
        if not isinstance(scene, dict):
            raise VivosunResponseError("Expected object at 'scene'")
        return self._expect_int(scene, "sceneId")

    def _optional_sensor_int(self, payload: Mapping[str, object], key: str) -> int | None:
        value = self._optional_int(payload, key, default=SENSOR_UNAVAILABLE_SENTINEL)
        if value == SENSOR_UNAVAILABLE_SENTINEL:
            return None
        return value

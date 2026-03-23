"""Unit tests for VivosunCoordinator lifecycle orchestration."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

import pytest

from custom_components.vivosun_growhub.aws_auth import AWS_CREDENTIAL_REFRESH_SKEW, AwsCredentials
from custom_components.vivosun_growhub.const import (
    TOPIC_CHANNEL_APP,
    TOPIC_SHADOW_GET,
    TOPIC_SHADOW_GET_ACCEPTED,
    TOPIC_SHADOW_UPDATE,
)
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.exceptions import VivosunAuthError, VivosunResponseError
from custom_components.vivosun_growhub.models import AuthTokens, AwsIdentity, DeviceInfo

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


class _ApiStub:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tokens = AuthTokens(
            access_token="access",
            login_token="login",
            refresh_token="refresh",
            user_id="user-1",
        )
        self.devices = [
            DeviceInfo(
                device_id="device-2",
                client_id="thing-2",
                topic_prefix="topic/2",
                name="Device B",
                online=True,
                scene_id=66079,
                device_type="humidifier",
            ),
            DeviceInfo(
                device_id="device-1",
                client_id="thing-1",
                topic_prefix="topic/1",
                name="Device A",
                online=True,
                scene_id=66078,
                device_type="controller",
            ),
        ]
        self.identity = AwsIdentity(
            aws_host="example.iot.us-east-2.amazonaws.com",
            aws_region="us-east-2",
            aws_identity_id="us-east-2:identity-1",
            aws_open_id_token="openid-token",
            aws_port=443,
        )
        self.point_log: dict[str, int | None] = {}

    async def login(self, _email: str, _password: str) -> AuthTokens:
        self.calls.append("login")
        return self.tokens

    async def get_devices(self, _tokens: AuthTokens) -> list[DeviceInfo]:
        self.calls.append("get_devices")
        return self.devices

    async def get_aws_identity(self, _tokens: AuthTokens) -> AwsIdentity:
        self.calls.append("get_aws_identity")
        return self.identity

    async def get_point_log(
        self,
        _tokens: AuthTokens,
        _device: DeviceInfo,
        *,
        start_time: int,
        end_time: int,
    ) -> dict[str, int | None]:
        _ = (start_time, end_time)
        self.calls.append("get_point_log")
        return dict(self.point_log)


class _AwsAuthStub:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._credentials_responses: list[AwsCredentials] = []
        self.credentials_requested_event = asyncio.Event()

    def queue_credentials(self, credentials: AwsCredentials) -> None:
        self._credentials_responses.append(credentials)

    async def get_credentials_for_identity(self, _identity: AwsIdentity) -> AwsCredentials:
        self.calls.append("get_credentials_for_identity")
        self.credentials_requested_event.set()
        if not self._credentials_responses:
            raise AssertionError("No credentials queued")
        return self._credentials_responses.pop(0)

    def sigv4_sign_mqtt_url(self, *, endpoint: str, region: str, credentials: AwsCredentials) -> str:
        self.calls.append("sigv4_sign_mqtt_url")
        _ = credentials
        return f"wss://{endpoint}/mqtt?region={region}"

    def credentials_need_refresh(self, credentials: AwsCredentials) -> bool:
        now = datetime.now(tz=UTC)
        return credentials.expiration - now <= AWS_CREDENTIAL_REFRESH_SKEW


class _MqttStub:
    instances: ClassVar[list[_MqttStub]] = []
    connect_failures_remaining: ClassVar[int] = 0
    auth_failures_remaining: ClassVar[int] = 0
    instance_created_event: ClassVar[asyncio.Event] = asyncio.Event()

    def __init__(
        self,
        *,
        websocket_url: str,
        thing: str,
        topic_prefix: str,
        client_id: str | None = None,
        **_kwargs: object,
    ) -> None:
        self.websocket_url = websocket_url
        self.thing = thing
        self.topic_prefix = topic_prefix
        self.client_id = client_id
        self._callbacks: list[object] = []
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscribed_topics: list[tuple[str, int]] = []
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        _MqttStub.instances.append(self)
        _MqttStub.instance_created_event.set()

    @property
    def is_connected(self) -> bool:
        return self.connected

    def add_message_callback(self, callback: object) -> None:
        self._callbacks.append(callback)

    async def connect(self) -> None:
        self.connect_calls += 1
        if _MqttStub.auth_failures_remaining > 0:
            _MqttStub.auth_failures_remaining -= 1
            raise VivosunAuthError("expired")
        if _MqttStub.connect_failures_remaining > 0:
            _MqttStub.connect_failures_remaining -= 1
            raise RuntimeError("connect failed")
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    async def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, qos, retain))

    async def subscribe(self, topics: list[tuple[str, int]]) -> None:
        self.subscribed_topics.extend(topics)

    async def emit(self, topic: str, payload: bytes, qos: int = 0) -> None:
        for callback in self._callbacks:
            await callback(topic, payload, qos)


def _credentials(expiration: datetime) -> AwsCredentials:
    return AwsCredentials(
        access_key_id="ASIA",
        secret_access_key="secret",
        session_token="session",
        expiration=expiration,
    )


def _patch_coordinator_deps(monkeypatch: MonkeyPatch, api: _ApiStub, aws_auth: _AwsAuthStub) -> None:
    _MqttStub.instances.clear()
    _MqttStub.connect_failures_remaining = 0
    _MqttStub.auth_failures_remaining = 0
    _MqttStub.instance_created_event = asyncio.Event()
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator.VivosunApiClient", lambda _session: api)
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator.AwsAuthClient", lambda _session: aws_auth)
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator.MQTTClient", _MqttStub)


async def _wait_for_mqtt_instances(expected_count: int, max_wait_seconds: float = 2.0) -> None:
    while len(_MqttStub.instances) < expected_count:
        await asyncio.wait_for(_MqttStub.instance_created_event.wait(), timeout=max_wait_seconds)
        _MqttStub.instance_created_event.clear()


async def _wait_for_credential_requests(
    stub: _AwsAuthStub,
    expected_count: int,
    max_wait_seconds: float = 2.0,
) -> None:
    while stub.calls.count("get_credentials_for_identity") < expected_count:
        await asyncio.wait_for(stub.credentials_requested_event.wait(), timeout=max_wait_seconds)
        stub.credentials_requested_event.clear()


async def _wait_for_mqtt_connected(
    coordinator: VivosunCoordinator,
    max_wait_seconds: float = 3.0,
) -> None:
    while not coordinator.is_mqtt_connected:
        await asyncio.wait_for(_MqttStub.instance_created_event.wait(), timeout=max_wait_seconds)
        _MqttStub.instance_created_event.clear()


async def test_coordinator_bootstrap_order_and_initial_shadow_get(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    # 2 devices → 2 point_log calls
    assert api.calls == ["login", "get_devices", "get_aws_identity", "get_point_log", "get_point_log"]
    assert aws_auth.calls[:2] == ["get_credentials_for_identity", "sigv4_sign_mqtt_url"]
    assert coordinator.device.device_id == "device-1"
    assert coordinator.device.scene_id == 66078
    assert len(_MqttStub.instances) == 1
    mqtt = _MqttStub.instances[0]
    assert mqtt.connected is True
    assert isinstance(mqtt.client_id, str)
    assert mqtt.client_id.startswith("ha-vivosun-")
    assert mqtt.client_id != mqtt.thing
    # Initial shadow get for both devices
    shadow_get_publishes = [p for p in mqtt.published if "/shadow/get" in p[0] and not p[0].endswith("/accepted")]
    assert len(shadow_get_publishes) == 2

    await coordinator.async_shutdown()


async def test_coordinator_mqtt_callbacks_update_shadow_and_sensor_state(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    api.point_log = {"inHumi": 5500}
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    mqtt = _MqttStub.instances[0]
    device_id = coordinator.device.device_id
    shadow_payload = {
        "state": {
            "reported": {
                "light": {"mode": 0, "lv": 42, "manu": {"lv": 42, "spec": 20}, "inPlan": 0},
                "connected": 1,
            }
        }
    }
    await mqtt.emit(
        TOPIC_SHADOW_GET_ACCEPTED.format(thing=coordinator.device.client_id),
        json.dumps(shadow_payload).encode("utf-8"),
    )
    await mqtt.emit(
        TOPIC_CHANNEL_APP.format(topic_prefix=coordinator.device.topic_prefix),
        b'{"inTemp":2300,"outTemp":1900}',
    )
    coordinator._sensor_states.setdefault(device_id, {}).update({"inHumi": 5500})

    shadows = coordinator.data["shadows"]
    sensors = coordinator.data["sensors"]
    device_shadow = shadows[device_id]
    device_sensors = sensors[device_id]
    assert isinstance(device_shadow, dict)
    assert isinstance(device_sensors, dict)
    assert device_shadow["light"]["level"] == 42
    assert device_shadow["connection"]["connected"] is True
    assert device_sensors["inTemp"] == 2300
    assert device_sensors["outTemp"] == 1900
    assert device_sensors["inHumi"] == 5500

    await coordinator.async_shutdown()


async def test_coordinator_channel_payload_recovers_stale_connection_state(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    mqtt = _MqttStub.instances[0]
    device_id = coordinator.device.device_id
    await mqtt.emit(
        TOPIC_SHADOW_GET_ACCEPTED.format(thing=coordinator.device.client_id),
        b'{"state":{"reported":{"connected":0}}}',
    )
    assert coordinator.data["shadows"][device_id]["connection"]["connected"] is False

    await mqtt.emit(
        TOPIC_CHANNEL_APP.format(topic_prefix=coordinator.device.topic_prefix),
        b'{"inTemp":2300}',
    )

    assert coordinator.data["shadows"][device_id]["connection"]["connected"] is True
    await coordinator.async_shutdown()


async def test_coordinator_refresh_requests_shadow_sync_when_connection_state_is_stale(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.vivosun_growhub.coordinator._SHADOW_REFRESH_INTERVAL_SECONDS",
        0.0,
    )
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    mqtt = _MqttStub.instances[0]
    shadow_get_publishes = [p for p in mqtt.published if p[0].endswith("/shadow/get")]
    assert len(shadow_get_publishes) == 2

    await mqtt.emit(
        TOPIC_SHADOW_GET_ACCEPTED.format(thing=coordinator.device.client_id),
        b'{"state":{"reported":{"connected":0}}}',
    )
    await coordinator._async_update_data()

    refreshed_shadow_gets = [p for p in mqtt.published if p[0] == TOPIC_SHADOW_GET.format(thing="thing-1")]
    assert len(refreshed_shadow_gets) == 2
    await coordinator.async_shutdown()


async def test_coordinator_refresh_loop_triggers_credentials_refresh_and_reconnect(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    now = datetime.now(tz=UTC)
    aws_auth.queue_credentials(_credentials(now + timedelta(seconds=1)))
    aws_auth.queue_credentials(_credentials(now + timedelta(hours=2)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    await _wait_for_credential_requests(aws_auth, expected_count=2)
    await _wait_for_mqtt_instances(expected_count=2)

    assert len(_MqttStub.instances) >= 2
    assert aws_auth.calls.count("get_credentials_for_identity") >= 2

    await coordinator.async_shutdown()


async def test_coordinator_reconnect_supervisor_retries_after_failures(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._RECONNECT_BACKOFF_INITIAL", 0.05)
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._RECONNECT_BACKOFF_MAX", 0.1)
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._RECONNECT_HEALTH_CHECK_SECONDS", 0.05)

    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    try:
        _MqttStub.connect_failures_remaining = 2
        mqtt = _MqttStub.instances[0]
        mqtt.connected = False
        coordinator._reconnect_event.set()
        await _wait_for_mqtt_instances(expected_count=3, max_wait_seconds=3.0)
        await _wait_for_mqtt_connected(coordinator, max_wait_seconds=3.0)

        assert len(_MqttStub.instances) >= 3
        assert coordinator.is_mqtt_connected is True
    finally:
        await coordinator.async_shutdown()


async def test_coordinator_shutdown_cancels_tasks_and_disconnects_mqtt(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    mqtt = _MqttStub.instances[0]
    assert coordinator.is_mqtt_connected is True
    await coordinator.async_shutdown()

    assert mqtt.disconnect_calls == 1
    assert coordinator.is_mqtt_connected is False
    assert coordinator._refresh_task is None
    assert coordinator._reconnect_task is None


async def test_coordinator_publish_shadow_update_encodes_payload_variants(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    mqtt = _MqttStub.instances[0]
    initial_count = len(mqtt.published)

    await coordinator.async_publish_shadow_update({"state": {"desired": {"light": {"manu": {"lv": 60}}}}})
    await coordinator.async_publish_shadow_update('{"state":{"desired":{"light":{"manu":{"lv":10}}}}}')
    await coordinator.async_publish_shadow_update(b'{"state":{"desired":{"light":{"manu":{"lv":5}}}}}')

    shadow_update_topic = TOPIC_SHADOW_UPDATE.format(thing=coordinator.device.client_id)
    update_publishes = mqtt.published[initial_count:]
    assert update_publishes == [
        (shadow_update_topic, b'{"state":{"desired":{"light":{"manu":{"lv":60}}}}}', 0, False),
        (shadow_update_topic, b'{"state":{"desired":{"light":{"manu":{"lv":10}}}}}', 0, False),
        (shadow_update_topic, b'{"state":{"desired":{"light":{"manu":{"lv":5}}}}}', 0, False),
    ]

    await coordinator.async_shutdown()


async def test_coordinator_ignores_malformed_and_non_object_shadow_payloads(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    mqtt = _MqttStub.instances[0]
    initial_data = coordinator.data

    await mqtt.emit(TOPIC_SHADOW_GET_ACCEPTED.format(thing=coordinator.device.client_id), b'{"state":')
    await mqtt.emit(TOPIC_SHADOW_GET_ACCEPTED.format(thing=coordinator.device.client_id), b'["not","object"]')

    assert coordinator.data == initial_data
    await coordinator.async_shutdown()


async def test_coordinator_reconnect_auth_failure_performs_full_relogin(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._RECONNECT_BACKOFF_INITIAL", 0.05)
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._RECONNECT_BACKOFF_MAX", 0.1)
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._RECONNECT_HEALTH_CHECK_SECONDS", 0.05)

    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=2)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    try:
        _MqttStub.auth_failures_remaining = 1
        _MqttStub.instances[0].connected = False
        coordinator._reconnect_event.set()
        await _wait_for_credential_requests(aws_auth, expected_count=2, max_wait_seconds=3.0)
        await _wait_for_mqtt_instances(expected_count=3, max_wait_seconds=3.0)
        await _wait_for_mqtt_connected(coordinator, max_wait_seconds=3.0)

        assert api.calls.count("login") >= 2
        assert api.calls.count("get_aws_identity") >= 2
        assert coordinator.is_mqtt_connected is True
    finally:
        await coordinator.async_shutdown()


async def test_coordinator_start_idempotent_and_select_device_requires_entries(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    await coordinator.async_start()
    assert len(_MqttStub.instances) == 1

    assert await coordinator._async_update_data() == coordinator.data
    await coordinator.async_shutdown()

    empty_api = _ApiStub()
    empty_api.devices = []
    empty_aws_auth = _AwsAuthStub()
    empty_aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, empty_api, empty_aws_auth)
    empty_coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    with pytest.raises(VivosunResponseError, match="No devices found"):
        await empty_coordinator.async_start()

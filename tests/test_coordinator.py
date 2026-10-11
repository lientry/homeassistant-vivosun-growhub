"""Unit tests for VivosunCoordinator lifecycle orchestration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.aws_auth import AWS_CREDENTIAL_REFRESH_SKEW, AwsCredentials
from custom_components.vivosun_growhub.camera import VivosunGrowCamEntity
from custom_components.vivosun_growhub.camera import async_setup_entry as async_setup_camera
from custom_components.vivosun_growhub.const import (
    CONF_CAMERA_IPS,
    DOMAIN,
    TOPIC_CHANNEL_APP,
    TOPIC_SHADOW_GET,
    TOPIC_SHADOW_GET_ACCEPTED,
    TOPIC_SHADOW_UPDATE,
    TOPIC_SHADOW_UPDATE_ACCEPTED,
)
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.diagnostics import async_get_config_entry_diagnostics
from custom_components.vivosun_growhub.exceptions import VivosunAuthError, VivosunResponseError
from custom_components.vivosun_growhub.models import AuthTokens, AwsIdentity, CaptureDevice, DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.mqtt_client import MQTTConnectionError
from custom_components.vivosun_growhub.redaction import redact_topic

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


class _ApiStub:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.plan_stage_info_calls: list[str] = []
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
        self.capture_devices_override: list[CaptureDevice] | None = None

    @property
    def capture_devices(self) -> list[CaptureDevice]:
        if self.capture_devices_override is not None:
            return self.capture_devices_override
        return [
            CaptureDevice(d.client_id, d.device_id, d.topic_prefix, d.name, d.device_type)
            for d in self.devices
            if d.client_id and d.device_type != "camera"
        ]

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

    async def get_plan_stage_info(self, _tokens: AuthTokens, stage_id: str) -> object:
        self.plan_stage_info_calls.append(stage_id)
        from custom_components.vivosun_growhub.models import PlanStageInfo

        return PlanStageInfo(stage_name="Seedling", icon="", content={"light": {"slot": []}})


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
        if self.client_id and self.client_id.startswith("ha-vivosun-probe-") and topic.endswith("/shadow/get"):
            request = json.loads(payload)
            await self.emit(
                topic + "/accepted",
                json.dumps(
                    {
                        "clientToken": request["clientToken"],
                        "state": {"reported": {"mode": 2}},
                    }
                ).encode(),
            )

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


async def test_coordinator_skips_point_log_for_no_scene_devices(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    api.devices = [
        DeviceInfo(
            device_id="vcure-1",
            client_id="thing-vcure",
            topic_prefix="topic/vcure",
            name="VCure C80",
            online=True,
            scene_id=0,
            device_type="curing_box",
            supports_point_log=False,
        )
    ]
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    assert api.calls == ["login", "get_devices", "get_aws_identity"]

    await coordinator.async_shutdown()


async def test_camera_only_account_loads_and_restarts_without_mqtt(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    # Public issue #39: a GrowCam-only account must run LAN RTSP without AWS IoT.
    api = _ApiStub()
    api.devices = [
        DeviceInfo(
            device_id="cam-1",
            client_id="",
            topic_prefix="",
            name="GrowCam C4",
            online=True,
            scene_id=0,
            device_type="camera",
            camera_username="cam-user",
            camera_password="cam-password",
        )
    ]
    aws_auth = _AwsAuthStub()
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    entry = MockConfigEntry(
        domain=DOMAIN, title="GrowCam", data={},
        options={CONF_CAMERA_IPS: {"cam-1": "192.168.2.45"}},
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id, coordinator=coordinator
    )

    for _ in range(2):
        await coordinator.async_start()
        await coordinator.async_refresh()
        assert api.calls[-2:] == ["login", "get_devices"]
        assert aws_auth.calls == []
        assert _MqttStub.instances == []
        assert coordinator.devices == []
        assert coordinator.is_mqtt_connected is False
        assert coordinator._refresh_task is None
        assert coordinator._reconnect_task is None

        cameras: list[VivosunGrowCamEntity] = []
        await async_setup_camera(hass, entry, cameras.extend)
        assert len(cameras) == 1
        assert await cameras[0].stream_source() == (
            "rtsp://cam-user:cam-password@192.168.2.45:554/"
        )
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["device"] is None
        assert diagnostics["camera_configuration"]["configured_count"] == 1

        await coordinator.async_shutdown()
        assert coordinator.camera_devices == []


async def test_coordinator_rejects_account_with_no_supported_devices(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    api.devices = []
    aws_auth = _AwsAuthStub()
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")

    with pytest.raises(VivosunResponseError, match="No devices found"):
        await coordinator.async_start()
    await coordinator.async_shutdown()

    assert aws_auth.calls == []
    assert _MqttStub.instances == []


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


async def test_mqtt_updates_keep_rest_poll_scheduled(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    """Repeated fan updates must not postpone the 90-second REST refresh."""
    api = _ApiStub()
    api.point_log = {"inTemp": 2150}
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    notifications: list[object] = []
    unsubscribe = coordinator.async_add_listener(lambda: notifications.append(coordinator.data))
    refresh_handle = coordinator._unsub_refresh.__self__
    assert refresh_handle is not None

    mqtt = _MqttStub.instances[0]
    device = coordinator.device
    for level in (25, 50, 75):
        await mqtt.emit(
            TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=device.client_id),
            json.dumps({"state": {"reported": {"dFan": {"lv": level}}}}).encode(),
        )
        assert coordinator.data["shadows"][device.device_id]["dFan"]["level"] == level
        assert coordinator._unsub_refresh.__self__ is refresh_handle
        assert not refresh_handle.cancelled()

    assert len(notifications) == 3
    prior_polls = api.calls.count("get_point_log")
    await coordinator.async_refresh()
    assert api.calls.count("get_point_log") == prior_polls + 2

    unsubscribe()
    await coordinator.async_shutdown()


async def test_e42a_auto_mode_survives_level_only_accepted_update(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    # Issue #40: E42A reports mode=2 for Auto, then sends partial level updates.
    api = _ApiStub()
    api.devices[1] = replace(api.devices[1], client_id="vivosun-VSCTLE42A-acc-device-1")
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    mqtt = _MqttStub.instances[0]
    device = coordinator.device

    await mqtt.emit(
        TOPIC_SHADOW_GET_ACCEPTED.format(thing=device.client_id),
        b'{"state":{"reported":{"dFan":{"mode":2,"lv":0,"auto":{"tMax":2797}}}}}',
    )
    await mqtt.emit(
        TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=device.client_id),
        b'{"state":{"reported":{"dFan":{"lv":20}}}}',
    )
    dfan = coordinator.data["shadows"][device.device_id]["dFan"]
    assert dfan["mode"] == 2
    assert dfan["auto_enabled"] is True
    assert dfan["level"] == 20
    assert dfan["auto"]["tMax"] == 2797

    await coordinator.async_shutdown()


async def test_coordinator_accepts_desired_only_shadow_payloads(
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
        b'{"state":{"desired":{"hmdf":{"on":1,"targetHumi":5500}}}}',
    )

    assert coordinator.data["shadows"][device_id]["hmdf"]["on"] is True
    assert coordinator.data["shadows"][device_id]["hmdf"]["target_humidity"] == 5500

    await coordinator.async_shutdown()


async def test_coordinator_mqtt_shadow_update_refreshes_plan_stage_info_immediately(
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
    shadow_payload = {
        "state": {
            "reported": {
                "plan": {
                    "stage1": {"startT": 1700000000, "contId": "abc+seedling"},
                    "stage2": {"startT": 0, "contId": "def+veg"},
                },
                "connected": 1,
            }
        }
    }
    await mqtt.emit(
        TOPIC_SHADOW_GET_ACCEPTED.format(thing=coordinator.device.client_id),
        json.dumps(shadow_payload).encode("utf-8"),
    )

    assert api.plan_stage_info_calls == ["abc", "def"]
    await coordinator.async_shutdown()


async def test_coordinator_mqtt_shadow_update_refreshes_active_stage_even_if_cached(
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
    shadow_payload = {
        "state": {
            "reported": {
                "plan": {
                    "stage1": {"startT": 1700000000, "contId": "abc+seedling"},
                },
                "connected": 1,
            }
        }
    }
    from custom_components.vivosun_growhub.models import PlanStageInfo
    coordinator._plan_stage_cache["abc"] = PlanStageInfo(stage_name="Old", icon="", content={})
    await mqtt.emit(
        TOPIC_SHADOW_GET_ACCEPTED.format(thing=coordinator.device.client_id),
        json.dumps(shadow_payload).encode("utf-8"),
    )

    assert api.plan_stage_info_calls == ["abc"]
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


async def test_support_capture_defers_topic_subscription_if_mqtt_drops(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    original_subscribe = _MqttStub.subscribe

    async def _drop_probe_subscribe(self: _MqttStub, topics: list[tuple[str, int]]) -> None:
        if isinstance(self.client_id, str) and self.client_id.startswith("ha-vivosun-probe-"):
            self.connected = False
            raise MQTTConnectionError("MQTT client disconnected")
        await original_subscribe(self, topics)

    monkeypatch.setattr(_MqttStub, "subscribe", _drop_probe_subscribe)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    await coordinator.async_start_support_capture(max_events=100)

    await coordinator._support_capture_task
    snapshot = coordinator.support_capture_snapshot()
    events = cast("list[dict[str, object]]", snapshot["events"])
    subscription_results = cast("list[dict[str, object]]", snapshot["subscription_results"])
    assert snapshot["active"] is True
    assert coordinator.is_mqtt_connected is True
    assert any(event["kind"] == "support_topics_subscription_deferred" for event in events)
    assert subscription_results
    assert all(result["status"] == "deferred" for result in subscription_results)
    assert all(result["reason"] == "probe_disconnected" for result in subscription_results)
    assert any(
        event["kind"] == "support_topics_subscription_deferred"
        and event["data"]["primary_mqtt_connected"] is True
        and event["data"]["reason"] == "probe_disconnected"
        for event in events
    )

    await coordinator.async_shutdown()


async def test_capture_initialization_does_not_block_setup_or_shutdown(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    waiting = asyncio.Event()
    original_subscribe = _MqttStub.subscribe

    async def slow_subscribe(self: _MqttStub, topics: list[tuple[str, int]]) -> None:
        if self.client_id and self.client_id.startswith("ha-vivosun-probe-"):
            waiting.set()
            await asyncio.sleep(60)
        await original_subscribe(self, topics)

    monkeypatch.setattr(_MqttStub, "subscribe", slow_subscribe)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    await asyncio.wait_for(coordinator.async_start_support_capture(max_events=100), 1)
    await asyncio.wait_for(waiting.wait(), 1)
    assert coordinator.support_capture_active
    assert coordinator.is_mqtt_connected
    assert any(e["kind"] == "capture_initializing" for e in coordinator.support_capture_snapshot()["events"])
    await asyncio.wait_for(coordinator.async_shutdown(), 1)


async def test_support_capture_uses_only_exact_discovered_topics(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    topics = coordinator._support_capture_topic_names()

    assert "$aws/things/thing-1/shadow/get/rejected" in topics
    assert "$aws/things/thing-2/shadow/update/documents" in topics
    assert "topic/1/channel/app" in topics
    assert "topic/2/channel/app" in topics
    assert all("+" not in topic and "#" not in topic for topic in topics)

    await coordinator.async_shutdown()


async def test_capture_records_supported_mqtt_once_and_keeps_probe_only_traffic(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    skipped = CaptureDevice("thing-skipped", "skipped", "topic/skipped", "Unsupported", "unknown", ("sceneId",))
    api.capture_devices_override = [*api.capture_devices, skipped]
    auth = _AwsAuthStub()
    auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    await coordinator.async_start_support_capture(max_events=100)
    await coordinator._support_capture_task
    main = _MqttStub.instances[0]
    probe = coordinator._support_capture_probe_client
    assert probe is not None
    assert coordinator.support_capture_snapshot()["shadow_read_results"][0]["status"] == "accepted"

    topic = "$aws/things/thing-1/shadow/update/delta"
    payload = b'{"state":{"dFan":{"mode":2}}}'
    await probe.emit(topic, payload)
    await main.emit(topic, payload)
    await main.emit(topic, payload)
    # Repeated identical messages from the same source are valid observations.
    matching = [
        e for e in coordinator.support_capture_snapshot()["events"]
        if e["kind"] in ("mqtt_publish", "support_probe_mqtt_publish")
        and e["data"]["topic"] == redact_topic(topic)
    ]
    assert [e["kind"] for e in matching] == ["mqtt_publish", "mqtt_publish"]

    # The primary never subscribes to rejected topics or skipped-device shadows.
    await probe.emit("$aws/things/thing-1/shadow/get/rejected", b'{"code":403}')
    await probe.emit("$aws/things/thing-skipped/shadow/update/delta", payload)
    captured = [
        e for e in coordinator.support_capture_snapshot()["events"]
        if e["kind"] == "support_probe_mqtt_publish"
    ]
    assert {e["data"]["topic"] for e in captured} >= {
        redact_topic("$aws/things/thing-1/shadow/get/rejected"),
        redact_topic("$aws/things/thing-skipped/shadow/update/delta"),
    }

    main.connected = False
    await probe.emit(topic, b'{"state":{"dFan":{"mode":0}}}')
    assert coordinator.support_capture_snapshot()["events"][-1]["kind"] == "support_probe_mqtt_publish"
    assert coordinator.support_capture_snapshot()["events"][-1]["data"]["payload"]["state"]["dFan"]["mode"] == 0
    await coordinator.async_shutdown()


async def test_support_capture_records_accepted_and_rejected_topic_filters(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    original_subscribe = _MqttStub.subscribe

    async def _subscribe_with_single_rejection(self: _MqttStub, topics: list[tuple[str, int]]) -> None:
        if isinstance(self.client_id, str) and self.client_id.startswith("ha-vivosun-probe-"):
            assert len(topics) == 1
            if topics[0][0] == "$aws/things/thing-1/shadow/update/rejected":
                raise MQTTConnectionError("Broker rejected one or more topic subscriptions")
        await original_subscribe(self, topics)

    monkeypatch.setattr(_MqttStub, "subscribe", _subscribe_with_single_rejection)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    await coordinator.async_start_support_capture(max_events=100)

    await coordinator._support_capture_task
    snapshot = coordinator.support_capture_snapshot()
    events = cast("list[dict[str, object]]", snapshot["events"])
    subscription_results = cast("list[dict[str, object]]", snapshot["subscription_results"])
    by_topic = {cast("str", result["topic"]): result for result in subscription_results}

    assert coordinator.is_mqtt_connected is True
    assert by_topic[redact_topic("$aws/things/thing-1/shadow/update/rejected")]["status"] == "rejected"
    assert by_topic[redact_topic("topic/1/channel/app")]["status"] == "accepted"
    assert by_topic[redact_topic("topic/2/channel/app")]["status"] == "accepted"
    assert any(
        event["kind"] == "support_topics_subscribed"
        and cast("dict[str, object]", event["data"])["rejected_topic_count"] == 1
        for event in events
    )

    await coordinator.async_shutdown()


async def test_support_capture_records_model_metadata_results(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    api.devices = [
        DeviceInfo(
            device_id="device-2",
            client_id="vivosun-VSHUMH05-account-device-2",
            topic_prefix="topic/2",
            name="AeroStream H05",
            online=True,
            scene_id=66079,
            device_type="humidifier",
        ),
        DeviceInfo(
            device_id="device-1",
            client_id="vivosun-VSCTLE42AP-account-device-1",
            topic_prefix="topic/1",
            name="GrowHub E42A+",
            online=True,
            scene_id=66078,
            device_type="controller",
        ),
    ]
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    await coordinator.async_start_support_capture(max_events=100)

    snapshot = coordinator.support_capture_snapshot()
    model_metadata_results = cast(
        "list[dict[str, object]]", snapshot["model_metadata_results"]
    )
    by_token = {
        cast("str", result["model_code"]): result for result in model_metadata_results
    }

    assert by_token["VSCTLE42AP"]["matched"] is True
    assert by_token["VSCTLE42AP"]["default_name"] == "GrowHub E42A+"
    assert by_token["VSCTLE42AP"]["channel_param_key"] == {
        "0": {"temp": "bTemp", "humi": "bHumi"},
        "1": {"temp": "pTemp", "humi": "pHumi"},
    }
    assert by_token["VSHUMH05"]["matched"] is True
    assert by_token["VSHUMH05"]["comm_mode_list"] == ["MQTT"]
    assert by_token["VSHUMH05"]["data_upload_groups"] == [
        {
            "group_name": "Probe",
            "group_type": "CLIMATE",
            "shadow_channel": 0,
            "data_keys": ["pTemp", "pHumi", "pVpd", "pCo2"],
        }
    ]
    await coordinator.async_shutdown()


async def test_support_capture_includes_skipped_vseed_discovery_without_raw_details(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    api.skipped_devices = [{
        "device_group": "STORE",
        "model_token": "VSSBS40",
        "available_keys": ["clientId", "deviceId", "scene", "topicPrefix"],
        "missing_fields": ["sceneId"],
        "raw": {"setting": {"jf": {"devPass": "must-not-appear"}}},
    }]
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()

    await coordinator.async_start_support_capture(max_events=100)
    events = coordinator.support_capture_snapshot()["events"]
    discovery = next(event["data"] for event in events if event["kind"] == "skipped_discovery")
    assert discovery["devices"] == [{
        "device_group": "STORE",
        "model_token": "VSSBS40",
        "available_keys": ["clientId", "deviceId", "scene", "topicPrefix"],
        "missing_fields": ["sceneId"],
    }]
    assert "must-not-appear" not in str(coordinator.support_capture_snapshot())

    await coordinator.async_shutdown()


async def test_capture_reads_skipped_device_without_changing_entities(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    seed = CaptureDevice("vivosun-VSSBS40-account-seed", "seed", "vivosun/seed", "VSeed S40", "unknown", ("sceneId",))
    api.capture_devices_override = [*api.capture_devices, seed]
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    initial = coordinator.data
    assert not any(m.client_id and m.client_id.startswith("ha-vivosun-probe-") for m in _MqttStub.instances)

    await coordinator.async_start_support_capture(max_events=1)

    await coordinator._support_capture_task
    snapshot = coordinator.support_capture_snapshot()
    results = snapshot["shadow_read_results"]
    assert len(results) == 3
    assert all(r["status"] == "accepted" for r in results)
    assert next(r for r in results if r["model_token"] == "VSSBS40")["payload"]["state"]["reported"] == {"mode": 2}
    assert len(coordinator.devices) == 2
    assert coordinator.data == initial
    assert coordinator.is_mqtt_connected
    assert coordinator._support_capture_probe_client.is_connected
    seed_topic = f"$aws/things/{seed.client_id}/shadow/update/documents"
    await coordinator._support_capture_probe_client.emit(seed_topic, b'{"current":{"state":{"reported":{"mode":3}}}}')
    events = coordinator.support_capture_snapshot()["events"]
    assert events[-1]["data"]["payload"]["current"]["state"]["reported"]["mode"] == 3
    assert "account-seed" not in str(snapshot)
    await coordinator.async_shutdown()


@pytest.mark.parametrize("reply, expected", [("rejected", "rejected"), ("foreign", "timeout")])
async def test_capture_correlates_shadow_responses_and_records_failed_reads(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
    reply: str,
    expected: str,
) -> None:
    api = _ApiStub()
    api.devices = api.devices[:1]
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._SUPPORT_SHADOW_READ_TIMEOUT", 0.01)
    original_publish = _MqttStub.publish

    async def reply_to_read(self: _MqttStub, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        if self.client_id and self.client_id.startswith("ha-vivosun-probe-") and topic.endswith("/shadow/get"):
            self.published.append((topic, payload, qos, retain))
            token = json.loads(payload)["clientToken"] if reply == "rejected" else "other-client"
            await self.emit(topic + "/rejected", json.dumps({"clientToken": token, "code": 404}).encode())
            return
        await original_publish(self, topic, payload, qos, retain)

    monkeypatch.setattr(_MqttStub, "publish", reply_to_read)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    await coordinator.async_start_support_capture(max_events=10)
    await coordinator._support_capture_task
    result = coordinator.support_capture_snapshot()["shadow_read_results"][0]
    assert result["status"] == expected
    if reply == "rejected":
        assert result["code"] == 404
    assert coordinator.is_mqtt_connected
    await coordinator.async_shutdown()


async def test_skipped_only_account_can_start_metadata_only_then_opt_in_to_mqtt_capture(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    api.devices = []
    api.capture_devices_override = [
        CaptureDevice("vivosun-VSSBS40-account-seed", "seed", "", "VSeed S40", "unknown", ("sceneId", "topicPrefix"))
    ]
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    assert not coordinator.devices
    assert not _MqttStub.instances
    assert "get_aws_identity" not in api.calls

    await coordinator.async_start_support_capture(max_events=10)

    await coordinator._support_capture_task
    assert coordinator.support_capture_snapshot()["shadow_read_results"][0]["status"] == "accepted"
    assert not coordinator.devices
    assert not coordinator.is_mqtt_connected
    await coordinator.async_shutdown()


@pytest.mark.parametrize("failure", ["subscribe", "connect"])
async def test_capture_reports_permission_and_connection_failures_without_breaking_main(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, failure: str,
) -> None:
    api = _ApiStub()
    api.devices = api.devices[:1]
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    if failure == "connect":
        _MqttStub.connect_failures_remaining = 1
    else:
        original_subscribe = _MqttStub.subscribe

        async def reject_read(self: _MqttStub, topics: list[tuple[str, int]]) -> None:
            if topics[0][0].endswith("/shadow/get/accepted"):
                raise MQTTConnectionError("Broker rejected one or more topic subscriptions")
            await original_subscribe(self, topics)

        monkeypatch.setattr(_MqttStub, "subscribe", reject_read)

    await coordinator.async_start_support_capture(max_events=10)

    await coordinator._support_capture_task
    result = coordinator.support_capture_snapshot()["shadow_read_results"][0]
    assert result["status"] == ("deferred" if failure == "connect" else "subscription_rejected")
    if failure == "connect":
        assert result["stage"] == "mqtt_connect"
    assert not any(topic.endswith("/shadow/get") for mqtt in _MqttStub.instances[1:] for topic, *_ in mqtt.published)
    assert coordinator.is_mqtt_connected
    await coordinator.async_shutdown()


async def test_connect_mqtt_defers_active_support_capture_topics_if_subscription_fails(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)

    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    await coordinator.async_start_support_capture(max_events=100)

    async def _fail_subscribe() -> None:
        raise MQTTConnectionError("MQTT client disconnected")

    coordinator._async_subscribe_support_capture_topics = _fail_subscribe  # type: ignore[method-assign]

    await coordinator._connect_mqtt()

    await coordinator._support_capture_task
    snapshot = coordinator.support_capture_snapshot()
    events = cast("list[dict[str, object]]", snapshot["events"])
    assert snapshot["active"] is True
    assert any(event["kind"] == "support_topics_subscription_deferred" for event in events)

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

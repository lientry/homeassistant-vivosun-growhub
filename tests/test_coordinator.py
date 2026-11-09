"""Unit tests for VivosunCoordinator lifecycle orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.vivosun_growhub.api import VivosunApiClient
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
from custom_components.vivosun_growhub.exceptions import (
    VivosunAuthError,
    VivosunConnectionError,
    VivosunResponseError,
)
from custom_components.vivosun_growhub.garden_topology import GardenTopology
from custom_components.vivosun_growhub.gardens import (
    Garden,
    GardenInventory,
    GardenMember,
    GardenPlans,
    parse_garden_plans,
)
from custom_components.vivosun_growhub.models import AuthTokens, AwsIdentity, CaptureDevice, DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.mqtt_client import MQTTConnectionError
from custom_components.vivosun_growhub.redaction import redact_topic
from custom_components.vivosun_growhub.sensor import async_setup_entry as async_setup_sensors
from custom_components.vivosun_growhub.support_capture import SUPPORT_CAPTURE_MAX_STAGE_RESULTS

if TYPE_CHECKING:
    import aiohttp
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.core import HomeAssistant, State
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


def _garden_inventory(*, moved: bool = False, added: bool = False) -> GardenInventory:
    """Three independently listed synthetic gardens, including an empty one."""
    first = [GardenMember("device-1", "thing-1")]
    second = [] if moved else [GardenMember("device-2", "thing-2")]
    if moved:
        first.append(GardenMember("device-2", "thing-2"))
    if added:
        second.append(GardenMember("device-3", "thing-3"))
    return GardenInventory((
        Garden(66078, 600, "First", "Europe/Berlin", tuple(first)),
        Garden(66079, 600, "Second", "Europe/Berlin", tuple(second)),
        Garden(66080, 600, "Empty", "Europe/Berlin", ()),
    ))


class _GardenApiStub(_ApiStub):
    def __init__(self) -> None:
        super().__init__()
        self.gardens = _garden_inventory()
        self.failure: Exception | None = None
        self.plan_failures: set[int] = set()
        self.plan_errors: dict[int, Exception] = {}
        self.plans: dict[int, GardenPlans] = {}

    async def get_gardens(self, _tokens: AuthTokens) -> GardenInventory:
        self.calls.append("get_gardens")
        if self.failure:
            raise self.failure
        return self.gardens

    async def get_garden_plans(self, _tokens: AuthTokens, scene_id: int) -> GardenPlans:
        self.calls.append(f"get_garden_plans:{scene_id}")
        if scene_id in self.plan_errors:
            raise self.plan_errors[scene_id]
        if scene_id in self.plan_failures:
            raise VivosunResponseError("synthetic plan failure")
        return self.plans.get(scene_id, GardenPlans(scene_id, ()))


def _running_garden_recipe(scene_id: int) -> GardenPlans:
    """Synthetic cloud plan with a running stage and light schedule."""
    plan_id = scene_id + 100
    return parse_garden_plans({
        "scene": {"sceneId": scene_id},
        "iotUserPlanList": [{
            "sceneId": scene_id, "userPlanId": plan_id, "userRecipeId": 1,
            "name": "Photoperiod", "needSync": False, "iotDeviceList": [],
            "userPlanStageList": [{
                "userPlanStageId": scene_id + 200, "userPlanId": plan_id,
                "stageFlag": "stage1", "stageName": "Vegetative", "icon": "",
                "sort": 1, "startTime": 1, "timeOffset": 0,
                "contentUpdate": 1, "planMode": 1,
                "planStageContent": json.dumps({"light": {"slot": [
                    {"time": 360, "level": 70}, {"time": 1080, "level": 0},
                ]}}),
            }],
        }],
    }, scene_id=scene_id)


def _seedling_garden_recipe(scene_id: int, *, active: bool) -> GardenPlans:
    """An independently configured Seedling stage in the garden REST response."""
    recipe = _running_garden_recipe(scene_id)
    plan = recipe.plans[0]
    stage = replace(plan.stages[0], name="Seedling", start_time=1 if active else 0)
    return GardenPlans(scene_id, (replace(plan, name="Autoflower", stages=(stage,)),))


async def _wait_for_garden_stage(coordinator: VivosunCoordinator, scene_id: int, name: str) -> None:
    from custom_components.vivosun_growhub.garden_display import select_garden_plan

    async def changed() -> None:
        while True:
            read = coordinator.garden_plan_read(scene_id)
            if read.status == "current" and read.plans is not None:
                selected = select_garden_plan(read.plans, int(datetime.now(tz=UTC).timestamp()))
                if selected.stage is not None and selected.stage.name == name:
                    return
            await asyncio.sleep(0.001)

    await asyncio.wait_for(changed(), timeout=2)


async def test_reported_mqtt_plan_change_refetches_only_its_garden_without_inventory(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    api.plans[66078] = _seedling_garden_recipe(66078, active=False)
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    await coordinator.async_start()
    entry, platform = await _setup_native_garden_sensors(hass, coordinator)
    try:
        assert _garden_sensor_state(hass, entry, 66078, "stage").state == "Inactive"
        api.plans[66078] = _seedling_garden_recipe(66078, active=True)
        mqtt = _MqttStub.instances[0]
        topic = TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing="thing-1")
        reported = {"state": {"reported": {"plan": {
            "stage1": {"startT": 1700000000, "contId": "seedling+active"},
        }}}}
        inventory_calls = api.calls.count("get_gardens")
        baseline = api.calls.count("get_garden_plans:66078")
        await mqtt.emit(topic, json.dumps(reported).encode())
        await _wait_for_garden_stage(coordinator, 66078, "Seedling")
        await hass.async_block_till_done()
        assert _garden_sensor_state(hass, entry, 66078, "stage").state == "Seedling"
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "Autoflower"
        assert api.calls.count("get_garden_plans:66078") == baseline + 1
        assert api.calls.count("get_garden_plans:66079") == 1
        assert api.calls.count("get_gardens") == inventory_calls

        # The exact same push, desired-only acknowledgment and irrelevant sensor
        # updates must not schedule redundant cloud recipe reads.
        await mqtt.emit(topic, json.dumps(reported).encode())
        await mqtt.emit(topic, b'{"state":{"desired":{"plan":{"stage1":{"startT":2}}}}}')
        await mqtt.emit(topic, b'{"state":{"reported":{"light":{"manu":{"lv":15}}}}}')
        await mqtt.emit(TOPIC_CHANNEL_APP.format(topic_prefix="topic/1"), b'{"inTemp":2200}')
        await hass.async_block_till_done()
        assert api.calls.count("get_garden_plans:66078") == baseline + 1
    finally:
        await entry._async_process_on_unload(hass)
        await platform.async_reset()
        await coordinator.async_shutdown()


async def test_mqtt_plan_change_during_read_replays_latest_and_recovers_from_failure(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    await coordinator.async_start()
    mqtt = _MqttStub.instances[0]
    entered, resume = asyncio.Event(), asyncio.Event()
    reads = 0

    async def read(_tokens: AuthTokens, scene_id: int) -> GardenPlans:
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            await resume.wait()
            raise VivosunConnectionError("offline")
        return _seedling_garden_recipe(scene_id, active=True)

    api.get_garden_plans = read  # type: ignore[method-assign]
    topic = TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing="thing-1")
    try:
        first = {"state": {"reported": {"plan": {"stage1": {"startT": 1, "contId": "a+one"}}}}}
        second = {"state": {"reported": {"plan": {"stage1": {"startT": 2, "contId": "b+two"}}}}}
        third = {"state": {"reported": {"plan": {"stage1": {"startT": 3, "contId": "c+three"}}}}}
        await mqtt.emit(topic, json.dumps(first).encode())
        await asyncio.wait_for(entered.wait(), timeout=2)
        await mqtt.emit(topic, json.dumps(second).encode())
        await mqtt.emit(topic, json.dumps(third).encode())
        resume.set()
        await _wait_for_garden_stage(coordinator, 66078, "Seedling")
        # Several distinct reports during one blocked read still require one replay.
        assert reads == 2
        assert coordinator.garden_plan_read(66079).status == "current"
    finally:
        resume.set()
        await coordinator.async_shutdown()


async def test_no_mqtt_stage_changes_are_refreshed_without_account_inventory(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    await coordinator.async_start()
    try:
        api.plans[66080] = _seedling_garden_recipe(66080, active=True)
        coordinator._garden_read_last_attempt[66080] = datetime.now(tz=UTC) - timedelta(minutes=2)
        before = api.calls.count("get_gardens")
        await coordinator._async_garden_tick(datetime.now(tz=UTC))
        await _wait_for_garden_stage(coordinator, 66080, "Seedling")
        assert api.calls.count("get_garden_plans:66080") == 2  # Initial read plus periodic refresh.
        assert api.calls.count("get_gardens") == before
    finally:
        await coordinator.async_shutdown()


async def _setup_native_garden_sensors(
    hass: HomeAssistant, coordinator: VivosunCoordinator,
) -> tuple[MockConfigEntry, EntityPlatform]:
    """Exercise real garden entities through the Home Assistant sensor platform."""
    entry = MockConfigEntry(domain=DOMAIN, title="Synthetic gardens", data={})
    entry.add_to_hass(hass)
    coordinator.set_garden_topology(GardenTopology(hass, entry.entry_id))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id, coordinator=coordinator,
    )
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    platform.config_entry = entry

    def add_entities(entities: list[SensorEntity]) -> None:
        hass.async_create_task(platform.async_add_entities(entities))

    await async_setup_sensors(hass, entry, add_entities)
    await hass.async_block_till_done()
    return entry, platform


def _garden_sensor_state(hass: HomeAssistant, entry: MockConfigEntry, scene_id: int, role: str) -> State:
    unique_id = f"vivosun_growhub_{entry.entry_id}_garden_{scene_id}_{role}"
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_garden_plan_reads_fail_independently_and_recover_without_mqtt(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    try:
        await coordinator.async_start()
        assert {g: coordinator.garden_plan_read(g).status for g in (66078, 66079, 66080)} == {
            66078: "current", 66079: "current", 66080: "current",
        }
        api.plan_failures.add(66078)
        await coordinator.async_refresh_inventory(force=True)
        first = coordinator.garden_plan_read(66078)
        assert first.status == "stale" and first.plans == GardenPlans(66078, ())
        assert coordinator.garden_plan_read(66079).status == "current"
        assert coordinator.garden_plan_read(66080).status == "current"
        coordinator._mqtt_client = None
        assert coordinator.garden_plan_read(66079).status == "current"
        api.plan_failures.clear()
        await coordinator.async_refresh_inventory(force=True)
        assert coordinator.garden_plan_read(66078).status == "current"
    finally:
        await coordinator.async_shutdown()


async def test_garden_plan_refresh_keeps_snapshot_until_result(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    try:
        await coordinator.async_start()
        before = coordinator.garden_plan_read(66078).plans
        entered, release = asyncio.Event(), asyncio.Event()
        original = api.get_garden_plans

        async def paused(tokens: AuthTokens, scene_id: int) -> GardenPlans:
            if scene_id == 66078:
                entered.set()
                await release.wait()
            return await original(tokens, scene_id)

        api.get_garden_plans = paused  # type: ignore[method-assign]
        refresh = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            pending = coordinator.garden_plan_read(66078)
            assert pending.status == "refreshing" and pending.plans == before
            # Other gardens finish independently, so either state is valid here.
            assert coordinator.garden_plan_read(66079).status in ("refreshing", "current")
            api.plan_failures.add(66078)
        finally:
            release.set()
            await refresh
        failed = coordinator.garden_plan_read(66078)
        assert failed.status == "stale" and failed.plans == before
        assert coordinator.garden_plan_read(66079).status == "current"
        api.plan_failures.clear()
        await coordinator.async_refresh_inventory(force=True)
        assert coordinator.garden_plan_read(66078).status == "current"
    finally:
        await coordinator.async_shutdown()


async def test_native_garden_entities_preserve_valid_reads_without_reviving_failed_retries(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    api.plans[66078] = _running_garden_recipe(66078)
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    entry: MockConfigEntry | None = None
    platform: EntityPlatform | None = None
    refresh: asyncio.Task[None] | None = None
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await coordinator.async_start()
        entry, platform = await _setup_native_garden_sensors(hass, coordinator)
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "Photoperiod"
        assert _garden_sensor_state(hass, entry, 66078, "stage").state == "Vegetative"
        assert _garden_sensor_state(hass, entry, 66078, "plan_light").attributes["read_status"] == "current"
        assert _garden_sensor_state(hass, entry, 66078, "stage").attributes["configured_only"] is True
        original = api.get_garden_plans

        async def paused(tokens: AuthTokens, scene_id: int) -> GardenPlans:
            if scene_id == 66078:
                entered.set()
                await release.wait()
            return await original(tokens, scene_id)

        api.get_garden_plans = paused  # type: ignore[method-assign]
        refresh = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await hass.async_block_till_done()
        for role in ("recipe", "stage", "plan_light"):
            state = _garden_sensor_state(hass, entry, 66078, role)
            assert state.state != "unavailable" and state.attributes["read_status"] == "refreshing"
        assert _garden_sensor_state(hass, entry, 66079, "recipe").state == "No recipe"

        api.plan_failures.add(66078)
        release.set()
        await refresh
        await hass.async_block_till_done()
        for role in ("recipe", "stage", "plan_light"):
            state = _garden_sensor_state(hass, entry, 66078, role)
            assert state.state == "unavailable"
        # HA omits entity-provided attributes when an entity is unavailable.
        assert coordinator.garden_plan_read(66078).status == "stale"
        assert _garden_sensor_state(hass, entry, 66079, "recipe").attributes["read_status"] == "current"

        entered.clear()
        release.clear()
        refresh = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await hass.async_block_till_done()
        for role in ("recipe", "stage", "plan_light"):
            state = _garden_sensor_state(hass, entry, 66078, role)
            assert state.state == "unavailable"
        assert coordinator.garden_plan_read(66078).status == "stale"
        release.set()
        await refresh
        api.plan_failures.clear()
        api.get_garden_plans = original  # type: ignore[method-assign]
        await coordinator.async_refresh_inventory(force=True)
        await hass.async_block_till_done()
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "Photoperiod"
        assert _garden_sensor_state(hass, entry, 66078, "stage").state == "Vegetative"
        assert _garden_sensor_state(hass, entry, 66078, "plan_light").attributes["read_status"] == "current"
    finally:
        release.set()
        if refresh is not None and not refresh.done():
            await refresh
        if entry is not None:
            await entry._async_process_on_unload(hass)
        if platform is not None:
            await platform.async_reset()
        await coordinator.async_shutdown()


async def test_native_garden_first_failure_removal_and_inventory_failure(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    api.plans[66078] = _running_garden_recipe(66078)
    api.plan_failures.add(66078)
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    entry: MockConfigEntry | None = None
    platform: EntityPlatform | None = None
    try:
        await coordinator.async_start()
        entry, platform = await _setup_native_garden_sensors(hass, coordinator)
        first = _garden_sensor_state(hass, entry, 66078, "recipe")
        assert first.state == "unavailable"
        assert coordinator.garden_plan_read(66078).status == "stale"
        assert _garden_sensor_state(hass, entry, 66079, "recipe").state == "No recipe"

        api.plan_failures.clear()
        await coordinator.async_refresh_inventory(force=True)
        await hass.async_block_till_done()
        assert _garden_sensor_state(hass, entry, 66078, "stage").state == "Vegetative"

        inventory = api.gardens
        removed_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"vivosun_growhub_{entry.entry_id}_garden_66080_recipe",
        )
        assert removed_id is not None
        api.failure = VivosunResponseError("synthetic invalid garden listing")
        await coordinator.async_refresh_inventory(force=True)
        await hass.async_block_till_done()
        assert er.async_get(hass).async_get(removed_id) is not None
        assert dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, GardenTopology(hass, entry.entry_id).identifier(66080)), entry.entry_id,
        ) is not None
        api.failure = None
        api.gardens = GardenInventory(tuple(g for g in inventory.gardens if g.scene_id != 66080))
        await coordinator.async_refresh_inventory(force=True)
        await hass.async_block_till_done()
        for role in ("recipe", "stage"):
            assert er.async_get(hass).async_get_entity_id(
                "sensor", DOMAIN, f"vivosun_growhub_{entry.entry_id}_garden_66080_{role}",
            ) is None
        assert coordinator.garden_plan_read(66080).status == "unknown"
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "Photoperiod"
        assert hass.states.get(removed_id) is None

        api.failure = VivosunResponseError("synthetic invalid garden listing")
        await coordinator.async_refresh_inventory(force=True)
        await hass.async_block_till_done()
        assert coordinator.garden_inventory == api.gardens
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "unavailable"
        api.failure = None
        api.gardens = inventory
        await coordinator.async_refresh_inventory(force=True)
        await hass.async_block_till_done()
        assert _garden_sensor_state(hass, entry, 66080, "recipe").state == "No recipe"
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "Photoperiod"
        await coordinator.async_refresh_inventory(force=True)
        await hass.async_block_till_done()
        for role in ("recipe", "stage"):
            unique_id = f"vivosun_growhub_{entry.entry_id}_garden_66080_{role}"
            assert len([row for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                        if row.unique_id == unique_id]) == 1
    finally:
        if entry is not None:
            await entry._async_process_on_unload(hass)
        if platform is not None:
            await platform.async_reset()
        await coordinator.async_shutdown()


async def test_cancelled_garden_read_does_not_leave_native_entities_refreshing(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    api.plans[66078] = _running_garden_recipe(66078)
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    entry: MockConfigEntry | None = None
    platform: EntityPlatform | None = None
    refresh: asyncio.Task[None] | None = None
    entered, resume = asyncio.Event(), asyncio.Event()
    try:
        await coordinator.async_start()
        entry, platform = await _setup_native_garden_sensors(hass, coordinator)
        original = api.get_garden_plans

        async def paused(tokens: AuthTokens, scene_id: int) -> GardenPlans:
            if scene_id == 66078:
                entered.set()
                await resume.wait()
            return await original(tokens, scene_id)

        api.get_garden_plans = paused  # type: ignore[method-assign]
        refresh = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await hass.async_block_till_done()
        assert _garden_sensor_state(hass, entry, 66078, "recipe").attributes["read_status"] == "refreshing"
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        await hass.async_block_till_done()
        assert coordinator.garden_plan_read(66078).status == "stale"
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "unavailable"
        api.get_garden_plans = original  # type: ignore[method-assign]
        await coordinator.async_refresh_inventory(force=True)
        await hass.async_block_till_done()
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "Photoperiod"
    finally:
        resume.set()
        if refresh is not None and not refresh.done():
            await refresh
        if entry is not None:
            await entry._async_process_on_unload(hass)
        if platform is not None:
            await platform.async_reset()
        await coordinator.async_shutdown()


@pytest.mark.parametrize(("error", "category"), [
    (VivosunAuthError("SENSITIVE auth reply"), "authentication"),
    (VivosunConnectionError("SENSITIVE network path"), "transport"),
    (TimeoutError("SENSITIVE timeout"), "transport"),
    (VivosunResponseError("SENSITIVE response data"), "response_or_schema"),
    (RuntimeError("SENSITIVE unexpected failure"), "unexpected"),
])
async def test_garden_read_failure_logs_bounded_category_only(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error: Exception, category: str,
) -> None:
    api = _GardenApiStub()
    api.plan_errors[66078] = error
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    try:
        with caplog.at_level(logging.WARNING):
            await coordinator.async_start()
        messages = [record.getMessage() for record in caplog.records
                    if record.message.startswith("Garden recipe read unavailable")]
        assert messages == [
            f"Garden recipe read unavailable ({category}); keeping last valid result",
        ]
        assert "SENSITIVE" not in caplog.text
        assert coordinator.garden_plan_read(66078).status == "stale"
        assert coordinator.garden_plan_read(66079).status == "current"
    finally:
        await coordinator.async_shutdown()


async def test_late_plan_response_cannot_replace_newer_garden_read(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    try:
        await coordinator.async_start()
        waiting, proceed = asyncio.Event(), asyncio.Event()
        original = api.get_garden_plans

        async def slow(tokens: AuthTokens, scene_id: int) -> GardenPlans:
            if scene_id == 66078:
                waiting.set()
                await proceed.wait()
                return GardenPlans(scene_id, ())
            return await original(tokens, scene_id)

        api.get_garden_plans = slow  # type: ignore[method-assign]
        stale = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await waiting.wait()
        api.get_garden_plans = original  # type: ignore[method-assign]
        api.plan_failures.add(66078)
        requested = asyncio.Event()
        queue = coordinator._queue_garden_read

        def observe(scene_id: int, *, periodic: bool = False) -> asyncio.Task[None] | None:
            task = queue(scene_id, periodic=periodic)
            if scene_id == 66078:
                requested.set()
            return task

        monkeypatch.setattr(coordinator, "_queue_garden_read", observe)
        newer = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await asyncio.wait_for(requested.wait(), timeout=2)
        proceed.set()
        await asyncio.gather(stale, newer)
        assert coordinator.garden_plan_read(66078).status == "stale"
    finally:
        await coordinator.async_shutdown()


async def test_runtime_garden_discovery_preserves_mqtt_and_known_entities(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="test@example.com", password="secret")
    await coordinator.async_start()
    mqtt = _MqttStub.instances[0]
    assert coordinator.garden_inventory == _garden_inventory()

    discovered: list[tuple[str, ...]] = []
    unsubscribe = coordinator.async_add_discovery_listener(
        lambda devices: discovered.append(tuple(device.device_id for device in devices))
    )
    assert discovered == [("device-1", "device-2")]
    original_gets = [topic for topic, *_ in mqtt.published if topic.endswith("/shadow/get")]
    await coordinator.async_refresh_inventory()
    assert api.calls.count("get_gardens") == 1
    await coordinator.async_refresh_inventory(force=True)
    assert discovered[-1] == ("device-1", "device-2")
    assert [topic for topic, *_ in mqtt.published if topic.endswith("/shadow/get")] == original_gets

    api.devices.append(DeviceInfo("device-3", "thing-3", "topic/3", "Device C", True, 66079,
                                  device_type="controller"))
    api.devices[0] = replace(api.devices[0], scene_id=66078)
    api.gardens = _garden_inventory(moved=True, added=True)
    await coordinator.async_refresh_inventory(force=True)
    assert tuple(device.device_id for device in coordinator.devices) == ("device-1", "device-2", "device-3")
    assert discovered[-1] == ("device-1", "device-2", "device-3")
    assert len(_MqttStub.instances) == 1 and mqtt.connected
    assert (TOPIC_SHADOW_GET_ACCEPTED.format(thing="thing-3"), 1) in mqtt.subscribed_topics
    assert [topic for topic, *_ in mqtt.published if topic == TOPIC_SHADOW_GET.format(thing="thing-3")]

    api.failure = VivosunResponseError("bad listing")
    await coordinator.async_refresh_inventory(force=True)
    assert coordinator.garden_inventory == _garden_inventory(moved=True, added=True)
    api.failure = None
    api.devices[0] = replace(api.devices[0], scene_id=66079)
    await coordinator.async_refresh_inventory(force=True)
    assert coordinator.garden_inventory == _garden_inventory(moved=True, added=True)
    unsubscribe()
    await coordinator.async_shutdown()
    assert not coordinator._device_listeners


async def test_stale_garden_read_cannot_override_newer_complete_refresh(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    coordinator = VivosunCoordinator(hass, object(), email="test@example.com", password="secret")
    await coordinator.async_start()
    blocked = asyncio.Event()
    proceed = asyncio.Event()
    original_get_gardens = api.get_gardens

    async def delayed(_tokens: AuthTokens) -> GardenInventory:
        blocked.set()
        await proceed.wait()
        return _garden_inventory()

    api.get_gardens = delayed  # type: ignore[method-assign]
    stale = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
    await blocked.wait()
    api.get_gardens = original_get_gardens  # type: ignore[method-assign]
    api.devices = [replace(api.devices[0], scene_id=66079)]
    api.gardens = GardenInventory((Garden(66079, 600, "Renamed", "Europe/Berlin",
                                            (GardenMember("device-2", "thing-2"),)),))
    await coordinator.async_refresh_inventory(force=True)
    proceed.set()
    await stale
    assert coordinator.garden_inventory == api.gardens
    await coordinator.async_shutdown()


async def test_invalid_and_superseded_inventory_cannot_remove_registry_rows(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    entry: MockConfigEntry | None = None
    platform: EntityPlatform | None = None
    resume = asyncio.Event()
    stale: asyncio.Task[None] | None = None
    try:
        await coordinator.async_start()
        entry, platform = await _setup_native_garden_sensors(hass, coordinator)
        registry = er.async_get(hass)
        uid = f"vivosun_growhub_{entry.entry_id}_garden_66080_recipe"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
        topology = GardenTopology(hass, entry.entry_id)
        devices = dr.async_get(hass)
        parent = devices.async_get_device_by_identifier((DOMAIN, topology.identifier(66080)), entry.entry_id)
        assert entity_id is not None and parent is not None

        original = api.gardens
        api.gardens = GardenInventory(())
        await coordinator.async_refresh_inventory(force=True)
        assert registry.async_get(entity_id) is not None
        assert devices.async_get(parent.id) is not None
        api.gardens = original

        entered = asyncio.Event()
        get_gardens = api.get_gardens

        async def delayed(_tokens: AuthTokens) -> GardenInventory:
            entered.set()
            await resume.wait()
            return GardenInventory(tuple(g for g in original.gardens if g.scene_id != 66080))

        api.get_gardens = delayed  # type: ignore[method-assign]
        stale = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        api.get_gardens = get_gardens  # type: ignore[method-assign]
        await coordinator.async_refresh_inventory(force=True)
        resume.set()
        await asyncio.wait_for(stale, timeout=2)
        await hass.async_block_till_done()
        assert coordinator.garden_inventory == original
        assert registry.async_get(entity_id) is not None
        assert devices.async_get(parent.id) is not None
    finally:
        resume.set()
        if stale is not None and not stale.done():
            await stale
        if entry is not None:
            await entry._async_process_on_unload(hass)
        if platform is not None:
            await platform.async_reset()
        await coordinator.async_shutdown()


def _garden_coordinator(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, api: _GardenApiStub,
) -> tuple[VivosunCoordinator, _AwsAuthStub]:
    """Set up isolated coordinator and encrypted-read/MQTT test doubles."""
    aws = _AwsAuthStub()
    aws.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws)
    return VivosunCoordinator(hass, object(), email="synthetic@example.invalid", password="synthetic"), aws


async def test_shutdown_abandons_pending_discovery_and_restart_uses_new_lifecycle(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, aws = _garden_coordinator(hass, monkeypatch, api)
    entry = MockConfigEntry(domain=DOMAIN, title="Gardens", data={})
    entry.add_to_hass(hass)
    await coordinator.async_start()
    coordinator.set_garden_topology(GardenTopology(hass, entry.entry_id))
    prior = coordinator.garden_inventory
    received: list[tuple[str, ...]] = []
    coordinator.async_add_discovery_listener(lambda devices: received.append(tuple(d.device_id for d in devices)))
    mqtt = _MqttStub.instances[0]
    api.devices.append(DeviceInfo("device-3", "thing-3", "topic/3", "Third", True, 66079, "controller"))
    api.gardens = _garden_inventory(added=True)
    subscribed = asyncio.Event()
    resume = asyncio.Event()
    original = mqtt.subscribe

    async def delayed(topics: list[tuple[str, int]]) -> None:
        if any("thing-3" in topic for topic, _ in topics):
            subscribed.set()
            await resume.wait()
        await original(topics)

    mqtt.subscribe = delayed
    refresh = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=2)
        await coordinator.async_shutdown()
    finally:
        resume.set()
        await asyncio.wait_for(refresh, timeout=2)

    registry = dr.async_get(hass)
    assert coordinator.devices == [] and coordinator.garden_inventory is None
    assert coordinator._subscribed_routes == set()
    assert received == [("device-1", "device-2")]
    assert registry.async_get_device(identifiers={(DOMAIN, "device-3")}) is None
    assert prior is not None

    aws.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    await coordinator.async_start()
    coordinator.set_garden_topology(GardenTopology(hass, entry.entry_id))
    assert coordinator.garden_inventory == api.gardens
    assert registry.async_get_device(identifiers={(DOMAIN, "device-3")}) is not None
    assert len(_MqttStub.instances) == 2
    await coordinator.async_shutdown()


async def test_superseded_subscription_cannot_commit_and_recovery_adds_once(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    await coordinator.async_start()
    original_inventory = coordinator.garden_inventory
    new_device = DeviceInfo("device-3", "thing-3", "topic/3", "Third", True, 66079, "controller")
    api.devices.append(new_device)
    api.gardens = _garden_inventory(added=True)
    mqtt = _MqttStub.instances[0]
    subscribed = asyncio.Event()
    resume = asyncio.Event()
    original = mqtt.subscribe

    async def delayed(topics: list[tuple[str, int]]) -> None:
        if any("thing-3" in topic for topic, _ in topics):
            subscribed.set()
            await resume.wait()
        await original(topics)

    mqtt.subscribe = delayed
    received: list[tuple[str, ...]] = []
    coordinator.async_add_discovery_listener(lambda ds: received.append(tuple(d.device_id for d in ds)))
    stale = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=2)
        api.failure = VivosunResponseError("newer read rejected")
        await coordinator.async_refresh_inventory(force=True)
    finally:
        resume.set()
        await asyncio.wait_for(stale, timeout=2)
    assert coordinator.garden_inventory == original_inventory
    assert received == [("device-1", "device-2")]
    assert coordinator.get_device(new_device.device_id) is None
    assert (new_device.client_id, new_device.topic_prefix) not in coordinator._subscribed_routes

    api.failure = None
    await coordinator.async_refresh_inventory(force=True)
    assert received == [("device-1", "device-2"), ("device-1", "device-2", "device-3")]
    assert coordinator.garden_inventory == api.gardens
    await coordinator.async_refresh_inventory(force=True)
    assert received[-1] == ("device-1", "device-2", "device-3")
    await coordinator.async_shutdown()


@pytest.mark.parametrize("collision", ["client_id", "topic_prefix"])
async def test_retained_routes_cannot_be_reassigned_to_different_device(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, collision: str,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    await coordinator.async_start()
    before = coordinator.garden_inventory
    mqtt = _MqttStub.instances[0]
    subscribed_before = tuple(mqtt.subscribed_topics)
    client_id = "thing-1" if collision == "client_id" else "thing-new"
    prefix = "topic/1" if collision == "topic_prefix" else "topic/new"
    api.devices = [api.devices[0], DeviceInfo("device-new", client_id, prefix, "New", True, 66078, "controller")]
    api.gardens = GardenInventory((
        Garden(66078, 600, "First", "UTC", (GardenMember("device-new", client_id),)),
        Garden(66079, 600, "Second", "UTC", (GardenMember("device-2", "thing-2"),)),
    ))
    await coordinator.async_refresh_inventory(force=True)
    assert coordinator.garden_inventory == before
    assert coordinator.get_device("device-new") is None
    assert coordinator._route_topic_to_device(TOPIC_SHADOW_GET_ACCEPTED.format(thing="thing-1")) == "device-1"
    assert coordinator._route_topic_to_device(TOPIC_CHANNEL_APP.format(topic_prefix="topic/1")) == "device-1"
    assert tuple(mqtt.subscribed_topics) == subscribed_before
    await coordinator.async_shutdown()


@pytest.mark.parametrize("gardens", [None, GardenInventory(())])
async def test_empty_account_needs_verified_garden_to_bootstrap(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, gardens: GardenInventory | None,
) -> None:
    api = _GardenApiStub()
    api.devices = []
    if gardens is None:
        api.failure = VivosunResponseError("garden endpoint unavailable")
    else:
        api.gardens = gardens
    coordinator, aws = _garden_coordinator(hass, monkeypatch, api)
    try:
        with pytest.raises(VivosunResponseError, match="No devices found"):
            await coordinator.async_start()
        assert "get_aws_identity" not in api.calls
        assert aws.calls == []
    finally:
        await coordinator.async_shutdown()


async def test_verified_garden_only_account_registers_service_device(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    api.devices = []
    api.gardens = GardenInventory((Garden(100, 600, "Empty Garden", "UTC", ()),))
    coordinator, aws = _garden_coordinator(hass, monkeypatch, api)
    entry = MockConfigEntry(domain=DOMAIN, title="Account", data={})
    entry.add_to_hass(hass)
    try:
        await coordinator.async_start()
        coordinator.set_garden_topology(GardenTopology(hass, entry.entry_id))
        garden = dr.async_get(hass).async_get_device(
            identifiers={(DOMAIN, f"garden:{entry.entry_id}:100")}
        )
        assert garden is not None and garden.entry_type == dr.DeviceEntryType.SERVICE
        assert (garden.name, garden.via_device_id) == ("Empty Garden", None)
        assert coordinator.devices == [] and not coordinator.is_mqtt_connected
        assert "get_aws_identity" not in api.calls and aws.calls == []
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("endpoint", ["get_gardens", "get_devices", "get_garden_plans"])
async def test_garden_only_expired_auth_recovers_without_mqtt(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, endpoint: str,
) -> None:
    api = _GardenApiStub()
    api.devices = []
    api.gardens = GardenInventory((Garden(100, 600, "Empty Garden", "UTC", ()),))
    coordinator, aws = _garden_coordinator(hass, monkeypatch, api)
    try:
        await coordinator.async_start()
        old = api.tokens
        api.tokens = replace(old, access_token="renewed")
        attempts: list[AuthTokens] = []
        original = getattr(api, endpoint)

        async def expired(tokens: AuthTokens, *args: int) -> object:
            attempts.append(tokens)
            if tokens is old:
                raise VivosunAuthError("expired")
            return await original(tokens, *args)

        monkeypatch.setattr(api, endpoint, expired, raising=False)
        if endpoint in ("get_gardens", "get_devices"):
            await coordinator.async_refresh_inventory(force=True)
        elif endpoint == "get_garden_plans":
            task = coordinator._queue_garden_read(100)
            assert task is not None
            await task
        assert attempts == [old, api.tokens]
        assert api.calls.count("login") == 2
        assert coordinator.devices == [] and not coordinator.is_mqtt_connected
        assert aws.calls == []
    finally:
        await coordinator.async_shutdown()


async def test_concurrent_garden_reads_share_authentication_recovery(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    api.devices = []
    api.gardens = GardenInventory(tuple(Garden(i, 600, "Garden", "UTC", ()) for i in (10, 20, 30)))
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    try:
        await coordinator.async_start()
        old = api.tokens
        api.tokens = replace(old, access_token="renewed")
        login = api.login

        async def slow_login(email: str, password: str) -> AuthTokens:
            await asyncio.sleep(0)
            return await login(email, password)

        async def expired(tokens: AuthTokens, scene_id: int) -> GardenPlans:
            if tokens is old:
                raise VivosunAuthError("expired")
            return GardenPlans(scene_id, ())

        monkeypatch.setattr(api, "login", slow_login)
        monkeypatch.setattr(api, "get_garden_plans", expired)
        await asyncio.gather(*(coordinator._queue_garden_read(i) for i in (10, 20, 30)))
        assert api.calls.count("login") == 2  # Bootstrap and one shared recovery.
        assert all(coordinator.garden_plan_read(i).status == "current" for i in (10, 20, 30))
    finally:
        await coordinator.async_shutdown()


async def test_inventory_topic_prefix_change_subscribes_new_route(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    try:
        await coordinator.async_start()
        mqtt = coordinator._mqtt_client
        assert isinstance(mqtt, _MqttStub)
        api.devices = [replace(device, topic_prefix="replacement/topic") if device.device_id == "device-1"
                       else device for device in api.devices]
        await coordinator.async_refresh_inventory(force=True)
        topic = TOPIC_CHANNEL_APP.format(topic_prefix="replacement/topic")
        assert (topic, 1) in mqtt.subscribed_topics
        count = mqtt.subscribed_topics.count((topic, 1))
        await coordinator.async_refresh_inventory(force=True)
        assert mqtt.subscribed_topics.count((topic, 1)) == count
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("initial_kind", ["camera", "garden"])
async def test_periodic_discovery_runs_without_coordinator_entities_and_cancels_on_unload(
    hass: HomeAssistant, monkeypatch: MonkeyPatch, initial_kind: str,
) -> None:
    api = _GardenApiStub()
    api.devices = ([DeviceInfo("camera", "", "", "GrowCam", True, 0, "camera")]
                   if initial_kind == "camera" else [])
    api.gardens = GardenInventory((Garden(10, 600, "Empty", "UTC", ()),))
    coordinator, aws = _garden_coordinator(hass, monkeypatch, api)
    await coordinator.async_start()
    received: list[tuple[str, ...]] = []
    coordinator.async_add_discovery_listener(lambda devices: received.append(tuple(d.device_id for d in devices)))
    assert received == [()]
    old_calls = api.calls.count("get_gardens")
    now = datetime.now(tz=UTC)
    next_tick = now + timedelta(minutes=11)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return next_tick if tz is not None else next_tick.replace(tzinfo=None)

    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator.datetime", Clock)
    aws.queue_credentials(_credentials(next_tick + timedelta(hours=1)))
    api.devices.append(DeviceInfo("device-3", "thing-3", "topic/3", "Third", True, 10, "controller"))
    api.gardens = GardenInventory((Garden(10, 600, "Empty", "UTC", (GardenMember("device-3", "thing-3"),)),))
    try:
        async_fire_time_changed(hass, next_tick, fire_all=True)
        await hass.async_block_till_done()
        assert api.calls.count("get_gardens") > old_calls
        assert ("device-3",) in received
        assert coordinator.get_device("device-3") is not None
        await _wait_for_mqtt_connected(coordinator)
    finally:
        await coordinator.async_shutdown()
    calls_after_unload = api.calls.count("get_gardens")
    async_fire_time_changed(hass, next_tick + timedelta(minutes=11), fire_all=True)
    await hass.async_block_till_done()
    assert api.calls.count("get_gardens") == calls_after_unload


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


async def test_coordinator_reported_plan_does_not_call_obsolete_stage_info_api(
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

    assert api.plan_stage_info_calls == []
    await coordinator.async_shutdown()


async def test_latest_mqtt_event_survives_newer_manual_inventory_during_blocked_event_read(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    api.plans[66078] = _seedling_garden_recipe(66078, active=False)
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    entered, release = asyncio.Event(), asyncio.Event()
    entry: MockConfigEntry | None = None
    platform: EntityPlatform | None = None
    try:
        await coordinator.async_start()
        entry, platform = await _setup_native_garden_sensors(hass, coordinator)
        old = api.plans[66078]
        latest = _seedling_garden_recipe(66078, active=True)
        real_read = api.get_garden_plans
        calls = 0

        async def observed(tokens: AuthTokens, scene_id: int) -> GardenPlans:
            nonlocal calls
            if scene_id != 66078:
                return await real_read(tokens, scene_id)
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return old
            return latest

        api.get_garden_plans = observed  # type: ignore[method-assign]
        mqtt = _MqttStub.instances[0]
        topic = TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing="thing-1")
        await mqtt.emit(topic, b'{"state":{"reported":{"plan":{"stage1":{"startT":1,"contId":"one+seed"}}}}}')
        await asyncio.wait_for(entered.wait(), timeout=2)
        requested = asyncio.Event()
        queue = coordinator._queue_garden_read

        def observe(scene_id: int, *, periodic: bool = False) -> asyncio.Task[None] | None:
            task = queue(scene_id, periodic=periodic)
            if scene_id == 66078:
                requested.set()
            return task

        monkeypatch.setattr(coordinator, "_queue_garden_read", observe)
        manual = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await asyncio.wait_for(requested.wait(), timeout=2)
        await mqtt.emit(topic, b'{"state":{"reported":{"plan":{"stage1":{"startT":2,"contId":"two+seed"}}}}}')
        release.set()
        await asyncio.wait_for(manual, timeout=2)
        await hass.async_block_till_done()
        assert calls == 2
        assert _garden_sensor_state(hass, entry, 66078, "stage").state == "Seedling"
        assert _garden_sensor_state(hass, entry, 66078, "recipe").state == "Autoflower"
    finally:
        release.set()
        if entry is not None:
            await entry._async_process_on_unload(hass)
        if platform is not None:
            await platform.async_reset()
        await coordinator.async_shutdown()


async def test_cancelling_inventory_waiter_does_not_cancel_later_mqtt_stage_change(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    api.plans[66078] = _seedling_garden_recipe(66078, active=False)
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await coordinator.async_start()
        previous = api.plans[66078]
        latest = _seedling_garden_recipe(66078, active=True)
        real_read = api.get_garden_plans
        calls = 0

        async def paused(tokens: AuthTokens, scene_id: int) -> GardenPlans:
            nonlocal calls
            if scene_id != 66078:
                return await real_read(tokens, scene_id)
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return previous
            return latest

        api.get_garden_plans = paused  # type: ignore[method-assign]
        refresh = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await _MqttStub.instances[0].emit(
            TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing="thing-1"),
            b'{"state":{"reported":{"plan":{"stage1":{"startT":1,"contId":"abc+seed"}}}}}',
        )
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        release.set()
        await _wait_for_garden_stage(coordinator, 66078, "Seedling")
        assert calls == 2
        assert coordinator.garden_plan_read(66078).plans == latest
    finally:
        release.set()
        await coordinator.async_shutdown()


async def test_mixed_inventory_and_event_garden_reads_share_three_slots(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    three_entered, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    refresh: asyncio.Task[None] | None = None
    try:
        await coordinator.async_start()
        api.gardens = GardenInventory((*api.gardens.gardens, *(
            Garden(66081 + i, 600, f"Additional {i}", "UTC", ()) for i in range(4)
        )))
        real_read = api.get_garden_plans

        async def blocked(tokens: AuthTokens, scene_id: int) -> GardenPlans:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                three_entered.set()
            try:
                await release.wait()
                return await real_read(tokens, scene_id)
            finally:
                active -= 1

        api.get_garden_plans = blocked  # type: ignore[method-assign]
        refresh = asyncio.create_task(coordinator.async_refresh_inventory(force=True))
        await asyncio.wait_for(three_entered.wait(), timeout=2)
        mqtt = _MqttStub.instances[0]
        topic = TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing="thing-1")
        await mqtt.emit(topic, b'{"state":{"reported":{"plan":{"stage1":{"startT":1,"contId":"live+seed"}}}}}')
        await coordinator._async_garden_tick(datetime.now(tz=UTC))
        assert peak == 3
        assert len(coordinator._garden_read_tasks) <= len(api.gardens.gardens)
        release.set()
        await asyncio.wait_for(refresh, timeout=3)
        assert peak == 3
    finally:
        release.set()
        if refresh is not None:
            await refresh
        await coordinator.async_shutdown()


async def test_capture_restores_bounded_known_stage_api_evidence_without_normal_fanout(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    client = VivosunApiClient(cast("aiohttp.ClientSession", object()))
    client.set_support_capture(coordinator._support_capture)
    calls: list[str] = []

    async def response(
        _method: str, _path: str, *, json_body: dict[str, object], **_kwargs: object,
    ) -> dict[str, object]:
        stage_id = cast("str", json_body["userPlanStageId"])
        calls.append(stage_id)
        if stage_id == "new":
            raise VivosunResponseError("must-not-appear-in-capture")
        return {"stageName": "Seedling", "icon": "", "planStageContent": json.dumps({
            "light": {"slot": []}, "hwId": "must-remain-private", "accessToken": "secret",
        })}

    monkeypatch.setattr(client, "_request_json", response)
    api.get_plan_stage_info = client.get_plan_stage_info  # type: ignore[method-assign]
    try:
        await coordinator.async_start()
        mqtt = _MqttStub.instances[0]
        topic = TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing="thing-1")
        reported = b'{"state":{"reported":{"plan":{"stage1":{"startT":1,"contId":"abc+seed"}}}}}'
        await mqtt.emit(topic, reported)
        assert calls == [] and not coordinator.support_capture_active

        # Enabling capture after the genuine MQTT report must still collect it.
        await coordinator.async_start_support_capture(max_events=10)
        await asyncio.wait_for(asyncio.gather(*coordinator._support_stage_tasks), timeout=2)
        snapshot = coordinator.support_capture_snapshot()
        assert calls == ["abc"]
        assert len(snapshot["stage_info_results"]) == 1
        saved = snapshot["stage_info_results"][0]
        assert saved["status"] == "success" and saved["stage_name"] == "Seedling"
        assert saved["content"]["hwId"] == "***" and saved["content"]["accessToken"] == "***"
        assert "must-remain-private" not in str(snapshot)

        # Duplicate, desired-only and unrelated reporting do not refetch.
        await mqtt.emit(topic, reported)
        await mqtt.emit(topic, b'{"state":{"desired":{"plan":{"stage1":{"startT":3}}}}}')
        await mqtt.emit(topic, b'{"state":{"reported":{"light":{"mode":1}}}}')
        assert calls == ["abc"]
        await coordinator.async_stop_support_capture()
        await mqtt.emit(topic, b'{"state":{"reported":{"plan":{"stage1":{"startT":3,"contId":"new+seed"}}}}}')
        assert calls == ["abc"]
        await coordinator.async_start_support_capture(max_events=10)
        await asyncio.wait_for(asyncio.gather(*coordinator._support_stage_tasks), timeout=2)
        assert calls == ["abc", "new"]
        saved = coordinator.support_capture_snapshot()["stage_info_results"]
        assert len(saved) == 1 and saved[0]["stage_id"] == "new"
        assert saved[0]["status"] == "error" and saved[0]["error_type"] == "VivosunResponseError"
        assert "must-not-appear-in-capture" not in str(saved)
    finally:
        await coordinator.async_shutdown()


async def test_capture_known_stage_failures_bounds_and_stop_cancel_pending_reads(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _GardenApiStub()
    coordinator, _ = _garden_coordinator(hass, monkeypatch, api)
    three_entered, release = asyncio.Event(), asyncio.Event()
    in_flight = peak = 0
    calls: list[str] = []

    async def blocked(_tokens: AuthTokens, stage_id: str) -> object:
        nonlocal in_flight, peak
        calls.append(stage_id)
        in_flight += 1
        peak = max(peak, in_flight)
        if in_flight == 3:
            three_entered.set()
        try:
            await release.wait()
            raise VivosunResponseError("sensitive-stage-identifier")
        finally:
            in_flight -= 1

    api.get_plan_stage_info = blocked  # type: ignore[method-assign]
    try:
        await coordinator.async_start()
        await coordinator.async_start_support_capture(max_events=10)
        mqtt = _MqttStub.instances[0]
        topic = TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing="thing-1")
        for batch in range(8):
            stages = {f"stage{i + 1}": {"startT": batch + 1, "contId": f"stage-{batch * 4 + i}+known"}
                      for i in range(4)}
            await mqtt.emit(topic, json.dumps({"state": {"reported": {"plan": stages}}}).encode())
        await asyncio.wait_for(three_entered.wait(), timeout=2)
        assert len(coordinator._support_stage_seen) == SUPPORT_CAPTURE_MAX_STAGE_RESULTS
        assert peak == 3 and len(calls) == 3
        await coordinator.async_stop_support_capture()
        assert coordinator._support_stage_tasks == set()
        assert in_flight == 0
        assert coordinator.support_capture_snapshot()["stage_info_results"] == []
        assert not coordinator.support_capture_active
    finally:
        release.set()
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


async def test_optional_light_rejection_subscription_failure_preserves_connection(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    api = _ApiStub()
    aws_auth = _AwsAuthStub()
    aws_auth.queue_credentials(_credentials(datetime.now(tz=UTC) + timedelta(hours=1)))
    _patch_coordinator_deps(monkeypatch, api, aws_auth)
    original_subscribe = _MqttStub.subscribe

    async def _subscribe(self: _MqttStub, topics: list[tuple[str, int]]) -> None:
        if len(topics) == 1 and topics[0][0].endswith("/update/rejected"):
            raise MQTTConnectionError("optional topic denied")
        await original_subscribe(self, topics)

    monkeypatch.setattr(_MqttStub, "subscribe", _subscribe)
    coordinator = VivosunCoordinator(hass, object(), email="user@example.com", password="secret")
    await coordinator.async_start()
    try:
        mqtt = _MqttStub.instances[0]
        assert mqtt.is_connected
        assert len([topic for topic, _ in mqtt.subscribed_topics if topic.endswith("/update/accepted")]) >= 1
        await coordinator.async_publish_shadow_update({"state": {"desired": {"light": {"manu": {"lv": 58}}}}})
        assert mqtt.published[-1][0].endswith("/shadow/update")
    finally:
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

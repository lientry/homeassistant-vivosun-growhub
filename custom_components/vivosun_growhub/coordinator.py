"""Coordinator lifecycle and runtime orchestration for Vivosun GrowHub."""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal, TypeAlias, TypeVar, cast
from uuid import uuid4

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .a22 import (
    A22Settings,
    build_a22_auto_setting,
    build_a22_cycle_setting,
    finish_a22_manual_change,
    make_a22_mode_change,
    read_a22_settings,
)
from .api import VivosunApiClient
from .aws_auth import AWS_CREDENTIAL_REFRESH_SKEW, AwsAuthClient, AwsCredentials
from .const import (
    CFAN_LEVEL_MAP,
    DFAN_LEVEL_MAP,
    DOMAIN,
    E42A_DFAN_AUTO_MODE,
    MODE_AUTO,
    MODE_CYCLE,
    SENSOR_UNAVAILABLE_SENTINEL,
    TOPIC_CHANNEL_APP,
    TOPIC_SHADOW_GET,
    TOPIC_SHADOW_GET_ACCEPTED,
    TOPIC_SHADOW_UPDATE,
    TOPIC_SHADOW_UPDATE_ACCEPTED,
    TOPIC_SHADOW_UPDATE_DELTA,
    TOPIC_SHADOW_UPDATE_DOCUMENTS,
    TOPIC_SHADOW_UPDATE_REJECTED,
)
from .exceptions import VivosunAuthError, VivosunConnectionError, VivosunResponseError
from .garden_topology import GardenTopology, garden_membership
from .gardens import GardenPlanRead
from .model_metadata import support_capture_model_metadata
from .models import CaptureDevice, client_model_token
from .mqtt_client import MQTTClient, MQTTConnectionError, MQTTError
from .ports import A22_PORTS, build_manual_port_payload
from .redaction import redact_identifier
from .shadow import (
    ChannelSensorState,
    ShadowParseError,
    ShadowV1State,
    accepted_desired_light_level_timestamp,
    build_cfan_level_payload,
    build_dfan_level_payload,
    build_light_level_payload,
    clamp_light_level,
    extract_shadow_reported,
    parse_channel_sensor_payload,
    parse_reported_fragment,
    parse_shadow_document,
    reported_light_level_timestamp,
)
from .support_capture import (
    SUPPORT_CAPTURE_MAX_DEVICES,
    SUPPORT_CAPTURE_MAX_PAYLOAD_BYTES,
    SUPPORT_CAPTURE_MAX_STAGE_RESULTS,
    SupportCaptureManager,
    summarize_support_capture_payload,
)
from .vseed import POINT_LOG_KEYS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiohttp
    from homeassistant.core import HomeAssistant

    from .gardens import GardenInventory
    from .manual_preferences import ManualPreferences, ManualSelection
    from .models import AuthTokens, AwsIdentity, DeviceInfo

_LOGGER = logging.getLogger(__name__)
_ReadResult = TypeVar("_ReadResult")
_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 60.0
_RECONNECT_HEALTH_CHECK_SECONDS = 2.0
_POINT_LOG_WINDOW_SECONDS = 300
_SHADOW_REFRESH_INTERVAL_SECONDS = 30.0
_SUPPORT_SHADOW_READ_TIMEOUT = 10.0
_FIELD_CONFIRMATION_TIMEOUT = 15.0
_INVENTORY_REFRESH_INTERVAL = timedelta(minutes=10)
_GARDEN_TICK_INTERVAL = timedelta(minutes=1)
_GARDEN_READ_INTERVAL = timedelta(minutes=2)
_GARDEN_EVENT_COALESCE_SECONDS = 0.05


def _garden_read_failure_category(error: Exception) -> str:
    """Classify a cloud read without logging untrusted exception details."""
    if isinstance(error, VivosunAuthError):
        return "authentication"
    if isinstance(error, (VivosunConnectionError, TimeoutError)):
        return "transport"
    if isinstance(error, VivosunResponseError):
        return "response_or_schema"
    return "unexpected"

_Channel: TypeAlias = Literal["light", "cFan", "dFan"]
_Field: TypeAlias = Literal["level", "mode", "manual_level"]
_FieldKey: TypeAlias = tuple[str, _Channel, _Field]
_CONTROLLER_CHANNELS: tuple[_Channel, ...] = ("light", "cFan", "dFan")
_CYCLE_READOUT_FIELDS: dict[_Channel, tuple[str, ...]] = {
    "light": ("onDur", "offDur", "lv", "spec", "rate"),
    "cFan": ("onDur", "offDur", "lvOn", "lvOff"),
    "dFan": ("onDur", "offDur", "lvOn", "lvOff"),
}

@dataclass(slots=True)
class _PendingField:
    """One requested shadow field, kept separate from actual device reports."""

    generation: int
    token: str
    value: int
    baseline: int | None
    baseline_version: int | None
    baseline_timestamp: int | None
    superseded_values: frozenset[int]
    timeout: asyncio.TimerHandle
    accepted_version: int | None = None
    accepted_timestamp: int | None = None


@dataclass(slots=True)
class _ConfirmedCommandReceipt:
    """A request already at the device-reported level, kept for rejection logging."""

    device_id: str
    channel: str
    timeout: asyncio.TimerHandle


@dataclass(frozen=True, slots=True)
class _ReportedFieldProvenance:
    """Shadow version and leaf timestamp for one genuinely reported field."""

    version: int | None
    timestamp: int | None


_A22_FIELD_PATHS: tuple[tuple[str, ...], ...] = (
    ("mode",), ("inPlan",), ("lv",), ("dev",), ("manu", "lv"),
    ("cycle", "tOffset"), ("cycle", "onDur"), ("cycle", "offDur"),
    ("auto", "tMin"), ("auto", "tMax"), ("auto", "hMin"), ("auto", "hMax"),
)
_A22_GUARDS = (("mode",), ("inPlan",))
_A22_STATE_ROOTS = frozenset({path[0] for path in _A22_FIELD_PATHS})
_A22_GROUPS = {
    "mode": ("mode",),
    "manual": ("manu", "lv"),
    "tOffset": ("cycle", "tOffset"),
    "onDur": ("cycle", "onDur"), "offDur": ("cycle", "onDur"),
    "tMin": ("auto", "tMin"), "tMax": ("auto", "tMin"),
    "hMin": ("auto", "hMin"), "hMax": ("auto", "hMin"),
}


@dataclass(slots=True)
class _A22Pending:
    """Requested leaves and genuine readback progress for one atomic device edit."""

    token: str
    expected: dict[tuple[str, ...], int]
    baseline: dict[tuple[str, ...], _ReportedFieldProvenance]
    confirmed: set[tuple[str, ...]]
    reports: dict[tuple[str, ...], _ReportedFieldProvenance]
    timeout: asyncio.TimerHandle
    accepted_version: int | None = None
    accepted_timestamps: dict[tuple[str, ...], int] | None = None


@dataclass(slots=True)
class _A22Receipt:
    """A bounded token receipt survives report settlement for delayed rejection."""

    device_id: str
    port: str
    group: tuple[str, ...]
    paths: tuple[tuple[str, ...], ...]
    timeout: asyncio.TimerHandle


def _reported_field_is_fresh(
    previous: _ReportedFieldProvenance | None, incoming: _ReportedFieldProvenance, topic: str,
) -> bool:
    """Reject stale per-leaf reads without imposing whole-shadow ordering.

    Equal-version reports can supply different fields. Metadata timestamps have
    one-second precision, so same-time GETs cannot override known reports.
    """
    if previous is None:
        return True
    if previous.version is not None and (incoming.version is None or incoming.version < previous.version):
        return False
    if previous.timestamp is not None and incoming.timestamp is not None:
        if incoming.timestamp < previous.timestamp:
            return False
        if incoming.timestamp == previous.timestamp and topic.endswith("/get/accepted"):
            return False
    if (
        previous.version == incoming.version and previous.version is not None
        and (incoming.timestamp is None or (
            previous.timestamp is not None and incoming.timestamp <= previous.timestamp
        ))
    ):
        return False
    return not (previous.timestamp is not None and incoming.timestamp is None and incoming.version is None)


@dataclass(slots=True)
class _PendingManualChoice:
    """One explicit HA input awaiting its matching cloud shadow acknowledgement."""

    device_id: str
    channel: str
    selection: ManualSelection
    shadow_level: int
    generation: int
    timeout: asyncio.TimerHandle


class VivosunCoordinator(DataUpdateCoordinator[dict[str, object]]):  # type: ignore[misc]
    """Coordinate cloud bootstrap and MQTT push state lifecycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        *,
        email: str,
        password: str,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize coordinator dependencies and lifecycle state."""
        coordinator_logger = logger or _LOGGER
        super().__init__(
            hass,
            coordinator_logger,
            name=DOMAIN,
            update_interval=timedelta(seconds=90),
        )
        self._logger = coordinator_logger
        self._api = VivosunApiClient(session)
        self._aws_auth = AwsAuthClient(session)
        self._email = email
        self._password = password

        self._tokens: AuthTokens | None = None
        self._auth_lock = asyncio.Lock()
        self._aws_identity: AwsIdentity | None = None
        self._aws_credentials: AwsCredentials | None = None
        self._devices: list[DeviceInfo] = []
        self._camera_devices: list[DeviceInfo] = []
        self._all_devices: list[DeviceInfo] = []
        self._garden_inventory: GardenInventory | None = None
        self._garden_topology: GardenTopology | None = None
        self._garden_reads: dict[int, GardenPlanRead] = {}
        self._garden_read_limit = asyncio.Semaphore(3)
        self._garden_read_tasks: dict[int, asyncio.Task[None]] = {}
        self._garden_read_versions: dict[int, int] = {}
        self._garden_read_last_attempt: dict[int, datetime] = {}
        self._physical_sample_versions: dict[str, int] = {}
        self._reported_plan_signatures: dict[str, str] = {}
        self._garden_listeners: list[Callable[[GardenInventory], None]] = []
        self._inventory_generation = 0
        self._inventory_last_attempt: datetime | None = None
        self._unsub_inventory_refresh: Callable[[], None] | None = None
        self._unsub_garden_tick: Callable[[], None] | None = None
        self._inventory_apply_lock = asyncio.Lock()
        self._forced_inventory_task: asyncio.Task[None] | None = None
        self._device_listeners: list[tuple[Callable[[list[DeviceInfo]], None], bool]] = []
        self._subscribed_routes: set[tuple[str, str]] = set()
        self._capture_devices: list[CaptureDevice] = []
        self._support_shadow_waiters: dict[tuple[str, str], asyncio.Future[dict[str, object]]] = {}
        self._support_capture_task: asyncio.Task[None] | None = None
        self._support_stage_tasks: set[asyncio.Task[None]] = set()
        self._support_stage_seen: set[str] = set()
        self._mqtt_client: MQTTClient | None = None
        self._support_capture_probe_client: MQTTClient | None = None
        self._support_capture = SupportCaptureManager()
        attach_capture = getattr(self._api, "set_support_capture", None)
        if callable(attach_capture):
            attach_capture(self._support_capture)

        # Per-device state keyed by device_id
        self._shadow_states: dict[str, dict[str, object]] = {}
        self._e25_selection: dict[str, int | None] = {}
        self._e25_selection_provenance: dict[str, _ReportedFieldProvenance] = {}
        self._pending_fields: dict[_FieldKey, _PendingField] = {}
        self._reported_fields: dict[tuple[str, _Channel, str], _ReportedFieldProvenance] = {}
        self._has_reported_fields: set[_FieldKey] = set()
        self._locally_commanded_fields: set[_FieldKey] = set()
        self._already_confirmed_tokens: dict[str, _ConfirmedCommandReceipt] = {}
        self._confirmed_spectrum: dict[str, dict[str, int | None]] = {}
        self._spectrum_report_provenance: dict[str, dict[str, _ReportedFieldProvenance]] = {}
        self.manual_preferences: ManualPreferences | None = None
        self._pending_manual_choices: dict[str, _PendingManualChoice] = {}
        self._manual_choice_generation = 0
        self._last_accepted_manual_generation: dict[tuple[str, str], int] = {}
        self._manual_choice_lock = asyncio.Lock()
        self._confirmed_manual_facts: dict[tuple[str, str], dict[str, int | None]] = {}
        self._manual_fact_provenance: dict[tuple[str, str, str], _ReportedFieldProvenance] = {}
        self._a22_reported: dict[tuple[str, str], dict[str, object]] = {}
        self._a22_provenance: dict[tuple[str, str, tuple[str, ...]], _ReportedFieldProvenance] = {}
        self._a22_last: dict[tuple[str, str, tuple[str, ...]], _ReportedFieldProvenance] = {}
        self._a22_reads: dict[str, str] = {}
        self._a22_pending: dict[tuple[str, str, tuple[str, ...]], _A22Pending] = {}
        self._a22_receipts: dict[str, _A22Receipt] = {}
        self._a22_generation: dict[tuple[str, str], int] = {}
        self._a22_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._field_generation = 0
        self._sensor_states: dict[str, dict[str, object]] = {}

        # Reverse lookups for MQTT topic routing
        self._client_id_to_device_id: dict[str, str] = {}
        self._topic_prefix_to_device_id: dict[str, str] = {}

        self._refresh_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._shutdown_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._started = False
        self._last_shadow_refresh_request_at: dict[str, datetime] = {}

    @property
    def device(self) -> DeviceInfo:
        """Return primary device metadata (first controller, or first device)."""
        if not self._devices:
            raise RuntimeError("Device not available before coordinator start")
        for d in self._devices:
            if d.device_type == "controller":
                return d
        return self._devices[0]

    @property
    def devices(self) -> list[DeviceInfo]:
        """Return all discovered devices."""
        return list(self._devices)

    @property
    def camera_devices(self) -> list[DeviceInfo]:
        """Return discovered camera devices."""
        return list(self._camera_devices)

    @property
    def garden_inventory(self) -> GardenInventory | None:
        """Last complete consistent garden/device listing, if one was accepted."""
        return self._garden_inventory

    def garden_plan_read(self, scene_id: int) -> GardenPlanRead:
        """Return per-garden recipe read health, independent of MQTT."""
        return self._garden_reads.get(scene_id, GardenPlanRead(None, "unknown"))

    def _physical_poll_is_current(self, device: DeviceInfo, generation: int, version: int) -> bool:
        """Accept a completed poll only for its still-active inventory and route."""
        return (
            self._inventory_is_current(generation)
            and self._physical_sample_versions.get(device.device_id) == version
            and device in self._all_devices
            and device in self._devices
        )


    def garden_schedule_keys(self, scene_id: int) -> frozenset[str]:
        """Show families with configured schedule slots, independent of equipment."""
        inventory = self._garden_inventory
        read = self.garden_plan_read(scene_id)
        if inventory is None or read.plans is None or read.status not in ("current", "refreshing"):
            return frozenset()
        if not any(g.scene_id == scene_id for g in inventory.gardens):
            return frozenset()
        keys: set[str] = set()
        for plan in read.plans.plans:
            for stage in plan.stages:
                if stage.content_status != "valid" or stage.content is None:
                    continue
                for key in ("light", "cfan", "dfan", "hmdf", "dhmdf", "drip", "heat", "aircd"):
                    family = stage.content.get(key)
                    if isinstance(family, dict) and isinstance(family.get("slot"), list) and family["slot"]:
                        keys.add(key)
        return frozenset(keys)

    def async_add_garden_listener(
        self, callback: Callable[[GardenInventory], None],
    ) -> Callable[[], None]:
        """Deliver accepted gardens and configured recipe updates to platforms."""
        if self._garden_inventory is not None:
            callback(self._garden_inventory)
        self._garden_listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._garden_listeners:
                self._garden_listeners.remove(callback)

        return unsubscribe

    def _notify_garden_listeners(self) -> None:
        inventory = self._garden_inventory
        if inventory is None:
            return
        for listener in tuple(self._garden_listeners):
            try:
                listener(inventory)
            except Exception:
                self._logger.exception("Garden sensor discovery callback failed")

    def set_garden_topology(self, topology: GardenTopology) -> None:
        """Attach HA registry updates after bootstrap and before platforms load."""
        if self._garden_inventory is not None:
            topology.apply(self._garden_inventory, self._all_devices)
        self._garden_topology = topology

    def async_add_discovery_listener(
        self, callback: Callable[[list[DeviceInfo]], None], *, cameras: bool = False,
    ) -> Callable[[], None]:
        """Watch accepted account discoveries; provide current devices immediately."""
        subscription = (callback, cameras)
        callback(self.camera_devices if cameras else self.devices)
        self._device_listeners.append(subscription)

        def unsubscribe() -> None:
            if subscription in self._device_listeners:
                self._device_listeners.remove(subscription)

        return unsubscribe

    @property
    def support_capture_active(self) -> bool:
        """Return whether support capture is active."""
        return self._support_capture.active

    def support_capture_snapshot(self) -> dict[str, object]:
        """Return diagnostics-safe support capture state."""
        return self._support_capture.snapshot()

    def get_device(self, device_id: str) -> DeviceInfo | None:
        """Return a device by ID."""
        for d in self._devices:
            if d.device_id == device_id:
                return d
        return None

    def selected_controls(self, device_id: str) -> frozenset[str]:
        """Resolve E25 controls from last ordered reported port selection."""
        from .controls import selected_controls

        device = self.get_device(device_id)
        return selected_controls(device, self._e25_selection.get(device_id)) if device is not None else frozenset()

    def supports_control(self, device_id: str, control: str) -> bool:
        """Expose selected control eligibility to discovery and retained entities."""
        return control in self.selected_controls(device_id)

    @property
    def is_mqtt_connected(self) -> bool:
        """Return MQTT transport connectivity state."""
        return self._mqtt_client is not None and self._mqtt_client.is_connected

    async def _async_update_data(self) -> dict[str, object]:
        """Poll climate telemetry while MQTT handles device control and push state."""
        await self.async_refresh_inventory()
        await self._refresh_point_log()
        await self._async_refresh_stale_shadow_states()
        return self._build_state_snapshot()

    async def async_start(self) -> None:
        """Bootstrap cloud chain and launch lifecycle workers."""
        async with self._start_lock:
            if self._started:
                return

            self._shutdown_event.clear()
            self._reconnect_event.clear()
            await self._bootstrap_chain()
            await self.async_refresh_inventory(initial_devices=self._all_devices, wait_for_plans=False)
            if (not self._all_devices and not self._capture_devices and not self.skipped_devices_snapshot()
                    and not (self._garden_inventory and self._garden_inventory.gardens)):
                raise VivosunResponseError("No devices found for this account")
            if self._devices:
                self._refresh_task = asyncio.create_task(
                    self._credentials_refresh_loop(),
                    name="vivosun_credentials_refresh",
                )
                self._reconnect_task = asyncio.create_task(
                    self._reconnect_supervisor_loop(),
                    name="vivosun_reconnect_supervisor",
                )
            self._started = True
            self._unsub_inventory_refresh = async_track_time_interval(
                self.hass, self._async_periodic_inventory_refresh, _INVENTORY_REFRESH_INTERVAL,
            )
            self._unsub_garden_tick = async_track_time_interval(
                self.hass, self._async_garden_tick, _GARDEN_TICK_INTERVAL,
            )
            await self._refresh_point_log()
            self.async_set_updated_data(self._build_state_snapshot())

    async def async_shutdown(self) -> None:
        """Cancel workers and close MQTT resources safely."""
        async with self._start_lock:
            self._shutdown_event.set()
            self._reconnect_event.set()
            self._inventory_generation += 1
            if self._forced_inventory_task is not None:
                self._forced_inventory_task.cancel()
                await asyncio.gather(self._forced_inventory_task, return_exceptions=True)
                self._forced_inventory_task = None
            self._debounced_refresh.async_cancel()
            for task in self._garden_read_tasks.values():
                task.cancel()
            self._physical_sample_versions.clear()
            if self._garden_read_tasks:
                await asyncio.gather(*self._garden_read_tasks.values(), return_exceptions=True)
            self._garden_read_tasks.clear()
            self._garden_read_versions.clear()
            self._garden_read_last_attempt.clear()
            self._reported_plan_signatures.clear()
            if self._unsub_inventory_refresh is not None:
                self._unsub_inventory_refresh()
                self._unsub_inventory_refresh = None
            if self._unsub_garden_tick is not None:
                self._unsub_garden_tick()
                self._unsub_garden_tick = None

            tasks = [task for task in (self._refresh_task, self._reconnect_task) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            self._refresh_task = None
            self._reconnect_task = None

            await self.async_stop_support_capture()
            await self._disconnect_mqtt_client()

            self._started = False
            self._garden_inventory = None
            self._garden_topology = None
            self._garden_reads.clear()
            self._garden_listeners.clear()
            self._inventory_last_attempt = None
            self._all_devices.clear()
            self._device_listeners.clear()
            self._tokens = None
            self._aws_identity = None
            self._aws_credentials = None
            self._devices.clear()
            self._camera_devices.clear()
            self._shadow_states.clear()
            self._e25_selection.clear()
            self._e25_selection_provenance.clear()
            for pending in self._pending_fields.values():
                pending.timeout.cancel()
            self._pending_fields.clear()
            self._reported_fields.clear()
            self._has_reported_fields.clear()
            self._locally_commanded_fields.clear()
            self._confirmed_spectrum.clear()
            self._spectrum_report_provenance.clear()
            self._invalidate_a22()
            self._a22_reported.clear()
            self._a22_last.clear()
            self._a22_reads.clear()
            self._last_accepted_manual_generation.clear()
            self._sensor_states.clear()
            self._client_id_to_device_id.clear()
            self._topic_prefix_to_device_id.clear()
            self._last_shadow_refresh_request_at.clear()

    async def async_publish_shadow_update(
        self,
        payload: dict[str, object] | str | bytes,
        *,
        device_id: str | None = None,
        qos: int = 0,
    ) -> None:
        """Publish shadow control payload through the active MQTT session.

        If device_id is None, publishes to the primary device.
        """
        mqtt_client = self._mqtt_client
        if mqtt_client is None or not mqtt_client.is_connected:
            raise MQTTConnectionError("Cannot publish shadow update while MQTT is disconnected")

        encoded: bytes
        if isinstance(payload, dict):
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        elif isinstance(payload, str):
            encoded = payload.encode("utf-8")
        else:
            encoded = payload

        target_device = self._resolve_device(device_id)
        command_token: str | None = None
        if isinstance(payload, dict) and target_device.device_type == "controller":
            state = payload.get("state")
            desired = state.get("desired") if isinstance(state, dict) else None
            requested: list[tuple[_FieldKey, int]] = []
            for channel in _CONTROLLER_CHANNELS:
                part = desired.get(channel) if isinstance(desired, dict) else None
                # Raw light levels need the explicit entity publisher's token.
                # Proven mode-only commands can acquire their own below.
                manual = part.get("manu") if isinstance(part, dict) else None
                raw_level = manual.get("lv") if isinstance(manual, dict) else None
                if isinstance(part, dict):
                    raw_mode = part.get("mode")
                    e42a = client_model_token(target_device.client_id) == "VSCTLE42A"
                    auto_mode = E42A_DFAN_AUTO_MODE if e42a else MODE_AUTO
                    supported = (raw_mode in (0, auto_mode) or (e42a and raw_mode == MODE_CYCLE)
                                 if channel == "dFan" else e42a and raw_mode in (0, MODE_CYCLE))
                    if type(raw_mode) is int and supported:
                        requested.append(((target_device.device_id, channel, "mode"), raw_mode))
                valid_levels = (
                    range(101) if channel == "light"
                    else (*CFAN_LEVEL_MAP, 200) if channel == "cFan" else DFAN_LEVEL_MAP
                )
                if (type(raw_level) is int and raw_level in valid_levels
                        and (channel != "light" or isinstance(payload.get("clientToken"), str))):
                    requested.append(((target_device.device_id, channel, "level"), raw_level))
            if requested:
                token = payload.get("clientToken")
                if not isinstance(token, str):
                    token = uuid4().hex
                    payload = {**payload, "clientToken": token}
                command_token = token
                self._begin_requested_fields(requested, token)
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        topic = TOPIC_SHADOW_UPDATE.format(thing=target_device.client_id)
        try:
            await mqtt_client.publish(topic, encoded, qos=qos)
        except BaseException:
            if command_token is not None:
                self._clear_pending_token(target_device.device_id, command_token)
                self._forget_confirmed_receipt(target_device.device_id, command_token)
            raise
        self._support_capture.record(
            "shadow_update_request",
            data={
                "device_id": target_device.device_id,
                "topic": topic,
                "qos": qos,
                **summarize_support_capture_payload(encoded),
            },
        )

    def a22_settings(self, device_id: str, port: str) -> A22Settings:
        """Read the independently confirmed A22 port projection, excluding desired state."""
        self._check_a22_device(device_id, port)
        return read_a22_settings("VSPLUGA22", self._a22_reported.get((device_id, port)))

    def _check_a22_device(self, device_id: str, port: str) -> None:
        device = self.get_device(device_id)
        if device is None or client_model_token(device.client_id) != "VSPLUGA22" or port not in A22_PORTS:
            raise ServiceValidationError("A22 settings require an exact VSPLUGA22 physical port")

    def _a22_fresh(self, device_id: str, port: str, *paths: tuple[str, ...]) -> bool:
        """Require versioned and timestamped genuine reports for every guarded leaf."""
        return all(
            (proof := self._a22_provenance.get((device_id, port, path))) is not None
            and proof.version is not None and proof.timestamp is not None
            for path in (*_A22_GUARDS, *paths)
        )

    def _a22_available(self, device_id: str) -> None:
        if not self.is_mqtt_connected or self._shadow_connection_state(device_id) is False:
            raise HomeAssistantError("A22 port is unavailable")

    def _a22_require(self, device_id: str, port: str, *paths: tuple[str, ...]) -> A22Settings:
        self._check_a22_device(device_id, port)
        self._a22_available(device_id)
        if not self._a22_fresh(device_id, port, *paths):
            raise ServiceValidationError("A22 command requires fresh genuine reported facts")
        settings = self.a22_settings(device_id, port)
        if settings.status.mode not in (0, 1, 2) or settings.status.in_plan != 0:
            raise ServiceValidationError("A22 command requires known mode and no active plan")
        return settings

    def _a22_pending_key(self, device_id: str, port: str, field: str) -> tuple[str, str, tuple[str, ...]]:
        return (device_id, port, _A22_GROUPS[field])

    async def async_set_a22_manual_power(self, device_id: str, port: str, on: bool) -> None:
        """Route legacy physical power through the same per-port intent generation."""
        settings = self._a22_require(device_id, port, ("lv",))
        if settings.status.is_on is None:
            raise HomeAssistantError("A22 port has no reported output level")
        if not settings.status.can_write:
            raise ServiceValidationError("A22 manual power requires reported Manual and no active plan")
        key = (device_id, port)
        self._a22_generation[key] = self._a22_generation.get(key, 0) + 1
        try:
            await self._publish_a22(
                device_id, port, "manual", {("lv",): 100 if on else 0},
                build_manual_port_payload(port, on), supersede=True,
            )
        except BaseException:
            self._a22_provenance.pop((device_id, port, ("lv",)), None)
            raise

    def _a22_request_read(self, device_id: str) -> None:
        """Request one fresh snapshot after uncertainty, never retry an actuator write."""
        self._a22_reads.pop(device_id, None)

        async def read() -> None:
            try:
                await self._async_request_shadow_refresh(device_id)
            except (MQTTError, TimeoutError):
                self._logger.warning("A22 recovery read unavailable; controls remain guarded")

        if self.is_mqtt_connected and not self._shutdown_event.is_set():
            self.hass.async_create_task(read())

    def _a22_expire(
        self, key: tuple[str, str, tuple[str, ...]], token: str, *, recover: bool = True,
    ) -> None:
        pending = self._a22_pending.get(key)
        if pending is None or pending.token != token:
            return
        pending.timeout.cancel()
        del self._a22_pending[key]
        # An unconfirmed pair cannot justify another write using cached counterparts.
        for path in pending.expected:
            self._a22_provenance.pop((key[0], key[1], path), None)
        if key[2] == _A22_GROUPS["mode"]:
            port_key = (key[0], key[1])
            self._a22_generation[port_key] = self._a22_generation.get(port_key, 0) + 1
        if recover:
            self._a22_request_read(key[0])

    def _a22_forget_receipt(self, token: str) -> _A22Receipt | None:
        receipt = self._a22_receipts.pop(token, None)
        if receipt is not None:
            receipt.timeout.cancel()
        return receipt

    def _a22_reject(self, device_id: str, token: str) -> None:
        """Invalidate only the current request, including one already reported."""
        receipt = self._a22_receipts.get(token)
        if receipt is None or receipt.device_id != device_id:
            return
        self._a22_forget_receipt(token)
        port_key = (device_id, receipt.port)
        for key, pending in tuple(self._a22_pending.items()):
            if pending.token == token and key[:2] == port_key:
                self._a22_expire(key, token, recover=False)
        for path in receipt.paths:
            self._a22_provenance.pop((device_id, receipt.port, path), None)
        if ("lv",) in receipt.paths:
            self._a22_provenance.pop((device_id, receipt.port, ("mode",)), None)
        self._a22_generation[port_key] = self._a22_generation.get(port_key, 0) + 1
        self._a22_request_read(device_id)

    def _a22_accept_ack(self, device_id: str, document: dict[str, object]) -> None:
        """Keep desired acceptance as ordering evidence, not reported confirmation."""
        token = document.get("clientToken")
        version = document.get("version")
        if not isinstance(token, str) or type(version) is not int:
            return
        state = document.get("state")
        desired = state.get("desired") if isinstance(state, dict) else None
        metadata = document.get("metadata")
        desired_meta = metadata.get("desired") if isinstance(metadata, dict) else None
        for key, pending in tuple(self._a22_pending.items()):
            if key[0] != device_id or pending.token != token:
                continue
            part = desired.get(key[1]) if isinstance(desired, dict) else None
            meta = desired_meta.get(key[1]) if isinstance(desired_meta, dict) else None
            accepted: dict[tuple[str, ...], int] = {}
            for path, expected in pending.expected.items():
                desired_path = ("manu", "lv") if key[2] == _A22_GROUPS["manual"] else path
                leaf: object = part
                leaf_meta: object = meta
                for component in desired_path:
                    leaf = leaf.get(component) if isinstance(leaf, dict) else None
                    leaf_meta = leaf_meta.get(component) if isinstance(leaf_meta, dict) else None
                if leaf != expected:
                    break
                stamp = leaf_meta.get("timestamp") if isinstance(leaf_meta, dict) else None
                if type(stamp) is int and stamp >= 0:
                    accepted[path] = stamp
            else:
                pending.accepted_version = version
                pending.accepted_timestamps = accepted
                # Recheck genuine reports that arrived before their desired ACK.
                for path, proof in tuple(pending.reports.items()):
                    self._a22_reconcile_pending(
                        device_id, key[1], path, pending.expected[path], proof.version, proof.timestamp,
                        reported_change=True,
                    )
                self._a22_finish_pending(device_id, key[1])

    def _a22_reconcile_pending(
        self, device_id: str, port: str, path: tuple[str, ...], value: object,
        version: int | None, timestamp: int | None, *, reported_change: bool,
    ) -> None:
        # A desired ACK orders requests but never proves physical execution.
        # A later genuine reported write may share its one-second leaf timestamp.
        for key, pending in tuple(self._a22_pending.items()):
            if key[:2] != (device_id, port) or path not in pending.expected:
                continue
            baseline = pending.baseline[path]
            if reported_change:
                pending.reports[path] = _ReportedFieldProvenance(version, timestamp)
            if value != pending.expected[path]:
                pending.confirmed.discard(path)
                pending.reports.pop(path, None)
            elif (
                reported_change and version is not None and timestamp is not None
                and baseline.version is not None and version > baseline.version
                and baseline.timestamp is not None and timestamp >= baseline.timestamp
                and (pending.accepted_version is None or version > pending.accepted_version)
                and (
                    # Without desired ordering, a later leaf timestamp is required.
                    timestamp > baseline.timestamp if pending.accepted_version is None
                    else timestamp >= (pending.accepted_timestamps or {}).get(path, baseline.timestamp)
                )
            ):
                pending.confirmed.add(path)

    def _a22_finish_pending(self, device_id: str, port: str) -> None:
        """Settle a pair only after every leaf in the same report is reconciled."""
        for key, pending in tuple(self._a22_pending.items()):
            if key[:2] == (device_id, port) and pending.confirmed == pending.expected.keys():
                pending.timeout.cancel()
                del self._a22_pending[key]

    def _a22_recovered_get(
        self, key: tuple[str, str, tuple[str, ...]], value: object,
        previously_reported: object, incoming: _ReportedFieldProvenance,
        last: _ReportedFieldProvenance | None, topic: str, *, correlated_read: bool,
    ) -> bool:
        """Restore read authority from a current unchanged GET, never command confirmation.

        The reported leaf and its timestamp must agree with retained ordering.
        An unchanged snapshot requires the current session's outstanding GET token.
        """
        return (
            correlated_read and topic.endswith("/get/accepted") and key not in self._a22_provenance
            and last is not None and last.version is not None and last.timestamp is not None
            and incoming.version is not None and incoming.timestamp is not None
            and type(value) is int and value == previously_reported
            and incoming.version >= last.version and incoming.timestamp >= last.timestamp
            and not any(key[:2] == pending_key[:2] and key[2] in pending.expected
                        for pending_key, pending in self._a22_pending.items())
        )

    def _invalidate_a22(self, device_id: str | None = None) -> None:
        """Drop command authority on disconnect, cancellation or an uncertain send."""
        for key, pending in tuple(self._a22_pending.items()):
            if device_id is None or key[0] == device_id:
                self._a22_expire(key, pending.token, recover=False)
        for read_device in tuple(self._a22_reads):
            if device_id is None or read_device == device_id:
                del self._a22_reads[read_device]
        for key in tuple(self._a22_provenance):
            if device_id is None or key[0] == device_id:
                del self._a22_provenance[key]
        for token, receipt in tuple(self._a22_receipts.items()):
            if device_id is None or receipt.device_id == device_id:
                self._a22_forget_receipt(token)
        for generation_key in tuple(self._a22_generation):
            if device_id is None or generation_key[0] == device_id:
                self._a22_generation[generation_key] += 1

    async def _publish_a22(
        self, device_id: str, port: str, field: str, expected: dict[tuple[str, ...], int],
        payload: dict[str, object], *, supersede: bool = False,
    ) -> str:
        group = self._a22_pending_key(device_id, port, field)
        previous = self._a22_pending.get(group)
        baseline: dict[tuple[str, ...], _ReportedFieldProvenance] = {}
        confirmed: set[tuple[str, ...]] = set()
        reported = self._a22_reported.get((device_id, port), {})
        already_reported = True
        for path, value in expected.items():
            proof = self._a22_provenance.get((device_id, port, path))
            if proof is None or proof.version is None or proof.timestamp is None:
                raise ServiceValidationError("A22 command requires fresh genuine reported facts")
            baseline[path] = proof
            observed: object = reported
            for part in path:
                observed = observed.get(part) if isinstance(observed, dict) else None
            if observed != value:
                already_reported = False
            if observed == value and len(expected) > 1:
                # The unchanged mate was already genuinely reported before this edit.
                confirmed.add(path)
        if previous is not None and not supersede and not already_reported:
            raise ServiceValidationError("A22 paired setting has a pending reported update")
        if previous is not None:
            previous.timeout.cancel()
            del self._a22_pending[group]
        # Forget old same-group receipts only after the replacement can be staged.
        for old_token, receipt in tuple(self._a22_receipts.items()):
            if (receipt.device_id, receipt.port, receipt.group) == group:
                self._a22_forget_receipt(old_token)
        token = uuid4().hex
        if not already_reported or previous is not None:
            timeout = asyncio.get_running_loop().call_later(
                _FIELD_CONFIRMATION_TIMEOUT, self._a22_expire, group, token,
            )
            if previous is not None:
                # A replacement still needs readback for fields the older in-flight
                # request could change; the untouched mate retains its valid proof.
                confirmed = {
                    path for path in confirmed if previous.expected.get(path) == expected[path]
                }
            self._a22_pending[group] = _A22Pending(
                token, expected, baseline, confirmed, {}, timeout,
            )
        receipt_timeout = asyncio.get_running_loop().call_later(
            _FIELD_CONFIRMATION_TIMEOUT, self._a22_forget_receipt, token,
        )
        self._a22_receipts[token] = _A22Receipt(
            device_id, port, group[2], tuple(expected), receipt_timeout,
        )
        try:
            await self.async_publish_shadow_update(
                {**payload, "clientToken": token}, device_id=device_id, qos=1,
            )
        except BaseException:
            self._a22_expire(group, token, recover=False)
            self._a22_forget_receipt(token)
            self._a22_request_read(device_id)
            raise
        return token

    async def async_set_a22_setting(
        self, device_id: str, port: str, kind: str, field: str, value: int,
    ) -> None:
        """Publish one Cycle/Auto setting with both reported counterpart facts guarded."""
        if kind == "cycle" and field in ("tOffset", "onDur", "offDur"):
            paths = (("cycle", "onDur"), ("cycle", "offDur")) if field != "tOffset" else (("cycle", "tOffset"),)
            builder = build_a22_cycle_setting
        elif kind == "auto" and field in ("tMin", "tMax", "hMin", "hMax"):
            paths = (("auto", "tMin"), ("auto", "tMax")) if field.startswith("t") else (
                ("auto", "hMin"), ("auto", "hMax"),
            )
            builder = build_a22_auto_setting
        else:
            raise ServiceValidationError("Unsupported A22 setting")
        settings = self._a22_require(device_id, port, *paths)
        if (device_id, port, _A22_GROUPS["mode"]) in self._a22_pending:
            raise ServiceValidationError("A22 mode has a pending reported update")
        try:
            payload = builder("VSPLUGA22", port, field, value, settings, fresh=True)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        state = cast("dict[str, object]", payload["state"])
        changes = cast("dict[str, object]", state["desired"])
        target = cast("dict[str, object]", changes[port])
        expected: dict[tuple[str, ...], int] = {}
        for path in paths:
            value_at_path: object = target
            for part in path:
                value_at_path = value_at_path.get(part) if isinstance(value_at_path, dict) else None
            if type(value_at_path) is not int:
                raise ServiceValidationError("A22 setting payload is invalid")
            expected[path] = value_at_path
        await self._publish_a22(device_id, port, field, expected, payload)

    async def async_set_a22_mode(self, device_id: str, port: str, mode: int) -> None:
        """Follow firmware's two-write Manual exit without waiting for a cloud ACK."""
        key = (device_id, port)
        self._check_a22_device(device_id, port)
        if type(mode) is not int or mode not in (0, 1, 2):
            raise ServiceValidationError("Unsupported A22 mode")
        self._a22_generation[key] = self._a22_generation.get(key, 0) + 1
        generation = self._a22_generation[key]
        async with self._a22_locks.setdefault(key, asyncio.Lock()):
            if self._a22_generation[key] != generation:
                raise ServiceValidationError("A22 mode request superseded")
            settings = self._a22_require(device_id, port, ("lv",) if mode == 0 else ("mode",))
            try:
                change = make_a22_mode_change("VSPLUGA22", port, mode, settings, fresh=True)
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            if (change.preserved_level is not None
                    and self._a22_pending_key(device_id, port, "manual") in self._a22_pending):
                raise ServiceValidationError("A22 output has a pending reported update")
            token = await self._publish_a22(
                device_id, port, "mode", {("mode",): mode}, change.command, supersede=True,
            )
            if (change.preserved_level is None or self._a22_generation[key] != generation
                    or token not in self._a22_receipts):
                return
            # Never infer a reported mode or output from the desired acknowledgement.
            latest = self._a22_require(device_id, port, ("lv",))
            try:
                second = finish_a22_manual_change("VSPLUGA22", port, change, latest, fresh=True)
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            try:
                await self._publish_a22(
                    device_id, port, "manual", {("lv",): change.preserved_level}, second,
                )
            except BaseException:
                self._a22_provenance.pop((device_id, port, ("mode",)), None)
                raise

    def _record_a22_reported(
        self, device_id: str, document: dict[str, object], topic: str,
    ) -> None:
        """Apply only newer, genuine A22 leaf reports and retain independent write proof."""
        device = self.get_device(device_id)
        if device is None or client_model_token(device.client_id) != "VSPLUGA22":
            return
        reported = extract_shadow_reported(document)
        if not isinstance(reported, dict):
            return
        if reported.get("connected") is False:
            self._invalidate_a22(device_id)
            return
        token = document.get("clientToken")
        correlated_read = (
            topic.endswith("/get/accepted") and isinstance(token, str)
            and token == self._a22_reads.get(device_id)
        )
        if correlated_read:
            del self._a22_reads[device_id]
        body = document.get("current", document)
        if not isinstance(body, dict):
            return
        raw_version = body.get("version")
        version = raw_version if type(raw_version) is int and raw_version >= 0 else None
        metadata = body.get("metadata")
        metadata = metadata.get("reported") if isinstance(metadata, dict) else None
        for port in A22_PORTS:
            if port not in reported:
                continue
            raw = reported[port]
            port_meta = metadata.get(port) if isinstance(metadata, dict) else None
            current = self._a22_reported.setdefault((device_id, port), {})
            if not isinstance(raw, dict):
                # A whole-port null/invalid report clears every previously known child.
                if not self._a22_accept_parent(device_id, port, version, port_meta, topic):
                    continue
                current.clear()
            else:
                # Keep accessory/manual metadata and unrelated firmware settings across sparse reports.
                for extra, value in raw.items():
                    if extra not in _A22_STATE_ROOTS:
                        if isinstance(value, dict) and isinstance(current.get(extra), dict):
                            _deep_merge_mapping(cast("dict[str, object]", current[extra]), value)
                        else:
                            current[extra] = deepcopy(value)
                for block_name in ("cycle", "auto"):
                    block = raw.get(block_name)
                    if not isinstance(block, dict):
                        continue
                    nested = current.setdefault(block_name, {})
                    if not isinstance(nested, dict):
                        nested = current[block_name] = {}
                    for name, value in block.items():
                        if (block_name, name) not in _A22_FIELD_PATHS:
                            nested[name] = deepcopy(value)
            cleared_blocks: set[str] = set()
            if isinstance(raw, dict):
                for block_name in ("cycle", "auto"):
                    if block_name not in raw or isinstance(raw[block_name], dict):
                        continue
                    block_meta = port_meta.get(block_name) if isinstance(port_meta, dict) else None
                    if self._a22_accept_parent(
                        device_id, port, version, block_meta, topic, prefix=block_name,
                    ):
                        # Check the complete block before removing any of its children.
                        current.pop(block_name, None)
                        cleared_blocks.add(block_name)
            for path in _A22_FIELD_PATHS:
                leaf: object = raw
                meta: object = port_meta
                if isinstance(raw, dict):
                    parent = raw.get(path[0])
                    if path[0] not in raw or (len(path) == 2 and isinstance(parent, dict)
                                                  and path[1] not in parent):
                        continue
                    if len(path) == 2 and not isinstance(parent, dict) and path[0] not in cleared_blocks:
                        continue
                for part in path:
                    leaf = leaf.get(part) if isinstance(leaf, dict) else None
                    meta = meta.get(part) if isinstance(meta, dict) else None
                timestamp_raw = meta.get("timestamp") if isinstance(meta, dict) else None
                if timestamp_raw is None and (not isinstance(raw, dict) or (
                    len(path) == 2 and not isinstance(raw.get(path[0]), dict)
                )):
                    parent_meta = port_meta if not isinstance(raw, dict) else (
                        port_meta.get(path[0]) if isinstance(port_meta, dict) else None
                    )
                    timestamp_raw = parent_meta.get("timestamp") if isinstance(parent_meta, dict) else None
                timestamp = timestamp_raw if type(timestamp_raw) is int and timestamp_raw >= 0 else None
                incoming = _ReportedFieldProvenance(version, timestamp)
                key = (device_id, port, path)
                last = self._a22_last.get(key)
                previously_reported: object = current
                for part in path:
                    previously_reported = (
                        previously_reported.get(part) if isinstance(previously_reported, dict) else None
                    )
                if not (_reported_field_is_fresh(last, incoming, topic) or self._a22_recovered_get(
                    key, leaf, previously_reported, incoming, last, topic, correlated_read=correlated_read,
                )):
                    continue
                if len(path) == 1:
                    current[path[0]] = deepcopy(leaf)
                elif path[0] not in cleared_blocks:
                    nested = current.setdefault(path[0], {})
                    if isinstance(nested, dict):
                        nested[path[1]] = deepcopy(leaf)
                # Preserve the last known timestamp when a newer report omits metadata.
                self._a22_last[key] = _ReportedFieldProvenance(
                    version if version is not None else last.version if last else None,
                    timestamp if timestamp is not None else last.timestamp if last else None,
                )
                if version is not None and timestamp is not None and type(leaf) is int:
                    self._a22_provenance[key] = incoming
                else:
                    self._a22_provenance.pop(key, None)
                reported_change = topic.endswith("/update/accepted") or (
                    topic.endswith("/update/documents")
                    and _a22_documents_changed_leaf(document, port, path, leaf, timestamp)
                )
                self._a22_reconcile_pending(
                    device_id, port, path, leaf, version, timestamp, reported_change=reported_change,
                )
            self._a22_finish_pending(device_id, port)

    def _a22_accept_parent(
        self, device_id: str, port: str, version: int | None, metadata: object, topic: str,
        *, prefix: str | None = None,
    ) -> bool:
        """A whole-port or nested clear must be later than every affected leaf."""
        stamp = metadata.get("timestamp") if isinstance(metadata, dict) else None
        timestamp = stamp if type(stamp) is int and stamp >= 0 else None
        return all(
            _reported_field_is_fresh(last, _ReportedFieldProvenance(version, timestamp), topic)
            for (known_device, known_port, path), last in self._a22_last.items()
            if (known_device, known_port) == (device_id, port)
            and (prefix is None or path[0] == prefix)
        )

    def manual_memory_enabled(self, device_id: str, channel: str) -> bool:
        """Check the global option and supported controller channel."""
        device = self.get_device(device_id)
        preferences = self.manual_preferences
        return bool(
            device is not None and device.device_type == "controller"
            and preferences is not None and preferences.enabled(device_id, channel)
        )

    def remembered_manual_selection(self, device_id: str, channel: str) -> ManualSelection | None:
        """Return a remembered numeric or native preset when memory is enabled."""
        preferences = self.manual_preferences
        if preferences is None or not self.manual_memory_enabled(device_id, channel):
            return None
        return preferences.selection(device_id, channel)

    def manual_restore_allowed(self, device_id: str, channel: str) -> bool:
        """Require actual, fresh per-component Manual and no-plan reports."""
        facts = self._confirmed_manual_facts.get((device_id, channel), {})
        return facts.get("mode") == 0 and facts.get("inPlan") == 0

    def reported_operation_mode(self, device_id: str, channel: str) -> int | None:
        """Return a genuine reported mode, excluding desired-only startup fallback."""
        return self._confirmed_manual_facts.get((device_id, channel), {}).get("mode")

    def _stage_manual_choice(
        self, device_id: str, channel: str, selection: ManualSelection, shadow_level: int, token: str,
    ) -> None:
        """Keep a selected value pending until the cloud accepts that exact write."""
        self._manual_choice_generation += 1
        generation = self._manual_choice_generation
        timeout = asyncio.get_running_loop().call_later(
            _FIELD_CONFIRMATION_TIMEOUT, self._discard_manual_choice, token, device_id,
        )
        self._pending_manual_choices[token] = _PendingManualChoice(
            device_id, channel, selection, shadow_level, generation, timeout,
        )

    def _discard_manual_choice(self, token: str, device_id: str) -> bool:
        candidate = self._pending_manual_choices.get(token)
        if candidate is None or candidate.device_id != device_id:
            return False
        candidate.timeout.cancel()
        del self._pending_manual_choices[token]
        return True

    async def _accept_manual_choice(self, device_id: str, document: dict[str, object]) -> None:
        """Only a matching desired update/accepted can persist explicit HA intent."""
        token = document.get("clientToken")
        if not isinstance(token, str):
            return
        candidate = self._pending_manual_choices.get(token)
        if candidate is None or candidate.device_id != device_id:
            return
        state = document.get("state")
        desired = state.get("desired") if isinstance(state, dict) else None
        component = desired.get(candidate.channel) if isinstance(desired, dict) else None
        manual = component.get("manu") if isinstance(component, dict) else None
        if not isinstance(manual, dict) or manual.get("lv") != candidate.shadow_level:
            return
        self._discard_manual_choice(token, device_id)
        preferences = self.manual_preferences
        if preferences is None or not self.manual_memory_enabled(device_id, candidate.channel):
            return
        key = (candidate.device_id, candidate.channel)
        async with self._manual_choice_lock:
            if candidate.generation <= self._last_accepted_manual_generation.get(key, 0):
                return
            self._last_accepted_manual_generation[key] = candidate.generation
            await preferences.async_remember(candidate.device_id, candidate.channel, candidate.selection)

    async def async_publish_selected_fan_level(
        self, *, device_id: str, channel: str, shadow_level: int,
    ) -> None:
        """Tokenize explicit numeric speed or circulation Natural Wind selections."""
        values = CFAN_LEVEL_MAP if channel == "cFan" else DFAN_LEVEL_MAP if channel == "dFan" else ()
        is_natural_wind = channel == "cFan" and shadow_level == 200
        if not is_natural_wind and shadow_level not in values[1:]:
            raise ValueError("Unsupported manually selected fan level")
        if not self.manual_memory_enabled(device_id, channel):
            raise ValueError("Manual fan setting memory is not enabled")
        builder = build_cfan_level_payload if channel == "cFan" else build_dfan_level_payload
        token = uuid4().hex
        selection: ManualSelection = "natural_wind" if is_natural_wind else values.index(shadow_level) * 10
        self._stage_manual_choice(device_id, channel, selection, shadow_level, token)
        try:
            await self.async_publish_shadow_update(
                {**builder(shadow_level), "clientToken": token}, device_id=device_id,
            )
        except BaseException:
            self._discard_manual_choice(token, device_id)
            raise

    async def async_publish_light_level(
        self, level: int, *, device_id: str, remember_selection: bool = False,
    ) -> None:
        """Send a local controller light command with immediate provisional feedback."""
        if not self.is_mqtt_connected:
            raise MQTTConnectionError("Cannot publish shadow update while MQTT is disconnected")
        device = self._resolve_device(device_id)
        if device.device_type != "controller":
            raise VivosunResponseError("Light level commands require a controller")
        token = uuid4().hex
        payload: dict[str, object] = {**build_light_level_payload(level), "clientToken": token}
        requested = clamp_light_level(level)
        if remember_selection and requested > 0 and self.manual_memory_enabled(device_id, "light"):
            self._stage_manual_choice(device_id, "light", requested, requested, token)
        try:
            await self.async_publish_shadow_update(payload, device_id=device_id)
        except BaseException:
            self._discard_manual_choice(token, device_id)
            raise

    def _stage_confirmed_receipt(self, device_id: str, channel: str, token: str) -> None:
        """Keep a short rejection receipt without claiming the output changed."""
        timeout = asyncio.get_running_loop().call_later(
            _FIELD_CONFIRMATION_TIMEOUT, self._forget_confirmed_receipt, device_id, token,
        )
        self._already_confirmed_tokens[token] = _ConfirmedCommandReceipt(device_id, channel, timeout)

    def _forget_confirmed_receipt(self, device_id: str, token: str) -> str | None:
        receipt = self._already_confirmed_tokens.get(token)
        if receipt is None or receipt.device_id != device_id:
            return None
        receipt.timeout.cancel()
        del self._already_confirmed_tokens[token]
        return receipt.channel

    def _begin_requested_fields(self, requested: list[tuple[_FieldKey, int]], token: str) -> None:
        """Stage one local command atomically without changing reported cache."""
        changed = False
        confirmed_receipt: tuple[str, str] | None = None
        requested_keys = {key for key, _ in requested}
        for device_id, channel, field in requested_keys:
            if field == "mode":
                level_key: _FieldKey = (device_id, channel, "level")
                if level_key not in requested_keys:
                    # A later mode-only preset leaves actual output in charge.
                    # An older manual speed request is no longer local intent.
                    old_level = self._pending_fields.pop(level_key, None)
                    if old_level is not None:
                        old_level.timeout.cancel()
                        changed = True
        for key, value in requested:
            device_id, channel, field = key
            self._locally_commanded_fields.add(key)
            confirmed = self._shadow_states.get(device_id, {}).get(channel)
            current = confirmed.get(field) if isinstance(confirmed, dict) else None
            if key in self._has_reported_fields and key not in self._pending_fields and current == value:
                confirmed_receipt = (device_id, channel)
                continue
            # Desired-only startup fallback is not confirmed physical readback.
            if key not in self._has_reported_fields and isinstance(confirmed, dict):
                confirmed.pop(field, None)
                if field == "level" and channel != "dFan":
                    confirmed.pop("manual_level", None)
            previous = self._pending_fields.pop(key, None)
            superseded: frozenset[int] = frozenset()
            if previous is not None:
                previous.timeout.cancel()
                superseded = previous.superseded_values | {previous.value}
            self._field_generation += 1
            generation = self._field_generation
            baseline = confirmed.get(field) if isinstance(confirmed, dict) else None
            provenance = self._reported_fields.get(key)
            timeout = asyncio.get_running_loop().call_later(
                _FIELD_CONFIRMATION_TIMEOUT, self._expire_pending_field, key, generation,
            )
            self._pending_fields[key] = _PendingField(
                generation, token, value, baseline if type(baseline) is int else None,
                provenance.version if provenance else None,
                provenance.timestamp if provenance else None, superseded, timeout,
            )
            changed = True
        if confirmed_receipt is not None:
            self._stage_confirmed_receipt(*confirmed_receipt, token)
        if changed:
            self._notify_shadow_changed()

    def _clear_pending_field(self, key: _FieldKey, token: str, *, notify: bool = True) -> bool:
        pending = self._pending_fields.get(key)
        if pending is None or pending.token != token:
            return False
        pending.timeout.cancel()
        del self._pending_fields[key]
        if notify:
            self._notify_shadow_changed()
        return True

    def _clear_pending_token(self, device_id: str, token: str) -> set[str]:
        """Drop only matching in-flight fields, keeping newer local intent."""
        cleared: set[str] = set()
        for key, pending in tuple(self._pending_fields.items()):
            if key[0] == device_id and pending.token == token:
                self._clear_pending_field(key, token, notify=False)
                cleared.add(key[1])
        if cleared:
            self._notify_shadow_changed()
        return cleared

    def _expire_pending_field(self, key: _FieldKey, generation: int) -> None:
        pending = self._pending_fields.get(key)
        if pending is None or pending.generation != generation:
            return
        command = "Light command" if key[1] == "light" else f"{key[1]} {key[2]} command"
        self._logger.warning(
            "%s remains unconfirmed after %s seconds", command, _FIELD_CONFIRMATION_TIMEOUT,
        )
        self._clear_pending_field(key, pending.token)

    def _reconcile_pending_field(
        self, key: _FieldKey, value: object, version: int | None, timestamp: int | None,
        *, reported_change: bool,
    ) -> None:
        pending = self._pending_fields.get(key)
        if pending is None:
            return
        if (
            _fresh_field_report(pending, version, timestamp, reported_change=reported_change)
            and (value == pending.value or value not in pending.superseded_values)
        ):
            self._clear_pending_field(key, pending.token, notify=False)

    def _accept_pending_fields(self, device_id: str, document: dict[str, object]) -> None:
        """Cloud desired ACK establishes ordering, never physical confirmation."""
        token = document.get("clientToken")
        raw_version = document.get("version")
        if not isinstance(token, str) or type(raw_version) is not int:
            return
        state = document.get("state")
        desired = state.get("desired") if isinstance(state, dict) else None
        for key, pending in self._pending_fields.items():
            if key[0] != device_id or pending.token != token:
                continue
            _, channel, field = key
            component = desired.get(channel) if isinstance(desired, dict) else None
            if not isinstance(component, dict):
                continue
            if field == "mode":
                requested = component.get("mode")
            else:
                manual = component.get("manu")
                requested = manual.get("lv") if isinstance(manual, dict) else None
            if requested != pending.value:
                continue
            pending.accepted_version = raw_version
            pending.accepted_timestamp = _desired_field_timestamp(document, channel, field)

    def _reconcile_reported_fields(
        self, device_id: str, document: dict[str, object], parsed: ShadowV1State, topic: str,
    ) -> None:
        """One readback/freshness path for supported controller fields."""
        body = document.get("current", document)
        if not isinstance(body, dict):
            return
        raw_version = body.get("version")
        version = raw_version if type(raw_version) is int else None
        reported = extract_shadow_reported(document)
        device = self.get_device(device_id)
        e42a = device is not None and client_model_token(device.client_id) == "VSCTLE42A"
        for channel in _CONTROLLER_CHANNELS:
            component = reported.get(channel) if isinstance(reported, dict) else None
            if not isinstance(component, dict):
                # Legacy desired-only startup values remain visible until
                # a real report or local command establishes the boundary.
                # Duct output and mode must always come from genuine reported state.
                if channel == "dFan" or any(
                    key[0] == device_id and key[1] == channel
                    for key in self._has_reported_fields | self._locally_commanded_fields
                ):
                    cast("dict[str, object]", parsed).pop(channel, None)
                continue
            projected = parsed.get(channel)
            if not isinstance(projected, dict):
                continue
            self._reconcile_reported_cycle(
                device_id, channel, body, cast("dict[str, object]", projected), version, topic,
            )
            # Retain the original light rule for an older whole-shadow version.
            light_provenance = self._reported_fields.get((device_id, "light", "level"))
            if (
                channel == "light" and light_provenance is not None
                and version is not None and light_provenance.version is not None
                and version < light_provenance.version
            ):
                cast("dict[str, object]", parsed)["light"] = {
                    key: value for key, value in projected.items() if key == "cycle"
                }
                continue
            fields: tuple[_Field, ...] = (
                ("level", "mode", "manual_level") if channel == "dFan"
                else ("level", "mode") if e42a else ("level",)
            )
            for field in fields:
                if field not in projected:
                    continue
                key = (device_id, channel, field)
                value = projected[field]
                timestamp = _reported_field_timestamp(body, channel, field)
                last = self._reported_fields.get(key)
                confirmed = self._shadow_states.get(device_id, {}).get(channel)
                previous_value = confirmed.get(field) if isinstance(confirmed, dict) else None
                pending = self._pending_fields.get(key)
                reported_change = topic.endswith("/update/accepted") or (
                    topic.endswith("/update/documents")
                    and _documents_changed_reported_field(document, channel, field, value, timestamp)
                )
                ambiguous_get = pending is not None and topic.endswith("/get/accepted") and (
                    timestamp is None or (
                        pending.accepted_version is not None and version is not None
                        and version <= pending.accepted_version
                    )
                )
                stale = last is not None and (
                    (version is not None and last.version is not None and version < last.version)
                    or (timestamp is not None and last.timestamp is not None and (
                        timestamp < last.timestamp or (
                            timestamp == last.timestamp and value != previous_value and not reported_change
                        )
                    ))
                )
                if ambiguous_get or stale:
                    cast("dict[str, object]", projected).pop(field, None)
                    if field == "level" and channel != "dFan":
                        cast("dict[str, object]", projected).pop("manual_level", None)
                    continue
                # Missing metadata cannot erase previously known leaf ordering.
                self._reported_fields[key] = _ReportedFieldProvenance(
                    version if version is not None else last.version if last else None,
                    timestamp if timestamp is not None else last.timestamp if last else None,
                )
                self._has_reported_fields.add(key)
                self._reconcile_pending_field(key, value, version, timestamp, reported_change=reported_change)

    def _reconcile_reported_cycle(
        self, device_id: str, channel: _Channel, body: dict[str, object],
        projected: dict[str, object], version: int | None, topic: str,
    ) -> None:
        """Merge only fresh genuine Cycle leaves, including explicit null tombstones."""
        if "cycle" not in projected:
            return
        cycle = projected["cycle"]
        updates = (
            {leaf: cycle[leaf] for leaf in _CYCLE_READOUT_FIELDS[channel] if leaf in cycle}
            if isinstance(cycle, dict) else dict.fromkeys(_CYCLE_READOUT_FIELDS[channel])
        )
        metadata = body.get("metadata")
        reported_meta = metadata.get("reported") if isinstance(metadata, dict) else None
        channel_meta = reported_meta.get(channel) if isinstance(reported_meta, dict) else None
        cycle_meta = channel_meta.get("cycle") if isinstance(channel_meta, dict) else None
        for leaf in tuple(updates):
            leaf_meta = cycle_meta.get(leaf) if isinstance(cycle_meta, dict) else None
            raw_stamp = (leaf_meta.get("timestamp") if isinstance(leaf_meta, dict) else
                         cycle_meta.get("timestamp") if isinstance(cycle_meta, dict) else None)
            stamp = raw_stamp if type(raw_stamp) is int and raw_stamp >= 0 else None
            key = (device_id, channel, f"cycle.{leaf}")
            last = self._reported_fields.get(key)
            incoming = _ReportedFieldProvenance(version, stamp)
            if not _reported_field_is_fresh(last, incoming, topic):
                del updates[leaf]
                continue
            # Missing metadata must not erase earlier leaf ordering evidence.
            self._reported_fields[key] = _ReportedFieldProvenance(
                version if version is not None else last.version if last else None,
                stamp if stamp is not None else last.timestamp if last else None,
            )
        if updates:
            projected["cycle"] = updates
        else:
            projected.pop("cycle")

    def _notify_shadow_changed(self) -> None:
        self.data = self._build_state_snapshot()
        self._notify_garden_listeners()
        self.async_update_listeners()

    def _record_e25_selection(self, device_id: str, document: dict[str, object], topic: str) -> None:
        """Accept only ordered reported ctrlTyp, not desired or projected component blocks."""
        reported = extract_shadow_reported(document)
        if not isinstance(reported, dict) or "ctrlTyp" not in reported:
            return
        body = document.get("current", document)
        if not isinstance(body, dict):
            return
        raw_version = body.get("version")
        version = raw_version if type(raw_version) is int and raw_version >= 0 else None
        metadata = body.get("metadata")
        metadata = metadata.get("reported") if isinstance(metadata, dict) else None
        field = metadata.get("ctrlTyp") if isinstance(metadata, dict) else None
        raw_timestamp = field.get("timestamp") if isinstance(field, dict) else None
        timestamp = raw_timestamp if type(raw_timestamp) is int and raw_timestamp >= 0 else None
        previous = self._e25_selection_provenance.get(device_id)
        incoming = _ReportedFieldProvenance(version, timestamp)
        # A newer shadow version orders selection changes within the same second.
        if not _reported_field_is_fresh(previous, incoming, topic) and not (
            previous is not None and previous.version is not None
            and version is not None and version > previous.version
            and timestamp is not None and timestamp == previous.timestamp
        ):
            return
        selection = reported["ctrlTyp"]
        self._e25_selection_provenance[device_id] = incoming
        # Malformed data keeps the last selection; unknown integers map to no controls.
        if type(selection) is not int:
            return
        changed = self._e25_selection.get(device_id) != selection
        self._e25_selection[device_id] = selection
        if changed:
            for callback, cameras_only in tuple(self._device_listeners):
                if not cameras_only:
                    try:
                        callback(self.devices)
                    except Exception:
                        self._logger.exception("E25 control discovery callback failed")

    def _resolve_device(self, device_id: str | None) -> DeviceInfo:
        """Resolve a device_id to DeviceInfo, defaulting to primary."""
        if device_id is None:
            return self.device
        for d in self._devices:
            if d.device_id == device_id:
                return d
        raise VivosunResponseError(f"Device {device_id} not found")

    async def _bootstrap_chain(self) -> None:
        """Run strict API -> AWS -> MQTT bootstrap order."""
        self._logger.info("Starting Vivosun coordinator bootstrap")
        self._tokens = await self._api.login(self._email, self._password)
        all_devices = await self._api.get_devices(self._tokens)
        self._all_devices = list(all_devices)
        self._capture_devices = list({device.client_id: device for device in self._api.capture_devices}.values())
        self._camera_devices = [device for device in all_devices if device.device_type == "camera"]
        self._devices = self._select_devices(all_devices)
        self._build_topic_maps()
        if not self._devices:
            self._logger.info("No entity-capable MQTT devices; retaining discovery diagnostics without MQTT setup")
            return
        self._aws_identity = await self._api.get_aws_identity(self._tokens)
        self._aws_credentials = await self._aws_auth.get_credentials_for_identity(self._aws_identity)
        await self._connect_mqtt()

    async def _async_periodic_inventory_refresh(self, _now: datetime) -> None:
        """Poll account inventory without relying on entity platform listeners."""
        await self.async_refresh_inventory()

    async def _async_garden_tick(self, _now: datetime) -> None:
        """Re-evaluate stage labels and refresh gardens independently of account inventory."""
        if not self._shutdown_event.is_set() and self._garden_inventory is not None:
            self._notify_shadow_changed()
            for garden in self._garden_inventory.gardens:
                self._queue_garden_read(garden.scene_id, periodic=True)


    def _queue_garden_read(self, scene_id: int, *, periodic: bool = False) -> asyncio.Task[None] | None:
        """Coalesce per-garden plan invalidations without blocking MQTT handling."""
        if self._shutdown_event.is_set() or self._tokens is None or self._garden_inventory is None:
            return None
        if not any(g.scene_id == scene_id for g in self._garden_inventory.gardens):
            return None
        if periodic:
            last = self._garden_read_last_attempt.get(scene_id)
            if last is not None and datetime.now(tz=UTC) - last < _GARDEN_READ_INTERVAL:
                return None
        else:
            self._garden_read_versions[scene_id] = self._garden_read_versions.get(scene_id, 0) + 1
        task = self._garden_read_tasks.get(scene_id)
        if task is None or task.done():
            task = asyncio.create_task(self._run_garden_reads(scene_id))
            self._garden_read_tasks[scene_id] = task
            task.add_done_callback(partial(self._garden_read_done, scene_id))
        return task



    def _garden_read_done(self, scene_id: int, task: asyncio.Task[None]) -> None:
        if self._garden_read_tasks.get(scene_id) is task:
            self._garden_read_tasks.pop(scene_id, None)

    async def _run_garden_reads(self, scene_id: int) -> None:
        """Serialize a garden across all triggers; bound account-wide read concurrency."""
        await asyncio.sleep(_GARDEN_EVENT_COALESCE_SECONDS)
        while not self._shutdown_event.is_set():
            tokens = self._tokens
            if tokens is None or self._garden_inventory is None or not any(
                g.scene_id == scene_id for g in self._garden_inventory.gardens
            ):
                return
            async with self._garden_read_limit:
                # Capture validity after waiting for the shared slot, so an
                # event while queued does not cause an unnecessary extra read.
                generation = self._inventory_generation
                version = self._garden_read_versions.get(scene_id, 0)
                self._garden_read_last_attempt[scene_id] = datetime.now(tz=UTC)
                try:
                    plans = await self._authenticated_read(lambda auth: self._api.get_garden_plans(auth, scene_id))
                    if plans.scene_id != scene_id:
                        raise VivosunResponseError("Garden plan identity mismatch")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._logger.warning(
                        "Garden recipe read unavailable (%s); keeping last valid result",
                        _garden_read_failure_category(error),
                    )
                    plans = None
            async with self._inventory_apply_lock:
                if self._shutdown_event.is_set() or self._garden_inventory is None or not any(
                    g.scene_id == scene_id for g in self._garden_inventory.gardens
                ):
                    return
                if not self._inventory_is_current(generation) or self._garden_read_versions.get(scene_id, 0) != version:
                    # A later inventory/event must be read after this response.
                    # Neither a stale success nor stale failure may win.
                    continue
                previous = self._garden_reads.get(scene_id)
                self._garden_reads[scene_id] = GardenPlanRead(
                    plans if plans is not None else previous.plans if previous is not None else None,
                    "current" if plans is not None else "stale",
                )
                self._notify_shadow_changed()
                return

    async def async_request_refresh(self) -> None:
        """The explicit HA refresh path also checks inventory, regardless of cadence."""
        if self._shutdown_event.is_set():
            return
        task = self._forced_inventory_task
        if task is None or task.done():
            task = self._forced_inventory_task = asyncio.create_task(self.async_refresh_inventory(force=True))
        await asyncio.shield(task)
        if self._shutdown_event.is_set():
            return
        await super().async_request_refresh()

    async def async_refresh_inventory(
        self, *, force: bool = False, initial_devices: list[DeviceInfo] | None = None,
        wait_for_plans: bool = True,
    ) -> None:
        """Observe account garden membership without writes or destructive discovery."""
        tokens = self._tokens
        if tokens is None or self._shutdown_event.is_set():
            return
        now = datetime.now(tz=UTC)
        if (not force and self._inventory_last_attempt is not None
                and now - self._inventory_last_attempt < _INVENTORY_REFRESH_INTERVAL):
            return
        self._inventory_last_attempt = now
        self._inventory_generation += 1
        generation = self._inventory_generation
        try:
            inventory = await self._authenticated_read(self._api.get_gardens)
            devices = list(initial_devices if initial_devices is not None
                           else await self._authenticated_read(self._api.get_devices))
            garden_membership(inventory, devices)
            async with self._inventory_apply_lock:
                if not self._inventory_is_current(generation):
                    return
                await self._accept_inventory(inventory, devices, generation)
            if self._inventory_is_current(generation):
                await self._refresh_garden_plans(inventory, generation, wait=wait_for_plans)
        except asyncio.CancelledError:
            # A cancelled read has no successful outcome. Do not leave its
            # previous snapshot labelled as an active in-flight refresh.
            if self._inventory_is_current(generation):
                for scene_id, read in tuple(self._garden_reads.items()):
                    if read.status == "refreshing":
                        self._garden_reads[scene_id] = GardenPlanRead(read.plans, "stale")
                self._notify_shadow_changed()
            raise
        except Exception:
            # An optional account listing cannot break existing MQTT controls or camera-only accounts.
            self._logger.warning("Garden inventory refresh unavailable; keeping last verified links")
            # A superseded inventory may still be awaiting MQTT handoff under
            # the apply lock. Do not block failure handling behind that I/O.
            if self._inventory_is_current(generation):
                self._stale_garden_reads()
                self._notify_shadow_changed()

    def _stale_garden_reads(self) -> None:
        for scene_id, read in tuple(self._garden_reads.items()):
            self._garden_reads[scene_id] = GardenPlanRead(
                read.plans, "stale" if read.plans is not None else "unknown",
            )

    async def _refresh_garden_plans(
        self, inventory: GardenInventory, generation: int, *, wait: bool = True,
    ) -> None:
        """Share per-garden workers; startup queues them without delaying physical controls."""
        if not self._inventory_is_current(generation):
            return
        tasks: dict[int, asyncio.Task[None]] = {}
        started: dict[int, asyncio.Task[None]] = {}
        versions: dict[int, int] = {}
        for garden in inventory.gardens:
            scene_id = garden.scene_id
            prior = self._garden_read_tasks.get(scene_id)
            task = self._queue_garden_read(scene_id)
            if task is None:
                continue
            tasks[scene_id] = task
            versions[scene_id] = self._garden_read_versions[scene_id]
            if prior is None or prior.done():
                started[scene_id] = task
        if not wait:
            return
        try:
            # Caller cancellation must not erase a later MQTT invalidation
            # already queued on one of these shared workers.
            # A newer inventory may retire a garden and cancel its worker.
            # That cancellation must not cancel an otherwise valid older poll.
            await asyncio.gather(*(asyncio.shield(task) for task in tasks.values()), return_exceptions=True)
        except asyncio.CancelledError:
            abandoned = [
                task for scene_id, task in started.items()
                if self._inventory_is_current(generation)
                and self._garden_read_versions.get(scene_id) == versions[scene_id]
            ]
            for task in abandoned:
                task.cancel()
            if abandoned:
                await asyncio.gather(*abandoned, return_exceptions=True)
            raise

    def _inventory_is_current(self, generation: int) -> bool:
        """Check validity again whenever an inventory operation resumes after awaiting."""
        return generation == self._inventory_generation and not self._shutdown_event.is_set()

    async def _accept_inventory(
        self, inventory: GardenInventory, devices: list[DeviceInfo], generation: int,
    ) -> None:
        """Set up incremental MQTT routes before exposing a new complete snapshot."""
        mqtt_devices = {d.device_id: d for d in self._devices}
        cameras = {d.device_id: d for d in self._camera_devices}
        for device in devices:
            if device.device_type == "camera":
                cameras[device.device_id] = device
            elif device.client_id:
                mqtt_devices[device.device_id] = device

        # A disappeared device is retained, so validate the resulting routes, not
        # only the latest cloud list. Never let a new ID steal a live thing/topic.
        self._validate_runtime_routes(mqtt_devices, cameras)

        mqtt = self._mqtt_client
        prepared: set[tuple[str, str]] = set()
        if mqtt is not None and mqtt.is_connected:
            for device in mqtt_devices.values():
                if (device.client_id, device.topic_prefix) not in self._subscribed_routes:
                    await self._subscribe_device(mqtt, device, track=False, generation=generation)
                    if not self._inventory_is_current(generation):
                        return
                    if self._mqtt_client is not mqtt or not mqtt.is_connected:
                        break
                    prepared.add((device.client_id, device.topic_prefix))

        if not self._inventory_is_current(generation):
            return

        topology = self._garden_topology
        if topology is not None:
            topology.apply(inventory, devices)
        self._garden_inventory = inventory
        # A successful inventory preserves valid reads during the next plan fetch.
        # Never revive failed reads, or leave absent gardens looking current.
        listed = {garden.scene_id for garden in inventory.gardens}
        for scene_id, task in tuple(self._garden_read_tasks.items()):
            if scene_id not in listed:
                task.cancel()
                self._garden_read_tasks.pop(scene_id, None)
                self._garden_read_versions.pop(scene_id, None)
                self._garden_read_last_attempt.pop(scene_id, None)
        for scene_id, read in tuple(self._garden_reads.items()):
            if scene_id not in listed:
                self._garden_reads.pop(scene_id)
            elif read.status == "current":
                self._garden_reads[scene_id] = GardenPlanRead(
                    read.plans, "refreshing",
                )
        self._all_devices = devices
        self._devices = sorted(mqtt_devices.values(), key=lambda d: (d.device_id, d.client_id, d.topic_prefix))
        for device_id in tuple(self._physical_sample_versions):
            if device_id not in mqtt_devices:
                self._physical_sample_versions.pop(device_id, None)
        self._camera_devices = list(cameras.values())
        self._build_topic_maps()
        if mqtt is self._mqtt_client and mqtt is not None and mqtt.is_connected:
            self._subscribed_routes.update(prepared)

        for callback, cameras_only in tuple(self._device_listeners):
            try:
                callback(self.camera_devices if cameras_only else self.devices)
            except Exception:
                self._logger.exception("Platform discovery callback failed")
        self._notify_garden_listeners()

        if mqtt is not None and mqtt.is_connected:
            for device in self._devices:
                if device.device_id not in self._last_shadow_refresh_request_at:
                    try:
                        await self._async_request_shadow_refresh(device.device_id)
                    except (MQTTError, TimeoutError):
                        self._logger.warning("Initial shadow read unavailable for newly discovered device")
        elif self._devices and self._started:
            # Camera-only bootstraps must be able to discover a controller later.
            if self._reconnect_task is None:
                self._refresh_task = asyncio.create_task(self._credentials_refresh_loop())
                self._reconnect_task = asyncio.create_task(self._reconnect_supervisor_loop())
            self._reconnect_event.set()

        self.async_set_updated_data(self._build_state_snapshot())


    @staticmethod
    def _validate_runtime_routes(
        mqtt_devices: dict[str, DeviceInfo], cameras: dict[str, DeviceInfo],
    ) -> None:
        """Reject ambiguous retained route ownership before subscriptions or registry writes."""
        clients: set[str] = set()
        prefixes: set[str] = set()
        if mqtt_devices.keys() & cameras.keys():
            raise VivosunResponseError("Conflicting physical device types in inventory")
        for device in mqtt_devices.values():
            if device.client_id in clients or (device.topic_prefix and device.topic_prefix in prefixes):
                raise VivosunResponseError("Conflicting retained MQTT routes in inventory")
            clients.add(device.client_id)
            if device.topic_prefix:
                prefixes.add(device.topic_prefix)

    async def _refresh_point_log(self) -> None:
        """Refresh climate telemetry from the REST point-log endpoint for all devices."""
        tokens = self._tokens
        if tokens is None or not self._devices:
            return

        end_time = int(datetime.now(tz=UTC).timestamp())
        start_time = end_time - _POINT_LOG_WINDOW_SECONDS
        for device in self._devices:
            if not device.supports_point_log:
                continue
            generation = self._inventory_generation
            version = self._physical_sample_versions.get(device.device_id, 0) + 1
            self._physical_sample_versions[device.device_id] = version
            try:
                point_log = await self._api.get_point_log(
                    tokens, device, start_time=start_time, end_time=end_time
                )
                if not self._physical_poll_is_current(device, generation, version):
                    continue
                if device.device_type == "vseed":
                    self._sensor_states.setdefault(device.device_id, {}).update(
                        {key: point_log.get(key) for key in POINT_LOG_KEYS}
                    )
                elif point_log:
                    self._sensor_states.setdefault(device.device_id, {}).update(
                        cast("dict[str, object]", point_log)
                    )
            except VivosunResponseError:
                # A malformed response must not erase the last valid physical reading.
                continue
            except Exception:
                self._logger.debug(
                    "Failed to fetch point log for device %s", device.name, exc_info=True
                )


    def _select_devices(self, devices: list[DeviceInfo]) -> list[DeviceInfo]:
        """Select all MQTT-capable devices (filter out cameras without client_id)."""
        if not devices:
            return []
        selected = [d for d in devices if d.client_id and d.device_type != "camera"]
        if not selected and not self._camera_devices:
            raise VivosunResponseError("No controllable devices found for this account")
        selected.sort(key=lambda item: (item.device_id, item.client_id, item.topic_prefix))
        for d in selected:
            self._logger.info("Discovered device: %s (type=%s)", d.name, d.device_type)
        return selected

    def _build_topic_maps(self) -> None:
        """Build reverse lookup maps for MQTT topic routing."""
        self._client_id_to_device_id.clear()
        self._topic_prefix_to_device_id.clear()
        for d in self._devices:
            self._client_id_to_device_id[d.client_id] = d.device_id
            if d.topic_prefix:
                self._topic_prefix_to_device_id[d.topic_prefix] = d.device_id

    async def _connect_mqtt(self) -> None:
        """Establish MQTT session and subscribe topics for all devices."""
        if not self._devices:
            return
        primary = self.device
        websocket_url = self._mqtt_websocket_url()

        await self._cancel_support_capture_task()
        await self._disconnect_support_capture_probe_client()
        await self._disconnect_mqtt_client()

        mqtt_client = MQTTClient(
            websocket_url=websocket_url,
            thing=primary.client_id,
            topic_prefix=primary.topic_prefix,
            client_id=f"ha-vivosun-{primary.device_id[:12]}-{uuid4().hex[:8]}",
            label="main",
        )
        await mqtt_client.connect()

        async def receive_current_session(topic: str, payload: bytes, qos: int) -> None:
            """Ignore delayed publishes from a disconnected MQTT session."""
            if self._mqtt_client is mqtt_client:
                await self._handle_mqtt_publish(topic, payload, qos)

        mqtt_client.add_message_callback(receive_current_session)
        self._mqtt_client = mqtt_client
        self._subscribed_routes.add((primary.client_id, primary.topic_prefix))

        # Subscribe additional devices beyond the primary
        for device in self._devices:
            if device.client_id == primary.client_id:
                continue
            await self._subscribe_device(mqtt_client, device)

        # Rejection monitoring is optional: missing cloud permissions must not
        # prevent normal MQTT state and commands from working.
        for device in self._devices:
            if device.client_id != primary.client_id or (
                device.device_type != "controller"
                and client_model_token(device.client_id) != "VSPLUGA22"
            ):
                continue
            try:
                await mqtt_client.subscribe([(TOPIC_SHADOW_UPDATE_REJECTED.format(thing=device.client_id), 1)])
            except (MQTTError, TimeoutError):
                self._logger.warning("Cloud command rejection subscription unavailable; timeout remains active")

        if self._support_capture.active:
            self._schedule_support_capture_initialization()

        self._logger.info("Connected MQTT session for %d devices", len(self._devices))

        for device in self._devices:
            await self._async_request_shadow_refresh(device.device_id)

    async def _subscribe_device(
        self, mqtt_client: MQTTClient, device: DeviceInfo, *, track: bool = True,
        generation: int | None = None,
    ) -> None:
        """Subscribe one additional thing without disturbing the active connection."""
        if generation is not None and not self._inventory_is_current(generation):
            return
        await mqtt_client.subscribe([
            (TOPIC_SHADOW_GET_ACCEPTED.format(thing=device.client_id), 1),
            (TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=device.client_id), 1),
            (TOPIC_SHADOW_UPDATE_DOCUMENTS.format(thing=device.client_id), 1),
            (TOPIC_SHADOW_UPDATE_DELTA.format(thing=device.client_id), 1),
            (TOPIC_CHANNEL_APP.format(topic_prefix=device.topic_prefix), 1),
        ])
        if (mqtt_client is not self._mqtt_client or not mqtt_client.is_connected
                or self._shutdown_event.is_set()
                or (generation is not None and not self._inventory_is_current(generation))):
            return
        if device.device_type == "controller" or client_model_token(device.client_id) == "VSPLUGA22":
            try:
                await mqtt_client.subscribe([(TOPIC_SHADOW_UPDATE_REJECTED.format(thing=device.client_id), 1)])
            except (MQTTError, TimeoutError):
                self._logger.warning("Cloud command rejection subscription unavailable; timeout remains active")
        if (track and mqtt_client is self._mqtt_client and mqtt_client.is_connected
                and not self._shutdown_event.is_set()):
            self._subscribed_routes.add((device.client_id, device.topic_prefix))

    async def _disconnect_mqtt_client(self) -> None:
        mqtt_client = self._mqtt_client
        self._mqtt_client = None
        self._subscribed_routes.clear()
        self._invalidate_a22()
        had_pending = bool(self._pending_fields)
        for pending in self._pending_fields.values():
            pending.timeout.cancel()
        self._pending_fields.clear()
        for choice in self._pending_manual_choices.values():
            choice.timeout.cancel()
        self._pending_manual_choices.clear()
        self._confirmed_manual_facts.clear()
        self._manual_fact_provenance.clear()
        for receipt in self._already_confirmed_tokens.values():
            receipt.timeout.cancel()
        self._already_confirmed_tokens.clear()
        if had_pending:
            self._notify_shadow_changed()
        if mqtt_client is not None:
            await mqtt_client.disconnect()

    async def _disconnect_support_capture_probe_client(self) -> None:
        for waiter in self._support_shadow_waiters.values():
            if not waiter.done():
                waiter.set_result({"status": "disconnected"})
        probe_client = self._support_capture_probe_client
        self._support_capture_probe_client = None
        if probe_client is not None:
            await probe_client.disconnect()

    async def _handle_mqtt_publish(self, topic: str, payload: bytes, qos: int) -> None:
        """Process inbound MQTT publish and route to the correct device state."""
        _ = qos
        if not self._devices:
            return

        device_id = self._route_topic_to_device(topic)
        self._support_capture.record(
            "mqtt_publish",
            data={
                "device_id": device_id or "",
                "topic": topic,
                "qos": qos,
                **summarize_support_capture_payload(payload),
            },
        )
        if device_id is None:
            return

        updated = False
        try:
            # Shadow topics: $aws/things/{client_id}/shadow/...
            if "/shadow/" in topic:
                if topic.endswith("/update/rejected"):
                    document = self._parse_json_object(payload)
                    token = document.get("clientToken")
                    if isinstance(token, str):
                        self._a22_reject(device_id, token)
                        self._discard_manual_choice(token, device_id)
                        rejected_channels = self._clear_pending_token(device_id, token)
                        receipt_channel = self._forget_confirmed_receipt(device_id, token)
                        if receipt_channel is not None:
                            rejected_channels.add(receipt_channel)
                        for channel in rejected_channels:
                            self._logger.warning(
                                "Cloud rejected %s command (code=%s)", channel, document.get("code"),
                            )
                elif (
                    topic.endswith("/get/accepted")
                    or topic.endswith("/update/accepted")
                    or "/update/documents" in topic
                ):
                    document = self._parse_json_object(payload)
                    device = self.get_device(device_id)
                    parsed_shadow = parse_shadow_document(document)
                    if topic.endswith("/update/accepted"):
                        self._a22_accept_ack(device_id, document)
                    self._record_a22_reported(device_id, document, topic)
                    if device is not None and device.device_type == "controller":
                        if client_model_token(device.client_id) == "VSCTL002":
                            self._record_e25_selection(device_id, document, topic)
                        reported = extract_shadow_reported(document)
                        self._record_reported_spectrum(device_id, document, reported, topic)
                        self._record_reported_manual_facts(device_id, document, reported, topic)
                        self._reconcile_reported_fields(device_id, document, parsed_shadow, topic)
                        if topic.endswith("/update/accepted"):
                            self._accept_pending_fields(device_id, document)
                        response_token = document.get("clientToken")
                        if topic.endswith("/update/accepted") and isinstance(response_token, str):
                            self._forget_confirmed_receipt(device_id, response_token)
                    self._merge_shadow_state(device_id, parsed_shadow)
                    if device and client_model_token(device.client_id) == "VSPLUGA22":
                        self._shadow_states.setdefault(device_id, {})["ports"] = {
                            port: deepcopy(raw) for (owner, port), raw in self._a22_reported.items()
                            if owner == device_id
                        }
                    reported = extract_shadow_reported(document)
                    plan = reported.get("plan") if isinstance(reported, dict) else None
                    if isinstance(plan, dict):
                        signature = json.dumps(plan, sort_keys=True)
                        if self._reported_plan_signatures.get(device_id) != signature:
                            self._reported_plan_signatures[device_id] = signature
                            inventory = self._garden_inventory
                            if inventory is not None:
                                for garden in inventory.gardens:
                                    if any(member.device_id == device_id for member in garden.members):
                                        self._queue_garden_read(garden.scene_id)
                                        break
                        self._queue_support_stage_reads(plan)
                    if topic.endswith("/update/accepted"):
                        await self._accept_manual_choice(device_id, document)
                    updated = True
                elif topic.endswith("/update/delta"):
                    self._parse_json_object(payload)
            # Channel topics: {topic_prefix}/channel/app
            elif "/channel/app" in topic:
                self._merge_sensor_state(device_id, parse_channel_sensor_payload(payload))
                self._set_shadow_connection_state(device_id, True)
                updated = True
        except ShadowParseError:
            self._logger.debug(
                "Ignoring malformed MQTT payload for topic %s",
                redact_identifier(topic),
                exc_info=True,
            )
            return
        except ValueError:
            self._logger.debug(
                "Ignoring non-object MQTT JSON payload for topic %s",
                redact_identifier(topic),
                exc_info=True,
            )
            return

        if updated:
            # MQTT pushes must not restart Home Assistant's REST poll timer.
            self._notify_shadow_changed()

    def _route_topic_to_device(self, topic: str) -> str | None:
        """Extract device_id from an MQTT topic by matching client_id or topic_prefix."""
        # Shadow topics: $aws/things/{client_id}/shadow/...
        if topic.startswith("$aws/things/"):
            parts = topic.split("/")
            if len(parts) >= 3:
                client_id = parts[2]
                return self._client_id_to_device_id.get(client_id)
        # Channel topics: {topic_prefix}/channel/app
        for prefix, dev_id in self._topic_prefix_to_device_id.items():
            if topic.startswith(prefix + "/"):
                return dev_id
        return None

    async def async_start_support_capture(self, *, max_events: int) -> None:
        """Start bounded read-only capture independently of entity discovery."""
        await self._cancel_support_stage_reads()
        self._support_stage_seen.clear()
        await self._cancel_support_capture_task()
        await self._disconnect_support_capture_probe_client()
        self._support_capture.start(
            max_events=max_events,
            devices=[
                {
                    "device_id": device.device_id,
                    "device_type": device.device_type,
                    "client_id": device.client_id,
                    "topic_prefix": device.topic_prefix,
                    "name": device.name,
                    "model_token": client_model_token(device.client_id),
                    "missing_fields": list(device.missing_fields),
                }
                for device in self._capture_devices[:SUPPORT_CAPTURE_MAX_DEVICES]
            ],
            subscription_topics=self._support_capture_topic_names(),
        )
        self._support_capture.record(
            "capture_started",
            data={
                "mqtt_connected": self.is_mqtt_connected,
                "device_count": len(self._devices),
                "capture_candidate_count": len(self._capture_devices),
                "omitted_candidate_count": max(0, len(self._capture_devices) - SUPPORT_CAPTURE_MAX_DEVICES),
            },
        )
        skipped = self.skipped_devices_snapshot()
        if skipped:
            self._support_capture.record(
                "skipped_discovery",
                data={
                    "devices": [
                        {
                            "device_group": item.get("device_group"),
                            "model_token": item.get("model_token"),
                            "available_keys": item.get("available_keys"),
                            "missing_fields": item.get("missing_fields"),
                        }
                        for item in skipped[:25]
                    ]
                },
            )
        self._record_support_capture_model_metadata_results()
        self._schedule_support_capture_initialization()
        # A capture may be enabled after the device already reported its plan.
        # Only recorded genuine reports, never desired/shadow fallback, qualify.
        for signature in self._reported_plan_signatures.values():
            self._queue_support_stage_reads(json.loads(signature))

    def _queue_support_stage_reads(self, plan: dict[str, object]) -> None:
        """Fetch each genuinely reported known stage at most once per capture."""
        if not self._support_capture.active or self._tokens is None or self._shutdown_event.is_set():
            return
        for key in ("stage1", "stage2", "stage3", "stage4"):
            stage = plan.get(key)
            if not isinstance(stage, dict):
                continue
            cont_id = stage.get("contId")
            if not isinstance(cont_id, str):
                continue
            stage_id = cont_id.split("+", 1)[0]
            if not stage_id or len(stage_id) > 128 or stage_id in self._support_stage_seen:
                continue
            if len(self._support_stage_seen) >= SUPPORT_CAPTURE_MAX_STAGE_RESULTS:
                break
            self._support_stage_seen.add(stage_id)
            task = asyncio.create_task(self._capture_stage_info(stage_id))
            self._support_stage_tasks.add(task)
            task.add_done_callback(self._support_stage_tasks.discard)

    async def _capture_stage_info(self, stage_id: str) -> None:
        """Collect bounded opt-in evidence without delaying MQTT or garden reads."""
        tokens = self._tokens
        if tokens is None:
            return
        generation = self._support_capture.generation
        try:
            async with self._garden_read_limit, asyncio.timeout(15):
                if self._shutdown_event.is_set() or not self._support_capture.active:
                    return
                if generation != self._support_capture.generation:
                    return
                await self._api.get_plan_stage_info(tokens, stage_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._support_capture.active and generation == self._support_capture.generation:
                self._support_capture.record_stage_info_result(
                    stage_id, {"status": "error", "error_type": type(error).__name__},
                )

    async def _cancel_support_stage_reads(self) -> None:
        tasks = tuple(self._support_stage_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._support_stage_tasks.clear()

    def _schedule_support_capture_initialization(self) -> None:
        """Keep slow capture-only broker operations out of HA setup and reconnect."""
        previous = self._support_capture_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._support_capture_task = asyncio.create_task(
            self._initialize_support_capture(), name="vivosun_support_capture_initialization"
        )

    async def _initialize_support_capture(self) -> None:
        """Collect available evidence without a deadline on the entire capture."""
        self._support_capture.record("capture_initializing")
        try:
            if self._capture_devices:
                await self._async_start_support_capture_probe()
            # Bootstrap fetched point-log data before capture was enabled.
            await self._refresh_point_log()
        except asyncio.CancelledError:
            await self._disconnect_support_capture_probe_client()
            raise
        except Exception:
            self._logger.debug("Support capture initialization failed", exc_info=True)
            self._support_capture.record("capture_initialization_failed")
        else:
            self._support_capture.record("capture_initialized")

    async def _cancel_support_capture_task(self) -> None:
        task = self._support_capture_task
        self._support_capture_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def async_stop_support_capture(self) -> None:
        """Stop support capture."""
        self._support_capture.record("capture_stopping", data={"mqtt_connected": self.is_mqtt_connected})
        await self._cancel_support_stage_reads()
        await self._cancel_support_capture_task()
        await self._disconnect_support_capture_probe_client()
        self._support_capture.stop()

    def _support_capture_topic_names(self) -> list[str]:
        """Return exact, account-discovered topics without wildcard probes."""
        return self._support_capture_classic_topic_names()

    def _support_capture_classic_topic_names(self) -> list[str]:
        """Return only classic snapshot and transition topics for discovered things."""
        topics: list[str] = []
        for device in self._capture_devices[:SUPPORT_CAPTURE_MAX_DEVICES]:
            topics.extend(
                f"$aws/things/{device.client_id}/shadow/{suffix}"
                for suffix in (
                    "get/accepted",
                    "get/rejected",
                    "update/accepted",
                    "update/rejected",
                    "update/documents",
                    "update/delta",
                )
            )
            if device.topic_prefix:
                topics.append(f"{device.topic_prefix}/channel/app")
        return sorted(set(topics))

    def _record_support_capture_subscription_deferred(self, reason: str = "probe_disconnected") -> None:
        """Record that extra support subscriptions will be retried after reconnect."""
        self._support_capture.record(
            "support_topics_subscription_deferred",
            data={"reason": reason, "primary_mqtt_connected": self.is_mqtt_connected},
        )

    async def _async_subscribe_support_capture_topics(self) -> None:
        """Subscribe to known topics and collect exact classic snapshots."""
        classic_topics = self._support_capture_classic_topic_names()
        probe_client = self._support_capture_probe_client
        accepted = await self._async_subscribe_capture_filters(classic_topics, probe_client)
        await asyncio.gather(
            *(
                self._async_capture_shadow(device, accepted)
                for device in self._capture_devices[:SUPPORT_CAPTURE_MAX_DEVICES]
            )
        )

    async def _async_subscribe_capture_filters(self, topics: list[str], mqtt_client: MQTTClient | None) -> set[str]:
        """Record per-filter broker outcomes on the isolated capture connection."""
        if mqtt_client is None or not mqtt_client.is_connected:
            for topic in topics:
                self._support_capture.record_subscription_result(topic, status="deferred", reason="probe_disconnected")
            return set()
        if not topics:
            return set()
        accepted: set[str] = set()
        rejected_count = 0
        for index, topic in enumerate(topics):
            try:
                await mqtt_client.subscribe([(topic, 1)])
            except Exception as err:
                if isinstance(err, MQTTConnectionError) and "rejected one or more topic subscriptions" in str(err):
                    rejected_count += 1
                    self._support_capture.record_subscription_result(
                        topic,
                        status="rejected",
                        reason="broker_rejected",
                    )
                    continue

                for deferred_topic in topics[index:]:
                    self._support_capture.record_subscription_result(
                        deferred_topic,
                        status="deferred",
                        reason="subscription_timeout" if isinstance(err, TimeoutError) else "probe_disconnected",
                    )
                self._record_support_capture_subscription_deferred()
                await mqtt_client.disconnect()
                return accepted

            accepted.add(topic)
            self._support_capture.record_subscription_result(topic, status="accepted")

        self._support_capture.record(
            "support_topics_subscribed",
            data={
                "topic_count": len(topics),
                "accepted_topic_count": len(accepted),
                "rejected_topic_count": rejected_count,
            },
        )
        return accepted

    async def _async_capture_shadow(self, device: CaptureDevice, accepted_topics: set[str]) -> None:
        """Request one classic snapshot and correlate its response without changing entities."""
        model = {"model_token": client_model_token(device.client_id)}
        mqtt_client = self._support_capture_probe_client
        response_topic = f"$aws/things/{device.client_id}/shadow/get/accepted"
        if mqtt_client is None or not mqtt_client.is_connected:
            self._support_capture.record_shadow_read_result(
                device.client_id, {**model, "status": "deferred", "reason": "probe_disconnected"}
            )
            return
        if response_topic not in accepted_topics:
            self._support_capture.record_shadow_read_result(
                device.client_id, {**model, "status": "subscription_rejected"}
            )
            return
        token = uuid4().hex
        key = (device.client_id, token)
        waiter: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._support_shadow_waiters[key] = waiter
        self._support_capture.record_shadow_read_result(device.client_id, {**model, "status": "pending"})
        try:
            async with asyncio.timeout(_SUPPORT_SHADOW_READ_TIMEOUT):
                await mqtt_client.publish(
                    f"$aws/things/{device.client_id}/shadow/get", json.dumps({"clientToken": token}).encode(), qos=1
                )
                result = await waiter
        except TimeoutError:
            result = {"status": "timeout" if mqtt_client.is_connected else "disconnected"}
        except Exception:
            result = {"status": "publish_failed"}
        finally:
            self._support_shadow_waiters.pop(key, None)
            if not waiter.done():
                waiter.cancel()
        self._support_capture.record_shadow_read_result(device.client_id, {**model, **result})

    async def _async_start_support_capture_probe(self) -> None:
        """Start or refresh a dedicated probe MQTT client for recon subscriptions."""
        if not self._support_capture.active or not self._capture_devices:
            return
        await self._disconnect_support_capture_probe_client()
        primary = self._capture_devices[0]
        probe_client: MQTTClient | None = None
        stage = "aws_identity"
        try:
            if self._aws_credentials is None or self._aws_auth.credentials_need_refresh(self._aws_credentials):
                if self._tokens is None:
                    raise VivosunAuthError("Account bootstrap unavailable")
                self._aws_identity = await self._api.get_aws_identity(self._tokens)
                stage = "cognito_credentials"
                self._aws_credentials = await self._aws_auth.get_credentials_for_identity(self._aws_identity)
            stage = "mqtt_connect"
            probe_client = MQTTClient(
                websocket_url=self._mqtt_websocket_url(),
                thing=primary.client_id,
                topic_prefix=primary.topic_prefix,
                client_id=f"ha-vivosun-probe-{uuid4().hex[:12]}",
                label="support-capture-probe",
                subscribe_required_topics=False,
            )
            probe_client.add_message_callback(self._handle_support_capture_probe_publish)
            async with asyncio.timeout(30):
                await probe_client.connect()
        except asyncio.CancelledError:
            if probe_client is not None:
                await probe_client.disconnect()
            raise
        except Exception as err:
            self._logger.debug("Support capture probe connection failed", exc_info=True)
            self._record_support_capture_subscription_deferred("probe_connection_failed")
            for topic in self._support_capture_topic_names():
                self._support_capture.record_subscription_result(
                    topic,
                    status="deferred",
                    reason="probe_connection_failed",
                )
            for device in self._capture_devices[:SUPPORT_CAPTURE_MAX_DEVICES]:
                self._support_capture.record_shadow_read_result(
                    device.client_id,
                    {
                        "model_token": client_model_token(device.client_id),
                        "status": "deferred",
                        "reason": "probe_connection_failed",
                        "stage": stage,
                        "error_type": type(err).__name__,
                    },
                )
            if probe_client is not None:
                await probe_client.disconnect()
            return
        self._support_capture_probe_client = probe_client
        try:
            await self._async_subscribe_support_capture_topics()
        except Exception:
            self._logger.debug("Support capture probe subscriptions failed", exc_info=True)
            self._record_support_capture_subscription_deferred()
            for topic in self._support_capture_topic_names():
                self._support_capture.record_subscription_result(
                    topic,
                    status="deferred",
                    reason="probe_disconnected",
                )
            await self._disconnect_support_capture_probe_client()

    async def _handle_support_capture_probe_publish(self, topic: str, payload: bytes, qos: int) -> None:
        """Record probe-only MQTT messages without affecting entity state."""
        if not self._support_capture.active:
            return
        device = next(
            (
                device
                for device in self._capture_devices[:SUPPORT_CAPTURE_MAX_DEVICES]
                if topic.startswith(f"$aws/things/{device.client_id}/shadow/")
                or (device.topic_prefix and topic.startswith(device.topic_prefix + "/"))
            ),
            None,
        )
        if device is None:
            return
        summary = None
        if not self._primary_captures_topic(device, topic):
            summary = summarize_support_capture_payload(payload)
            self._support_capture.record(
                "support_probe_mqtt_publish",
                data={
                    "device_id": device.device_id,
                    "model_token": client_model_token(device.client_id),
                    "topic": topic,
                    "qos": qos,
                    **summary,
                },
            )
        if len(payload) > SUPPORT_CAPTURE_MAX_PAYLOAD_BYTES or topic not in {
            f"$aws/things/{device.client_id}/shadow/get/accepted",
            f"$aws/things/{device.client_id}/shadow/get/rejected",
        }:
            return
        try:
            document = json.loads(payload)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return
        if not isinstance(document, dict) or not isinstance(document.get("clientToken"), str):
            return
        waiter = self._support_shadow_waiters.get((device.client_id, document["clientToken"]))
        if waiter is not None and not waiter.done():
            if summary is None:
                summary = summarize_support_capture_payload(payload)
            result: dict[str, object] = {
                "status": "accepted" if topic.endswith("/accepted") else "rejected",
                **summary,
            }
            if type(document.get("code")) is int:
                result["code"] = document["code"]
            waiter.set_result(result)

    def _primary_captures_topic(self, device: CaptureDevice, topic: str) -> bool:
        """Use primary capture for its subscribed topics, including all supported devices."""
        if not self.is_mqtt_connected or device.client_id not in self._client_id_to_device_id:
            return False
        return topic in {
            TOPIC_SHADOW_GET_ACCEPTED.format(thing=device.client_id),
            TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=device.client_id),
            TOPIC_SHADOW_UPDATE_DOCUMENTS.format(thing=device.client_id),
            TOPIC_SHADOW_UPDATE_DELTA.format(thing=device.client_id),
            TOPIC_CHANNEL_APP.format(topic_prefix=device.topic_prefix),
        }

    def _record_support_capture_model_metadata_results(self) -> None:
        """Record bundled model metadata for each discovered device."""
        for device in self._capture_devices[:SUPPORT_CAPTURE_MAX_DEVICES]:
            self._support_capture.record_model_metadata_result(
                {
                    "device_id": device.device_id,
                    "client_id": device.client_id,
                    "name": device.name,
                    "device_type": device.device_type,
                    **support_capture_model_metadata(device.client_id),
                }
            )

    def skipped_devices_snapshot(self) -> list[dict[str, object]]:
        """Return skipped getTotalList entries recorded by the API client."""
        skipped = getattr(self._api, "skipped_devices", [])
        if isinstance(skipped, list):
            return [dict(entry) for entry in skipped if isinstance(entry, dict)]
        return []

    def _parse_json_object(self, payload: bytes) -> dict[str, object]:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("MQTT payload root must be an object")
        return cast("dict[str, object]", decoded)

    def _merge_shadow_state(self, device_id: str, state: ShadowV1State) -> None:
        device_shadow = self._shadow_states.setdefault(device_id, {})
        _deep_merge_mapping(device_shadow, cast("dict[str, object]", state))

    def _record_reported_spectrum(
        self, device_id: str, document: dict[str, object], reported: dict[str, object] | None, topic: str
    ) -> None:
        """Cache only fresh, genuine reported spectrum and manual-plan facts."""
        light = reported.get("light") if isinstance(reported, dict) else None
        if not isinstance(light, dict):
            return

        body = document.get("current", document)
        if not isinstance(body, dict):
            return
        raw_version = body.get("version")
        version = raw_version if type(raw_version) is int and raw_version >= 0 else None
        metadata = body.get("metadata")
        metadata = metadata.get("reported") if isinstance(metadata, dict) else None
        light_metadata = metadata.get("light") if isinstance(metadata, dict) else None

        fields: list[tuple[str, object, tuple[str, ...]]] = [
            (key, light[key], (key,)) for key in ("mode", "inPlan", "spec") if key in light
        ]
        manual = light.get("manu")
        if "spec" not in light and isinstance(manual, dict) and "spec" in manual:
            fields.append(("spec", manual["spec"], ("manu", "spec")))
        if not fields:
            return

        # Other entities keep the coordinator's normal shadow projection. These
        # independent facts must not borrow .8 light-LEVEL freshness evidence.
        state_cache = getattr(self, "_confirmed_spectrum", None)
        if state_cache is None:
            state_cache = self._confirmed_spectrum = {}
        provenance_cache = getattr(self, "_spectrum_report_provenance", None)
        if provenance_cache is None:
            provenance_cache = self._spectrum_report_provenance = {}
        state = state_cache.setdefault(device_id, {})
        provenance = provenance_cache.setdefault(device_id, {})

        for key, raw_value, path in fields:
            leaf = light_metadata
            for part in path:
                leaf = leaf.get(part) if isinstance(leaf, dict) else None
            raw_timestamp = leaf.get("timestamp") if isinstance(leaf, dict) else None
            timestamp = raw_timestamp if type(raw_timestamp) is int and raw_timestamp >= 0 else None
            incoming = _ReportedFieldProvenance(version=version, timestamp=timestamp)
            if not _reported_field_is_fresh(provenance.get(key), incoming, topic):
                continue

            # Explicit null, invalid and unavailable values invalidate old proof.
            # The bool false flag is valid for inPlan, but not a manual mode code.
            if key == "inPlan" and isinstance(raw_value, bool):
                value: int | None = int(raw_value)
            elif type(raw_value) is int and raw_value != SENSOR_UNAVAILABLE_SENTINEL:
                value = raw_value
            else:
                value = None
            state[key] = value
            provenance[key] = incoming

    def _record_reported_manual_facts(
        self, device_id: str, document: dict[str, object], reported: dict[str, object] | None, topic: str,
    ) -> None:
        """Keep confirmed per-component manual/plan facts apart from desired fallback."""
        if not isinstance(reported, dict):
            return
        current = document.get("current", document)
        if not isinstance(current, dict):
            return
        raw_version = current.get("version")
        version = raw_version if type(raw_version) is int and raw_version >= 0 else None
        metadata = current.get("metadata")
        metadata = metadata.get("reported") if isinstance(metadata, dict) else None
        for channel in ("light", "cFan", "dFan"):
            component = reported.get(channel)
            if not isinstance(component, dict):
                continue
            component_metadata = metadata.get(channel) if isinstance(metadata, dict) else None
            for field in ("mode", "inPlan"):
                if field not in component:
                    continue
                raw_value = component[field]
                leaf = component_metadata.get(field) if isinstance(component_metadata, dict) else None
                raw_time = leaf.get("timestamp") if isinstance(leaf, dict) else None
                timestamp = raw_time if type(raw_time) is int and raw_time >= 0 else None
                provenance_key = (device_id, channel, field)
                incoming = _ReportedFieldProvenance(version=version, timestamp=timestamp)
                if not _reported_field_is_fresh(self._manual_fact_provenance.get(provenance_key), incoming, topic):
                    continue
                if field == "inPlan" and isinstance(raw_value, bool):
                    value: int | None = int(raw_value)
                elif type(raw_value) is int and raw_value != SENSOR_UNAVAILABLE_SENTINEL:
                    value = raw_value
                else:
                    value = None
                self._confirmed_manual_facts.setdefault((device_id, channel), {})[field] = value
                self._manual_fact_provenance[provenance_key] = incoming

    def _merge_sensor_state(self, device_id: str, state: ChannelSensorState) -> None:
        self._sensor_states.setdefault(device_id, {}).update(cast("dict[str, object]", state))

    def _set_shadow_connection_state(self, device_id: str, connected: bool) -> None:
        device_shadow = self._shadow_states.setdefault(device_id, {})
        _deep_merge_mapping(device_shadow, {"connection": {"connected": connected}})

    def _build_state_snapshot(self) -> dict[str, object]:
        """Build immutable-ish snapshot consumed by entities."""
        devices_map: dict[str, object] = {d.device_id: d for d in self._devices}
        snapshot: dict[str, object] = {
            "devices": devices_map,
            "shadows": deepcopy(self._shadow_states),
            "sensors": deepcopy(self._sensor_states),
            "mqtt_connected": self.is_mqtt_connected,
        }
        shadows = cast("dict[str, dict[str, object]]", snapshot["shadows"])
        for device_id, confirmed in getattr(self, "_confirmed_spectrum", {}).items():
            shadows.setdefault(device_id, {})["confirmed_spectrum"] = dict(confirmed)
        for (device_id, channel, field), pending in self._pending_fields.items():
            component = shadows.setdefault(device_id, {}).setdefault(channel, {})
            if not isinstance(component, dict):
                continue
            component[field] = pending.value
        return snapshot

    async def _async_refresh_stale_shadow_states(self) -> None:
        """Re-request shadow state when a device is stuck in a disconnected state."""
        if not self.is_mqtt_connected:
            return
        for device in self._devices:
            if self._shadow_connection_state(device.device_id) is not False:
                continue
            if not self._shadow_refresh_due(device.device_id):
                continue
            await self._async_request_shadow_refresh(device.device_id)

    def _shadow_connection_state(self, device_id: str) -> bool | None:
        device_shadow = self._shadow_states.get(device_id)
        if not isinstance(device_shadow, dict):
            return None
        connection = device_shadow.get("connection")
        if not isinstance(connection, dict):
            return None
        connected = connection.get("connected")
        if isinstance(connected, bool):
            return connected
        return None

    def _shadow_refresh_due(self, device_id: str) -> bool:
        last_request_at = self._last_shadow_refresh_request_at.get(device_id)
        if last_request_at is None:
            return True
        now = datetime.now(tz=UTC)
        return (now - last_request_at).total_seconds() >= _SHADOW_REFRESH_INTERVAL_SECONDS

    async def _async_request_shadow_refresh(self, device_id: str) -> None:
        device = self.get_device(device_id)
        mqtt_client = self._mqtt_client
        if device is None or mqtt_client is None or not mqtt_client.is_connected:
            return
        self._last_shadow_refresh_request_at[device_id] = datetime.now(tz=UTC)
        topic = TOPIC_SHADOW_GET.format(thing=device.client_id)
        payload = b"{}"
        if client_model_token(device.client_id) == "VSPLUGA22":
            token = self._a22_reads[device_id] = uuid4().hex
            payload = json.dumps({"clientToken": token}).encode()
        await mqtt_client.publish(topic, payload)
        self._support_capture.record(
            "shadow_get_request",
            data={
                "device_id": device.device_id,
                "topic": topic,
            },
        )

    async def _credentials_refresh_loop(self) -> None:
        """Wait for refresh boundary and trigger reconnect cycle."""
        try:
            while not self._shutdown_event.is_set():
                sleep_seconds = self._seconds_until_refresh()
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=sleep_seconds)
                    return
                except TimeoutError:
                    self._reconnect_event.set()
        except asyncio.CancelledError:
            raise

    def _seconds_until_refresh(self) -> float:
        credentials = self._aws_credentials
        if credentials is None:
            return _RECONNECT_BACKOFF_INITIAL
        refresh_at = credentials.expiration - AWS_CREDENTIAL_REFRESH_SKEW
        now = datetime.now(tz=UTC)
        return max(0.0, (refresh_at - now).total_seconds())

    async def _reconnect_supervisor_loop(self) -> None:
        """Reconnect session when disconnected or credentials need refresh."""
        backoff = _RECONNECT_BACKOFF_INITIAL
        try:
            while not self._shutdown_event.is_set():
                reconnect_requested = False
                try:
                    await asyncio.wait_for(self._reconnect_event.wait(), timeout=_RECONNECT_HEALTH_CHECK_SECONDS)
                    reconnect_requested = True
                except TimeoutError:
                    reconnect_requested = False
                finally:
                    self._reconnect_event.clear()

                if self._shutdown_event.is_set():
                    return

                if not reconnect_requested and self.is_mqtt_connected and not self._credentials_need_refresh():
                    continue

                if await self._attempt_reconnect():
                    await self._refresh_point_log()
                    backoff = _RECONNECT_BACKOFF_INITIAL
                    self.async_set_updated_data(self._build_state_snapshot())
                    continue

                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=backoff)
                    return
                except TimeoutError:
                    backoff = min(backoff * 2.0, _RECONNECT_BACKOFF_MAX)
        except asyncio.CancelledError:
            raise

    async def _attempt_reconnect(self) -> bool:
        async with self._reconnect_lock:
            if self._shutdown_event.is_set():
                return True
            try:
                if self._credentials_need_refresh():
                    await self._refresh_credentials()
                await self._connect_mqtt()
                return True
            except VivosunAuthError:
                self._logger.warning("Authentication expired, performing full re-login")
                try:
                    await self._full_reauthenticate()
                    await self._connect_mqtt()
                    return True
                except Exception:
                    self._logger.warning("Failed to re-authenticate coordinator", exc_info=True)
                    return False
            except Exception:
                self._logger.warning("MQTT reconnect attempt failed", exc_info=True)
                return False

    def _credentials_need_refresh(self) -> bool:
        credentials = self._aws_credentials
        if credentials is None:
            return True
        return self._aws_auth.credentials_need_refresh(credentials)

    async def _refresh_credentials(self) -> None:
        tokens = self._tokens
        aws_identity = self._aws_identity
        if tokens is None:
            tokens = await self._api.login(self._email, self._password)
            self._tokens = tokens
        if aws_identity is None:
            aws_identity = await self._api.get_aws_identity(tokens)
            self._aws_identity = aws_identity
        self._aws_credentials = await self._aws_auth.get_credentials_for_identity(aws_identity)

    async def _renew_tokens(self, expired: AuthTokens | None) -> AuthTokens:
        """Share one re-login across REST readers and MQTT authentication recovery."""
        async with self._auth_lock:
            if self._shutdown_event.is_set():
                raise VivosunAuthError("Coordinator is shutting down")
            if self._tokens is expired or self._tokens is None:
                tokens = await self._api.login(self._email, self._password)
                if self._shutdown_event.is_set():
                    raise VivosunAuthError("Coordinator is shutting down")
                self._tokens = tokens
            return self._tokens

    async def _authenticated_read(self, read: Callable[[AuthTokens], Awaitable[_ReadResult]]) -> _ReadResult:
        """Retry one expired REST read without depending on an MQTT connection."""
        tokens = self._tokens
        if tokens is None:
            raise VivosunAuthError("Coordinator is not authenticated")
        try:
            return await read(tokens)
        except VivosunAuthError:
            return await read(await self._renew_tokens(tokens))

    async def _full_reauthenticate(self) -> None:
        tokens = await self._renew_tokens(self._tokens)
        aws_identity = await self._api.get_aws_identity(tokens)
        aws_credentials = await self._aws_auth.get_credentials_for_identity(aws_identity)
        self._aws_identity = aws_identity
        self._aws_credentials = aws_credentials

    def _mqtt_websocket_url(self) -> str:
        """Return the signed websocket URL for a new MQTT session."""
        aws_identity = self._aws_identity
        aws_credentials = self._aws_credentials
        if aws_identity is None or aws_credentials is None:
            raise RuntimeError("Coordinator is missing bootstrap state required for MQTT connect")
        return self._aws_auth.sigv4_sign_mqtt_url(
            endpoint=aws_identity.aws_host,
            region=aws_identity.aws_region,
            credentials=aws_credentials,
        )


def _deep_merge_mapping(target: dict[str, object], source: dict[str, object]) -> None:
    for key, value in source.items():
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge_mapping(
                cast("dict[str, object]", target[key]),
                cast("dict[str, object]", value),
            )
            continue
        target[key] = deepcopy(value)


def _fan_level_timestamp(document: dict[str, object], channel: str, *, reported: bool) -> int | None:
    """Read per-leaf fan level metadata from the matching shadow projection."""
    state = document.get("state")
    state_root = state.get("reported" if reported else "desired") if isinstance(state, dict) else None
    raw_component = state_root.get(channel) if isinstance(state_root, dict) else None
    raw_manual = raw_component.get("manu") if isinstance(raw_component, dict) else None
    # Duct actual output exists only at reported root lv, including explicit null.
    # Circulation fan may select manu.lv when root lv is missing/invalid.
    use_direct = reported and isinstance(raw_component, dict) and "lv" in raw_component and (
        channel == "dFan"
        or type(raw_component["lv"]) is int
        or not isinstance(raw_manual, dict)
        or "lv" not in raw_manual
    )
    if reported and channel == "dFan" and not use_direct:
        return None
    metadata = document.get("metadata")
    root = metadata.get("reported" if reported else "desired") if isinstance(metadata, dict) else None
    component = root.get(channel) if isinstance(root, dict) else None
    manual = component.get("manu") if isinstance(component, dict) else None
    leaf = component.get("lv") if use_direct and isinstance(component, dict) else (
        manual.get("lv") if isinstance(manual, dict) else None
    )
    value = leaf.get("timestamp") if isinstance(leaf, dict) else None
    return value if type(value) is int and value >= 0 else None


def _mode_timestamp(document: dict[str, object], channel: str, *, reported: bool) -> int | None:
    metadata = document.get("metadata")
    state_metadata = metadata.get("reported" if reported else "desired") if isinstance(metadata, dict) else None
    component = state_metadata.get(channel) if isinstance(state_metadata, dict) else None
    leaf = component.get("mode") if isinstance(component, dict) else None
    timestamp = leaf.get("timestamp") if isinstance(leaf, dict) else None
    return timestamp if type(timestamp) is int and timestamp >= 0 else None


def _reported_field_timestamp(document: dict[str, object], channel: _Channel, field: _Field) -> int | None:
    if field == "mode":
        return _mode_timestamp(document, channel, reported=True)
    if field == "manual_level":
        metadata = document.get("metadata")
        reported = metadata.get("reported") if isinstance(metadata, dict) else None
        raw_channel = reported.get(channel) if isinstance(reported, dict) else None
        manual = raw_channel.get("manu") if isinstance(raw_channel, dict) else None
        leaf = manual.get("lv") if isinstance(manual, dict) else None
        timestamp = leaf.get("timestamp") if isinstance(leaf, dict) else None
        return timestamp if type(timestamp) is int and timestamp >= 0 else None
    if channel == "light":
        return reported_light_level_timestamp(document)
    return _fan_level_timestamp(document, channel, reported=True)


def _desired_field_timestamp(document: dict[str, object], channel: _Channel, field: _Field) -> int | None:
    if field == "mode":
        return _mode_timestamp(document, channel, reported=False)
    if channel == "light":
        return accepted_desired_light_level_timestamp(document)
    return _fan_level_timestamp(document, channel, reported=False)


def _a22_documents_changed_leaf(
    document: dict[str, object], port: str, path: tuple[str, ...],
    value: object, timestamp: int | None,
) -> bool:
    """Use a documents transition, not its unchanged snapshot, as A22 write evidence."""
    previous = document.get("previous")
    if not isinstance(previous, dict):
        return False
    old: object = extract_shadow_reported(previous)
    metadata = previous.get("metadata")
    old_meta: object = metadata.get("reported") if isinstance(metadata, dict) else None
    for part in (port, *path):
        old = old.get(part) if isinstance(old, dict) else None
        old_meta = old_meta.get(part) if isinstance(old_meta, dict) else None
    old_stamp = old_meta.get("timestamp") if isinstance(old_meta, dict) else None
    return old != value or (
        type(old_stamp) is int and timestamp is not None and timestamp > old_stamp
    )


def _documents_changed_reported_field(
    document: dict[str, object], channel: _Channel, field: _Field,
    current_value: object, current_timestamp: int | None,
) -> bool:
    """Only changes to this reported leaf establish device-write evidence."""
    previous = document.get("previous")
    if not isinstance(previous, dict):
        return False
    previous_reported = extract_shadow_reported(previous)
    if previous_reported is None:
        return True
    previous_timestamp = _reported_field_timestamp(previous, channel, field)
    if previous_timestamp is not None and current_timestamp is not None and current_timestamp > previous_timestamp:
        return True
    old = cast("dict[str, object]", parse_reported_fragment(previous_reported)).get(channel)
    return not isinstance(old, dict) or field not in old or old[field] != current_value


def _fresh_field_report(
    pending: _PendingField, version: int | None, timestamp: int | None, *, reported_change: bool,
) -> bool:
    """Require reported-leaf freshness or a demonstrably later reported write.

    Shadow versions alone are not field timestamps. An accepted desired request
    gives cloud ordering evidence, but is never device confirmation itself.
    """
    if version is not None and pending.baseline_version is not None and version <= pending.baseline_version:
        return False
    if version is not None and pending.accepted_version is not None and version <= pending.accepted_version:
        return False
    if timestamp is not None and pending.baseline_timestamp is not None and timestamp < pending.baseline_timestamp:
        return False

    if pending.accepted_timestamp is not None:
        if timestamp is not None and timestamp > pending.accepted_timestamp:
            return pending.baseline_timestamp is None or timestamp > pending.baseline_timestamp
        if timestamp is not None and timestamp < pending.accepted_timestamp:
            return False
        # Equal-second timestamps cannot order reports. A direct reported write
        # *after* the cloud accepted the command can, using the version order.
        return (
            reported_change and pending.accepted_version is not None and version is not None
            and version > pending.accepted_version
        )

    if pending.accepted_version is not None:
        return reported_change and version is not None and version > pending.accepted_version

    # The cloud ACK may be absent or delayed. A write to reported, not a GET
    # snapshot or a newer desired-only document, can still supersede the
    # locally displayed request based on field change relative to baseline.
    if not reported_change:
        return False
    if timestamp is not None and pending.baseline_timestamp is not None:
        return timestamp > pending.baseline_timestamp
    return version is not None and pending.baseline_version is not None and version > pending.baseline_version

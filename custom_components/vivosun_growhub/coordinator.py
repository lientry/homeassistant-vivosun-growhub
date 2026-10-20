"""Coordinator lifecycle and runtime orchestration for Vivosun GrowHub."""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, TypeAlias, cast
from uuid import uuid4

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import VivosunApiClient
from .aws_auth import AWS_CREDENTIAL_REFRESH_SKEW, AwsAuthClient, AwsCredentials
from .const import (
    CFAN_LEVEL_MAP,
    DFAN_LEVEL_MAP,
    DOMAIN,
    E42A_DFAN_AUTO_MODE,
    MODE_AUTO,
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
from .exceptions import VivosunAuthError, VivosunResponseError
from .model_metadata import support_capture_model_metadata
from .models import CaptureDevice, client_model_token
from .mqtt_client import MQTTClient, MQTTConnectionError, MQTTError
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
    SupportCaptureManager,
    summarize_support_capture_payload,
)
from .vseed import POINT_LOG_KEYS

if TYPE_CHECKING:
    import aiohttp
    from homeassistant.core import HomeAssistant

    from .manual_preferences import ManualPreferences, ManualSelection
    from .models import AuthTokens, AwsIdentity, DeviceInfo

_LOGGER = logging.getLogger(__name__)
_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 60.0
_RECONNECT_HEALTH_CHECK_SECONDS = 2.0
_POINT_LOG_WINDOW_SECONDS = 300
_SHADOW_REFRESH_INTERVAL_SECONDS = 30.0
_SUPPORT_SHADOW_READ_TIMEOUT = 10.0
_FIELD_CONFIRMATION_TIMEOUT = 15.0

_Channel: TypeAlias = Literal["light", "cFan", "dFan"]
_Field: TypeAlias = Literal["level", "mode"]
_FieldKey: TypeAlias = tuple[str, _Channel, _Field]
_CONTROLLER_CHANNELS: tuple[_Channel, ...] = ("light", "cFan", "dFan")


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


def _reported_field_is_fresh(
    previous: _ReportedFieldProvenance | None, incoming: _ReportedFieldProvenance, topic: str,
) -> bool:
    """Reject stale per-leaf reads without blocking independent partial reports."""
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
    """Explicit HA input awaiting its matching cloud shadow acceptance."""

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
        self._aws_identity: AwsIdentity | None = None
        self._aws_credentials: AwsCredentials | None = None
        self._devices: list[DeviceInfo] = []
        self._camera_devices: list[DeviceInfo] = []
        self._capture_devices: list[CaptureDevice] = []
        self._support_shadow_waiters: dict[tuple[str, str], asyncio.Future[dict[str, object]]] = {}
        self._support_capture_task: asyncio.Task[None] | None = None
        self._mqtt_client: MQTTClient | None = None
        self._support_capture_probe_client: MQTTClient | None = None
        self._support_capture = SupportCaptureManager()
        attach_capture = getattr(self._api, "set_support_capture", None)
        if callable(attach_capture):
            attach_capture(self._support_capture)

        # Per-device state keyed by device_id
        self._shadow_states: dict[str, dict[str, object]] = {}
        self._pending_fields: dict[_FieldKey, _PendingField] = {}
        self._reported_fields: dict[_FieldKey, _ReportedFieldProvenance] = {}
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
        self._field_generation = 0
        self._sensor_states: dict[str, dict[str, object]] = {}
        self._plan_stage_cache: dict[str, object] = {}  # stage_id -> PlanStageInfo

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

    @property
    def is_mqtt_connected(self) -> bool:
        """Return MQTT transport connectivity state."""
        return self._mqtt_client is not None and self._mqtt_client.is_connected

    async def _async_update_data(self) -> dict[str, object]:
        """Poll climate telemetry while MQTT handles device control and push state."""
        await self._refresh_point_log()
        await self._async_refresh_stale_shadow_states()
        await self._refresh_plan_stages()
        return self._build_state_snapshot()

    async def async_start(self) -> None:
        """Bootstrap cloud chain and launch lifecycle workers."""
        async with self._start_lock:
            if self._started:
                return

            self._shutdown_event.clear()
            self._reconnect_event.clear()
            await self._bootstrap_chain()
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
            await self._refresh_point_log()
            self.async_set_updated_data(self._build_state_snapshot())

    async def async_shutdown(self) -> None:
        """Cancel workers and close MQTT resources safely."""
        async with self._start_lock:
            self._shutdown_event.set()
            self._reconnect_event.set()

            tasks = [task for task in (self._refresh_task, self._reconnect_task) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            self._refresh_task = None
            self._reconnect_task = None

            await self._cancel_support_capture_task()
            await self._disconnect_support_capture_probe_client()
            await self._disconnect_mqtt_client()

            self._started = False
            self._tokens = None
            self._aws_identity = None
            self._aws_credentials = None
            self._devices.clear()
            self._camera_devices.clear()
            self._shadow_states.clear()
            for pending in self._pending_fields.values():
                pending.timeout.cancel()
            self._pending_fields.clear()
            self._reported_fields.clear()
            self._has_reported_fields.clear()
            self._locally_commanded_fields.clear()
            self._confirmed_spectrum.clear()
            self._spectrum_report_provenance.clear()
            for receipt in self._already_confirmed_tokens.values():
                receipt.timeout.cancel()
            self._already_confirmed_tokens.clear()
            self._last_accepted_manual_generation.clear()
            self._sensor_states.clear()
            self._plan_stage_cache.clear()
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
                # Light's explicit entity publisher supplies its own token.
                # Raw shadow updates remain byte-for-byte compatible.
                if channel == "light" and not isinstance(payload.get("clientToken"), str):
                    continue
                manual = part.get("manu") if isinstance(part, dict) else None
                raw_level = manual.get("lv") if isinstance(manual, dict) else None
                if channel == "dFan" and isinstance(part, dict):
                    raw_mode = part.get("mode")
                    auto_mode = (
                        E42A_DFAN_AUTO_MODE
                        if client_model_token(target_device.client_id) == "VSCTLE42A" else MODE_AUTO
                    )
                    if type(raw_mode) is int and raw_mode in (0, auto_mode):
                        requested.append(((target_device.device_id, "dFan", "mode"), raw_mode))
                valid_levels = (
                    range(101) if channel == "light"
                    else (*CFAN_LEVEL_MAP, 200) if channel == "cFan" else DFAN_LEVEL_MAP
                )
                if type(raw_level) is int and raw_level in valid_levels:
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

    def manual_memory_enabled(self, device_id: str, channel: str) -> bool:
        """Check opt-in and the supported controller device/channel contract."""
        device = self.get_device(device_id)
        preferences = self.manual_preferences
        return bool(
            device is not None and device.device_type == "controller"
            and preferences is not None and preferences.enabled(device_id, channel)
        )

    def remembered_manual_selection(self, device_id: str, channel: str) -> ManualSelection | None:
        """Return stored HA intent only when memory is enabled."""
        preferences = self.manual_preferences
        if preferences is None or not self.manual_memory_enabled(device_id, channel):
            return None
        return preferences.selection(device_id, channel)

    def manual_restore_allowed(self, device_id: str, channel: str) -> bool:
        """Require fresh, genuinely reported manual and no-plan component state."""
        facts = self._confirmed_manual_facts.get((device_id, channel), {})
        return facts.get("mode") == 0 and facts.get("inPlan") == 0

    def _stage_manual_choice(
        self, device_id: str, channel: str, selection: ManualSelection, shadow_level: int, token: str,
    ) -> None:
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
        """Store only the explicit value correlated to a matching cloud ACK."""
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
        """Publish a selected fan level with an independent preference ACK receipt."""
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
            if channel == "dFan" and field == "mode":
                level_key: _FieldKey = (device_id, "dFan", "level")
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
                if field == "level":
                    confirmed.pop("manual_level", None)
                elif channel == "dFan":
                    confirmed.pop("auto_enabled", None)
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
        for channel in _CONTROLLER_CHANNELS:
            component = reported.get(channel) if isinstance(reported, dict) else None
            if not isinstance(component, dict):
                # Legacy desired-only startup values remain visible until
                # a real report or local command establishes the boundary.
                if any(
                    key[0] == device_id and key[1] == channel
                    for key in self._has_reported_fields | self._locally_commanded_fields
                ):
                    cast("dict[str, object]", parsed).pop(channel, None)
                continue
            projected = parsed.get(channel)
            if not isinstance(projected, dict):
                continue
            # Retain the original light rule for an older whole-shadow version.
            light_provenance = self._reported_fields.get((device_id, "light", "level"))
            if (
                channel == "light" and light_provenance is not None
                and version is not None and light_provenance.version is not None
                and version < light_provenance.version
            ):
                cast("dict[str, object]", parsed).pop("light", None)
                continue
            fields: tuple[_Field, ...] = ("level", "mode") if channel == "dFan" else ("level",)
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
                    if field == "level":
                        cast("dict[str, object]", projected).pop("manual_level", None)
                    elif channel == "dFan":
                        cast("dict[str, object]", projected).pop("auto_enabled", None)
                    continue
                # Missing metadata cannot erase previously known leaf ordering.
                self._reported_fields[key] = _ReportedFieldProvenance(
                    version if version is not None else last.version if last else None,
                    timestamp if timestamp is not None else last.timestamp if last else None,
                )
                self._has_reported_fields.add(key)
                self._reconcile_pending_field(key, value, version, timestamp, reported_change=reported_change)

    def _notify_shadow_changed(self) -> None:
        self.data = self._build_state_snapshot()
        self.async_update_listeners()

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
            try:
                point_log = await self._api.get_point_log(
                    tokens, device, start_time=start_time, end_time=end_time
                )
                if device.device_type == "vseed":
                    self._sensor_states.setdefault(device.device_id, {}).update(
                        {key: point_log.get(key) for key in POINT_LOG_KEYS}
                    )
                elif point_log:
                    self._sensor_states.setdefault(device.device_id, {}).update(
                        cast("dict[str, object]", point_log)
                    )
            except Exception:
                self._logger.debug(
                    "Failed to fetch point log for device %s", device.name, exc_info=True
                )

    def _select_devices(self, devices: list[DeviceInfo]) -> list[DeviceInfo]:
        """Select all MQTT-capable devices (filter out cameras without client_id)."""
        if not devices:
            if self._capture_devices or self.skipped_devices_snapshot():
                return []
            raise VivosunResponseError("No devices found for this account")
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
        mqtt_client.add_message_callback(self._handle_mqtt_publish)
        self._mqtt_client = mqtt_client

        # Subscribe additional devices beyond the primary
        for device in self._devices:
            if device.client_id == primary.client_id:
                continue
            extra_topics = [
                (TOPIC_SHADOW_GET_ACCEPTED.format(thing=device.client_id), 1),
                (TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=device.client_id), 1),
                (TOPIC_SHADOW_UPDATE_DOCUMENTS.format(thing=device.client_id), 1),
                (TOPIC_SHADOW_UPDATE_DELTA.format(thing=device.client_id), 1),
                (TOPIC_CHANNEL_APP.format(topic_prefix=device.topic_prefix), 1),
            ]
            await mqtt_client.subscribe(extra_topics)
            self._logger.debug("Subscribed to MQTT topics for %s", device.name)

        # Rejection monitoring is optional. Missing cloud permissions must not
        # prevent ordinary shadow subscriptions or control.
        for device in self._devices:
            if device.device_type != "controller":
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

    async def _disconnect_mqtt_client(self) -> None:
        mqtt_client = self._mqtt_client
        self._mqtt_client = None
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
                    auto_mode = (
                        E42A_DFAN_AUTO_MODE
                        if device and client_model_token(device.client_id) == "VSCTLE42A"
                        else MODE_AUTO
                    )
                    parsed_shadow = parse_shadow_document(document, dfan_auto_mode=auto_mode)
                    if device is not None and device.device_type == "controller":
                        self._record_reported_spectrum(
                            device_id, document, extract_shadow_reported(document), topic,
                        )
                        self._record_reported_manual_facts(
                            device_id, document, extract_shadow_reported(document), topic,
                        )
                        self._reconcile_reported_fields(device_id, document, parsed_shadow, topic)
                        if topic.endswith("/update/accepted"):
                            self._accept_pending_fields(device_id, document)
                            response_token = document.get("clientToken")
                            if isinstance(response_token, str):
                                self._forget_confirmed_receipt(device_id, response_token)
                    self._merge_shadow_state(device_id, parsed_shadow)
                    if topic.endswith("/update/accepted"):
                        await self._accept_manual_choice(device_id, document)
                    if "plan" in parsed_shadow:
                        await self._refresh_plan_stages(force_refresh_active=True)
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
            await self._refresh_plan_stages()
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
        state = self._confirmed_spectrum.setdefault(device_id, {})
        provenance = self._spectrum_report_provenance.setdefault(device_id, {})
        for key, raw_value, path in fields:
            leaf = light_metadata
            for part in path:
                leaf = leaf.get(part) if isinstance(leaf, dict) else None
            raw_timestamp = leaf.get("timestamp") if isinstance(leaf, dict) else None
            timestamp = raw_timestamp if type(raw_timestamp) is int and raw_timestamp >= 0 else None
            incoming = _ReportedFieldProvenance(version=version, timestamp=timestamp)
            if not _reported_field_is_fresh(provenance.get(key), incoming, topic):
                continue
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
        """Track genuinely reported manual/plan facts with independent per-leaf freshness."""
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

    async def _refresh_plan_stages(self, *, force_refresh_active: bool = False) -> None:
        """Fetch plan stage details for any stage IDs found in shadow plan data."""
        if self._tokens is None:
            return
        for device in self._devices:
            shadow = self._shadow_states.get(device.device_id, {})
            plan = shadow.get("plan")
            if not isinstance(plan, dict):
                continue
            active_key = plan.get("active_stage")
            stages = plan.get("stages")
            if not isinstance(stages, dict):
                continue
            for stage_key, stage_entry in stages.items():
                if not isinstance(stage_entry, dict):
                    continue
                stage_id = stage_entry.get("stage_id", "")
                if not stage_id:
                    continue
                should_refresh = stage_id not in self._plan_stage_cache
                if force_refresh_active and stage_key == active_key:
                    should_refresh = True
                if not should_refresh:
                    continue
                info = await self._api.get_plan_stage_info(self._tokens, stage_id)
                if info is not None:
                    self._plan_stage_cache[stage_id] = info

    def _build_state_snapshot(self) -> dict[str, object]:
        """Build immutable-ish snapshot consumed by entities."""
        devices_map: dict[str, object] = {d.device_id: d for d in self._devices}
        snapshot: dict[str, object] = {
            "devices": devices_map,
            "shadows": deepcopy(self._shadow_states),
            "sensors": deepcopy(self._sensor_states),
            "plan_stages": dict(self._plan_stage_cache),
            "mqtt_connected": self.is_mqtt_connected,
        }
        shadows = cast("dict[str, dict[str, object]]", snapshot["shadows"])
        for device_id, confirmed in self._confirmed_spectrum.items():
            shadows.setdefault(device_id, {})["confirmed_spectrum"] = dict(confirmed)
        for (device_id, channel, field), pending in self._pending_fields.items():
            component = shadows.setdefault(device_id, {}).setdefault(channel, {})
            if not isinstance(component, dict):
                continue
            component[field] = pending.value
            if channel == "dFan" and field == "mode":
                device = self.get_device(device_id)
                auto_mode = (
                    E42A_DFAN_AUTO_MODE
                    if device is not None and client_model_token(device.client_id) == "VSCTLE42A"
                    else MODE_AUTO
                )
                component["auto_enabled"] = pending.value == auto_mode
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
        await mqtt_client.publish(topic, b"{}")
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

    async def _full_reauthenticate(self) -> None:
        tokens = await self._api.login(self._email, self._password)
        aws_identity = await self._api.get_aws_identity(tokens)
        aws_credentials = await self._aws_auth.get_credentials_for_identity(aws_identity)
        self._tokens = tokens
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
    # Duct fan treats any present root lv (including explicit null) as final.
    # Circulation fan falls back to manu.lv when root lv is invalid.
    use_direct = reported and isinstance(raw_component, dict) and "lv" in raw_component and (
        channel == "dFan"
        or type(raw_component["lv"]) is int
        or not isinstance(raw_manual, dict)
        or "lv" not in raw_manual
    )
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
    if channel == "light":
        return reported_light_level_timestamp(document)
    return _fan_level_timestamp(document, channel, reported=True)


def _desired_field_timestamp(document: dict[str, object], channel: _Channel, field: _Field) -> int | None:
    if field == "mode":
        return _mode_timestamp(document, channel, reported=False)
    if channel == "light":
        return accepted_desired_light_level_timestamp(document)
    return _fan_level_timestamp(document, channel, reported=False)


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

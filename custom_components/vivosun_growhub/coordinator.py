"""Coordinator lifecycle and runtime orchestration for Vivosun GrowHub."""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import VivosunApiClient
from .aws_auth import AWS_CREDENTIAL_REFRESH_SKEW, AwsAuthClient, AwsCredentials
from .const import (
    DOMAIN,
    TOPIC_CHANNEL_APP,
    TOPIC_SHADOW_GET,
    TOPIC_SHADOW_GET_ACCEPTED,
    TOPIC_SHADOW_UPDATE,
    TOPIC_SHADOW_UPDATE_ACCEPTED,
    TOPIC_SHADOW_UPDATE_DELTA,
    TOPIC_SHADOW_UPDATE_DOCUMENTS,
)
from .exceptions import VivosunAuthError, VivosunResponseError
from .mqtt_client import MQTTClient, MQTTConnectionError
from .redaction import redact_identifier
from .shadow import (
    ChannelSensorState,
    ShadowParseError,
    ShadowV1State,
    parse_channel_sensor_payload,
    parse_shadow_document,
)

if TYPE_CHECKING:
    import aiohttp
    from homeassistant.core import HomeAssistant

    from .models import AuthTokens, AwsIdentity, DeviceInfo

_LOGGER = logging.getLogger(__name__)
_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 60.0
_RECONNECT_HEALTH_CHECK_SECONDS = 2.0
_POINT_LOG_WINDOW_SECONDS = 300
_SHADOW_REFRESH_INTERVAL_SECONDS = 30.0


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
        self._mqtt_client: MQTTClient | None = None

        # Per-device state keyed by device_id
        self._shadow_states: dict[str, dict[str, object]] = {}
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
        snapshot = self._build_state_snapshot()
        self.async_set_updated_data(snapshot)
        return snapshot

    async def async_start(self) -> None:
        """Bootstrap cloud chain and launch lifecycle workers."""
        async with self._start_lock:
            if self._started:
                return

            self._shutdown_event.clear()
            self._reconnect_event.clear()
            await self._bootstrap_chain()
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

            await self._disconnect_mqtt_client()

            self._started = False
            self._tokens = None
            self._aws_identity = None
            self._aws_credentials = None
            self._devices.clear()
            self._camera_devices.clear()
            self._shadow_states.clear()
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
        topic = TOPIC_SHADOW_UPDATE.format(thing=target_device.client_id)
        await mqtt_client.publish(topic, encoded, qos=qos)

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
        self._camera_devices = [device for device in all_devices if device.device_type == "camera"]
        self._devices = self._select_devices(all_devices)
        self._build_topic_maps()
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
            try:
                point_log = await self._api.get_point_log(
                    tokens, device, start_time=start_time, end_time=end_time
                )
                if point_log:
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
            raise VivosunResponseError("No devices found for this account")
        selected = [d for d in devices if d.client_id and d.device_type != "camera"]
        if not selected:
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
        aws_identity = self._aws_identity
        aws_credentials = self._aws_credentials
        if aws_identity is None or aws_credentials is None or not self._devices:
            raise RuntimeError("Coordinator is missing bootstrap state required for MQTT connect")

        primary = self.device
        websocket_url = self._aws_auth.sigv4_sign_mqtt_url(
            endpoint=aws_identity.aws_host,
            region=aws_identity.aws_region,
            credentials=aws_credentials,
        )

        await self._disconnect_mqtt_client()

        mqtt_client = MQTTClient(
            websocket_url=websocket_url,
            thing=primary.client_id,
            topic_prefix=primary.topic_prefix,
            client_id=f"ha-vivosun-{primary.device_id[:12]}-{uuid4().hex[:8]}",
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

        self._logger.info("Connected MQTT session for %d devices", len(self._devices))

        for device in self._devices:
            await self._async_request_shadow_refresh(device.device_id)

    async def _disconnect_mqtt_client(self) -> None:
        mqtt_client = self._mqtt_client
        self._mqtt_client = None
        if mqtt_client is not None:
            await mqtt_client.disconnect()

    async def _handle_mqtt_publish(self, topic: str, payload: bytes, qos: int) -> None:
        """Process inbound MQTT publish and route to the correct device state."""
        _ = qos
        if not self._devices:
            return

        device_id = self._route_topic_to_device(topic)
        if device_id is None:
            return

        updated = False
        try:
            # Shadow topics: $aws/things/{client_id}/shadow/...
            if "/shadow/" in topic:
                if (
                    topic.endswith("/get/accepted")
                    or topic.endswith("/update/accepted")
                    or "/update/documents" in topic
                ):
                    document = self._parse_json_object(payload)
                    self._merge_shadow_state(device_id, parse_shadow_document(document))
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
            self.async_set_updated_data(self._build_state_snapshot())

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

    def _parse_json_object(self, payload: bytes) -> dict[str, object]:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("MQTT payload root must be an object")
        return cast("dict[str, object]", decoded)

    def _merge_shadow_state(self, device_id: str, state: ShadowV1State) -> None:
        device_shadow = self._shadow_states.setdefault(device_id, {})
        _deep_merge_mapping(device_shadow, cast("dict[str, object]", state))

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
        await mqtt_client.publish(TOPIC_SHADOW_GET.format(thing=device.client_id), b"{}")

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

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
        self._device: DeviceInfo | None = None
        self._mqtt_client: MQTTClient | None = None

        self._shadow_state: dict[str, object] = {}
        self._sensor_state: dict[str, object] = {}

        self._refresh_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._shutdown_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._started = False

    @property
    def device(self) -> DeviceInfo:
        """Return selected GrowHub device metadata."""
        if self._device is None:
            raise RuntimeError("Device not available before coordinator start")
        return self._device

    @property
    def is_mqtt_connected(self) -> bool:
        """Return MQTT transport connectivity state."""
        return self._mqtt_client is not None and self._mqtt_client.is_connected

    async def _async_update_data(self) -> dict[str, object]:
        """Poll climate telemetry while MQTT handles device control and push state."""
        await self._refresh_point_log()
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
            self._device = None
            self._shadow_state.clear()
            self._sensor_state.clear()

    async def async_publish_shadow_update(
        self,
        payload: dict[str, object] | str | bytes,
        *,
        qos: int = 0,
    ) -> None:
        """Publish shadow control payload through the active MQTT session."""
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
        await mqtt_client.publish_shadow_update(encoded, qos=qos)

    async def _bootstrap_chain(self) -> None:
        """Run strict API -> AWS -> MQTT bootstrap order."""
        self._logger.info("Starting Vivosun coordinator bootstrap")
        self._tokens = await self._api.login(self._email, self._password)
        devices = await self._api.get_devices(self._tokens)
        self._device = self._select_device(devices)
        self._aws_identity = await self._api.get_aws_identity(self._tokens)
        self._aws_credentials = await self._aws_auth.get_credentials_for_identity(self._aws_identity)
        await self._connect_mqtt()

    async def _refresh_point_log(self) -> None:
        """Refresh climate telemetry from the REST point-log endpoint."""
        tokens = self._tokens
        device = self._device
        if tokens is None or device is None:
            return

        end_time = int(datetime.now(tz=UTC).timestamp())
        start_time = end_time - _POINT_LOG_WINDOW_SECONDS
        point_log = await self._api.get_point_log(tokens, device, start_time=start_time, end_time=end_time)
        if point_log:
            self._sensor_state.update(cast("dict[str, object]", point_log))

    def _select_device(self, devices: list[DeviceInfo]) -> DeviceInfo:
        """Pick a deterministic single GrowHub target device."""
        if not devices:
            raise VivosunResponseError("No GrowHub devices found for this account")
        ordered = sorted(devices, key=lambda item: (item.device_id, item.client_id, item.topic_prefix))
        device = ordered[0]
        self._logger.info("Selected GrowHub device: %s", device.name)
        return device

    async def _connect_mqtt(self) -> None:
        """Establish MQTT session using current AWS credentials and subscribe topics."""
        aws_identity = self._aws_identity
        aws_credentials = self._aws_credentials
        device = self._device
        if aws_identity is None or aws_credentials is None or device is None:
            raise RuntimeError("Coordinator is missing bootstrap state required for MQTT connect")

        websocket_url = self._aws_auth.sigv4_sign_mqtt_url(
            endpoint=aws_identity.aws_host,
            region=aws_identity.aws_region,
            credentials=aws_credentials,
        )

        await self._disconnect_mqtt_client()

        mqtt_client = MQTTClient(
            websocket_url=websocket_url,
            thing=device.client_id,
            topic_prefix=device.topic_prefix,
            client_id=f"ha-vivosun-{device.device_id[:12]}-{uuid4().hex[:8]}",
        )
        await mqtt_client.connect()
        mqtt_client.add_message_callback(self._handle_mqtt_publish)
        self._mqtt_client = mqtt_client
        self._logger.info("Connected MQTT session for device %s", device.name)

        await mqtt_client.publish(TOPIC_SHADOW_GET.format(thing=device.client_id), b"{}")

    async def _disconnect_mqtt_client(self) -> None:
        mqtt_client = self._mqtt_client
        self._mqtt_client = None
        if mqtt_client is not None:
            await mqtt_client.disconnect()

    async def _handle_mqtt_publish(self, topic: str, payload: bytes, qos: int) -> None:
        """Process inbound MQTT publish and propagate push state updates."""
        _ = qos
        device = self._device
        if device is None:
            return

        shadow_topic_get_accepted = TOPIC_SHADOW_GET_ACCEPTED.format(thing=device.client_id)
        shadow_topic_update_accepted = TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=device.client_id)
        shadow_topic_documents = TOPIC_SHADOW_UPDATE_DOCUMENTS.format(thing=device.client_id)
        shadow_topic_delta = TOPIC_SHADOW_UPDATE_DELTA.format(thing=device.client_id)
        channel_topic = TOPIC_CHANNEL_APP.format(topic_prefix=device.topic_prefix)

        updated = False
        try:
            if topic in (shadow_topic_get_accepted, shadow_topic_update_accepted, shadow_topic_documents):
                document = self._parse_json_object(payload)
                self._merge_shadow_state(parse_shadow_document(document))
                updated = True
            elif topic == shadow_topic_delta:
                # Delta reflects desired-vs-reported drift and can temporarily carry
                # stale values (for example light.lv=50). Do not surface it as the
                # live entity state; wait for reported state on accepted/documents.
                self._parse_json_object(payload)
            elif topic == channel_topic:
                self._merge_sensor_state(parse_channel_sensor_payload(payload))
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

    def _parse_json_object(self, payload: bytes) -> dict[str, object]:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("MQTT payload root must be an object")
        return cast("dict[str, object]", decoded)

    def _merge_shadow_state(self, state: ShadowV1State) -> None:
        _deep_merge_mapping(self._shadow_state, cast("dict[str, object]", state))

    def _merge_sensor_state(self, state: ChannelSensorState) -> None:
        self._sensor_state.update(cast("dict[str, object]", state))

    def _build_state_snapshot(self) -> dict[str, object]:
        """Build immutable-ish snapshot consumed by entities."""
        snapshot: dict[str, object] = {
            "device": self._device,
            "shadow": deepcopy(self._shadow_state),
            "sensors": deepcopy(self._sensor_state),
            "mqtt_connected": self.is_mqtt_connected,
        }
        return snapshot

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

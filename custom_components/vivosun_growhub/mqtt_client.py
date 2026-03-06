"""Custom MQTT 3.1.1 codec and websocket transport client."""

from __future__ import annotations

import asyncio
import inspect
import logging
import ssl
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

import websockets

from .const import (
    TOPIC_CHANNEL_APP,
    TOPIC_SHADOW_GET_ACCEPTED,
    TOPIC_SHADOW_UPDATE,
    TOPIC_SHADOW_UPDATE_ACCEPTED,
    TOPIC_SHADOW_UPDATE_DELTA,
    TOPIC_SHADOW_UPDATE_DOCUMENTS,
)

_LOGGER = logging.getLogger(__name__)


class MQTTError(Exception):
    """Base MQTT client error."""


class MQTTProtocolError(MQTTError):
    """Raised when an MQTT packet is malformed or unexpected."""


class MQTTIncompleteError(MQTTProtocolError):
    """Raised when additional stream bytes are required for parsing."""


class MQTTConnectionError(MQTTError):
    """Raised for transport/session level failures."""


class MQTTPingTimeoutError(MQTTConnectionError):
    """Raised when ping response is not received in time."""


class _PacketType(IntEnum):
    CONNECT = 1
    CONNACK = 2
    PUBLISH = 3
    PUBACK = 4
    SUBSCRIBE = 8
    SUBACK = 9
    PINGREQ = 12
    PINGRESP = 13
    DISCONNECT = 14


@dataclass(slots=True, frozen=True)
class ReceivedPublish:
    """Decoded inbound publish payload."""

    topic: str
    payload: bytes
    qos: int
    retain: bool
    dup: bool
    packet_id: int | None


@dataclass(slots=True, frozen=True)
class ParsedPacket:
    """Decoded packet envelope extracted from stream."""

    packet_type: int
    flags: int
    payload: bytes


MessageCallback = Callable[[str, bytes, int], Awaitable[None] | None]


class _WebSocketLike(Protocol):
    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes | str: ...

    async def close(self) -> None: ...


def encode_remaining_length(length: int) -> bytes:
    """Encode MQTT variable-byte integer for remaining length."""
    if length < 0:
        raise MQTTProtocolError("Remaining length cannot be negative")
    encoded = bytearray()
    value = length
    while True:
        digit = value % 128
        value //= 128
        if value > 0:
            digit |= 0x80
        encoded.append(digit)
        if value == 0:
            break
    if len(encoded) > 4:
        raise MQTTProtocolError("Remaining length exceeds MQTT maximum")
    return bytes(encoded)


def decode_remaining_length(data: bytes, start_index: int = 0) -> tuple[int, int]:
    """Decode MQTT variable-byte integer from bytes at start_index."""
    multiplier = 1
    value = 0
    consumed = 0
    index = start_index
    while True:
        if index >= len(data):
            raise MQTTIncompleteError("Incomplete remaining length field")
        encoded_byte = data[index]
        value += (encoded_byte & 127) * multiplier
        consumed += 1
        index += 1
        if multiplier > 128 * 128 * 128:
            raise MQTTProtocolError("Malformed remaining length field")
        if (encoded_byte & 0x80) == 0:
            break
        multiplier *= 128
    return value, consumed


def _encode_utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise MQTTProtocolError("MQTT UTF-8 string exceeds 65535 bytes")
    return len(encoded).to_bytes(2, "big") + encoded


def _decode_utf8(data: bytes, index: int) -> tuple[str, int]:
    if index + 2 > len(data):
        raise MQTTProtocolError("MQTT UTF-8 field missing length")
    size = int.from_bytes(data[index : index + 2], "big")
    start = index + 2
    end = start + size
    if end > len(data):
        raise MQTTProtocolError("MQTT UTF-8 field truncated")
    try:
        return data[start:end].decode("utf-8"), end
    except UnicodeDecodeError as err:
        raise MQTTProtocolError("MQTT UTF-8 field is invalid") from err


def build_connect_packet(*, client_id: str, keepalive: int, clean_session: bool = True) -> bytes:
    """Build MQTT CONNECT packet (3.1.1)."""
    if keepalive < 0 or keepalive > 0xFFFF:
        raise MQTTProtocolError("keepalive must be in range 0..65535")
    variable_header = (
        _encode_utf8("MQTT")
        + bytes([0x04])
        + bytes([0x02 if clean_session else 0x00])
        + keepalive.to_bytes(2, "big")
    )
    payload = _encode_utf8(client_id)
    remaining = encode_remaining_length(len(variable_header) + len(payload))
    return bytes([_PacketType.CONNECT << 4]) + remaining + variable_header + payload


def parse_connack_packet(packet: bytes) -> tuple[bool, int]:
    """Parse MQTT CONNACK packet and return (session_present, return_code)."""
    if not packet:
        raise MQTTProtocolError("Empty packet")
    packet_type = packet[0] >> 4
    flags = packet[0] & 0x0F
    if packet_type != _PacketType.CONNACK:
        raise MQTTProtocolError("Expected CONNACK packet")
    if flags != 0:
        raise MQTTProtocolError("CONNACK flags must be zero")
    remaining_length, consumed = decode_remaining_length(packet, 1)
    payload_start = 1 + consumed
    payload_end = payload_start + remaining_length
    if payload_end != len(packet):
        raise MQTTProtocolError("CONNACK size mismatch")
    if remaining_length != 2:
        raise MQTTProtocolError("CONNACK remaining length must be 2")
    ack_flags = packet[payload_start]
    return_code = packet[payload_start + 1]
    if ack_flags & 0xFE:
        raise MQTTProtocolError("CONNACK reserved bits are set")
    return bool(ack_flags & 0x01), return_code


def build_subscribe_packet(packet_id: int, topics: list[tuple[str, int]]) -> bytes:
    """Build MQTT SUBSCRIBE packet."""
    if packet_id <= 0 or packet_id > 0xFFFF:
        raise MQTTProtocolError("packet_id must be in range 1..65535")
    if not topics:
        raise MQTTProtocolError("SUBSCRIBE requires at least one topic")

    payload = bytearray()
    for topic, qos in topics:
        if qos not in (0, 1):
            raise MQTTProtocolError("Only QoS 0 and 1 are supported")
        payload.extend(_encode_utf8(topic))
        payload.append(qos)

    variable_header = packet_id.to_bytes(2, "big")
    remaining = encode_remaining_length(len(variable_header) + len(payload))
    return bytes([(_PacketType.SUBSCRIBE << 4) | 0x02]) + remaining + variable_header + bytes(payload)


def parse_suback_packet(packet: bytes) -> tuple[int, list[int]]:
    """Parse MQTT SUBACK packet and return (packet_id, return_codes)."""
    if not packet:
        raise MQTTProtocolError("Empty packet")
    packet_type = packet[0] >> 4
    flags = packet[0] & 0x0F
    if packet_type != _PacketType.SUBACK:
        raise MQTTProtocolError("Expected SUBACK packet")
    if flags != 0:
        raise MQTTProtocolError("SUBACK flags must be zero")

    remaining_length, consumed = decode_remaining_length(packet, 1)
    payload_start = 1 + consumed
    payload_end = payload_start + remaining_length
    if payload_end != len(packet):
        raise MQTTProtocolError("SUBACK size mismatch")
    if remaining_length < 3:
        raise MQTTProtocolError("SUBACK remaining length too small")

    packet_id = int.from_bytes(packet[payload_start : payload_start + 2], "big")
    if packet_id == 0:
        raise MQTTProtocolError("SUBACK packet identifier cannot be zero")

    return_codes = list(packet[payload_start + 2 : payload_end])
    if not return_codes:
        raise MQTTProtocolError("SUBACK must include at least one return code")
    for code in return_codes:
        if code not in (0x00, 0x01, 0x80):
            raise MQTTProtocolError("SUBACK contains invalid return code")
    return packet_id, return_codes


def build_publish_packet(
    *,
    topic: str,
    payload: bytes,
    qos: int = 0,
    retain: bool = False,
    packet_id: int | None = None,
) -> bytes:
    """Build MQTT PUBLISH packet."""
    if qos not in (0, 1):
        raise MQTTProtocolError("Only QoS 0 and 1 are supported")

    topic_field = _encode_utf8(topic)
    variable_header = bytearray(topic_field)
    if qos > 0:
        if packet_id is None or packet_id <= 0 or packet_id > 0xFFFF:
            raise MQTTProtocolError("QoS 1 publish requires packet_id in range 1..65535")
        variable_header.extend(packet_id.to_bytes(2, "big"))

    fixed_flags = (qos << 1) | (0x01 if retain else 0x00)
    remaining = encode_remaining_length(len(variable_header) + len(payload))
    return bytes([(_PacketType.PUBLISH << 4) | fixed_flags]) + remaining + bytes(variable_header) + payload


def parse_publish_packet(packet: bytes) -> ReceivedPublish:
    """Parse MQTT PUBLISH packet."""
    if not packet:
        raise MQTTProtocolError("Empty packet")
    packet_type = packet[0] >> 4
    if packet_type != _PacketType.PUBLISH:
        raise MQTTProtocolError("Expected PUBLISH packet")

    flags = packet[0] & 0x0F
    retain = bool(flags & 0x01)
    qos = (flags >> 1) & 0x03
    dup = bool(flags & 0x08)
    if qos == 0x03:
        raise MQTTProtocolError("Invalid QoS in PUBLISH flags")

    remaining_length, consumed = decode_remaining_length(packet, 1)
    payload_start = 1 + consumed
    payload_end = payload_start + remaining_length
    if payload_end != len(packet):
        raise MQTTProtocolError("PUBLISH size mismatch")

    packet_id: int | None = None
    topic, cursor = _decode_utf8(packet, payload_start)
    if qos > 0:
        if cursor + 2 > payload_end:
            raise MQTTProtocolError("QoS publish missing packet identifier")
        packet_id = int.from_bytes(packet[cursor : cursor + 2], "big")
        if packet_id == 0:
            raise MQTTProtocolError("PUBLISH packet identifier cannot be zero")
        cursor += 2

    payload = packet[cursor:payload_end]
    return ReceivedPublish(topic=topic, payload=payload, qos=qos, retain=retain, dup=dup, packet_id=packet_id)


def build_puback_packet(packet_id: int) -> bytes:
    """Build MQTT PUBACK packet."""
    if packet_id <= 0 or packet_id > 0xFFFF:
        raise MQTTProtocolError("PUBACK packet_id must be in range 1..65535")
    return bytes([_PacketType.PUBACK << 4, 0x02]) + packet_id.to_bytes(2, "big")


def parse_puback_packet(packet: bytes) -> int:
    """Parse MQTT PUBACK packet and return packet identifier."""
    if not packet:
        raise MQTTProtocolError("Empty packet")
    packet_type = packet[0] >> 4
    flags = packet[0] & 0x0F
    if packet_type != _PacketType.PUBACK:
        raise MQTTProtocolError("Expected PUBACK packet")
    if flags != 0:
        raise MQTTProtocolError("PUBACK flags must be zero")

    remaining_length, consumed = decode_remaining_length(packet, 1)
    payload_start = 1 + consumed
    payload_end = payload_start + remaining_length
    if payload_end != len(packet):
        raise MQTTProtocolError("PUBACK size mismatch")
    if remaining_length != 2:
        raise MQTTProtocolError("PUBACK remaining length must be 2")

    packet_id = int.from_bytes(packet[payload_start:payload_end], "big")
    if packet_id == 0:
        raise MQTTProtocolError("PUBACK packet identifier cannot be zero")
    return packet_id


def build_pingreq_packet() -> bytes:
    """Build MQTT PINGREQ packet."""
    return bytes([_PacketType.PINGREQ << 4, 0x00])


def is_pingresp_packet(packet: bytes) -> bool:
    """Return True when packet is MQTT PINGRESP."""
    return packet == bytes([_PacketType.PINGRESP << 4, 0x00])


def build_disconnect_packet() -> bytes:
    """Build MQTT DISCONNECT packet."""
    return bytes([_PacketType.DISCONNECT << 4, 0x00])


class MQTTStreamParser:
    """Incremental parser for MQTT packets over stream transports."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[ParsedPacket]:
        """Consume bytes and return decoded full packets."""
        self._buffer.extend(data)
        packets: list[ParsedPacket] = []

        while True:
            if len(self._buffer) < 2:
                break

            try:
                remaining_length, consumed = decode_remaining_length(bytes(self._buffer), 1)
            except MQTTIncompleteError:
                break

            packet_len = 1 + consumed + remaining_length
            if len(self._buffer) < packet_len:
                break

            first_byte = self._buffer[0]
            packet_type = first_byte >> 4
            flags = first_byte & 0x0F
            payload_start = 1 + consumed
            payload = bytes(self._buffer[payload_start:packet_len])
            packets.append(ParsedPacket(packet_type=packet_type, flags=flags, payload=payload))
            del self._buffer[:packet_len]

        return packets


class MQTTClient:
    """MQTT 3.1.1 websocket transport client for AWS IoT."""

    def __init__(
        self,
        *,
        websocket_url: str,
        thing: str,
        topic_prefix: str,
        client_id: str | None = None,
        keepalive_seconds: float = 60,
        connect_timeout: float = 15.0,
    ) -> None:
        self._websocket_url = websocket_url
        self._thing = thing
        self._topic_prefix = topic_prefix
        self._client_id = client_id or thing
        self._keepalive = keepalive_seconds
        self._connect_timeout = connect_timeout

        self._ws: _WebSocketLike | None = None
        self._connected = False
        self._parser = MQTTStreamParser()
        self._packet_id = 0
        self._disconnect_lock = asyncio.Lock()

        self._receive_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

        self._suback_waiters: dict[int, asyncio.Future[list[int]]] = {}
        self._ping_response_event = asyncio.Event()
        self._message_callbacks: list[MessageCallback] = []
        self._pending_packets: deque[ParsedPacket] = deque()

    @property
    def is_connected(self) -> bool:
        """Return current transport connectivity state."""
        return self._connected

    @property
    def required_topics(self) -> list[str]:
        """Return required topic subscriptions for the session."""
        return [
            TOPIC_SHADOW_GET_ACCEPTED.format(thing=self._thing),
            TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=self._thing),
            TOPIC_SHADOW_UPDATE_DOCUMENTS.format(thing=self._thing),
            TOPIC_SHADOW_UPDATE_DELTA.format(thing=self._thing),
            TOPIC_CHANNEL_APP.format(topic_prefix=self._topic_prefix),
        ]

    def add_message_callback(self, callback: MessageCallback) -> None:
        """Register callback for inbound PUBLISH packets."""
        self._message_callbacks.append(callback)

    async def connect(self) -> None:
        """Open websocket, complete MQTT handshake, and subscribe required topics."""
        if self._connected:
            return
        if self._keepalive <= 0:
            raise MQTTProtocolError("keepalive_seconds must be > 0")

        try:
            ssl_context = await asyncio.to_thread(ssl.create_default_context)
            self._ws = await websockets.connect(
                self._websocket_url,
                ssl=ssl_context,
                subprotocols=[websockets.Subprotocol("mqtt")],
                compression=None,
                ping_interval=None,
                ping_timeout=None,
            )
            mqtt_keepalive = max(1, int(self._keepalive))
            await self._send_packet(build_connect_packet(client_id=self._client_id, keepalive=mqtt_keepalive))
            connack_packet = await self._read_packet(wait_timeout=self._connect_timeout)
            if connack_packet.packet_type != _PacketType.CONNACK:
                raise MQTTProtocolError("First packet from broker must be CONNACK")

            session_present, return_code = parse_connack_packet(
                bytes([(_PacketType.CONNACK << 4) | connack_packet.flags])
                + encode_remaining_length(len(connack_packet.payload))
                + connack_packet.payload
            )
            if return_code != 0:
                raise MQTTConnectionError(f"MQTT broker rejected connect with return code {return_code}")
            _LOGGER.debug("MQTT connected (session_present=%s)", session_present)

            self._connected = True
            self._receive_task = asyncio.create_task(self._receive_loop(), name="vivosun_mqtt_receive")
            self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="vivosun_mqtt_keepalive")

            await self.subscribe([(topic, 1) for topic in self.required_topics])
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        """Close session and cancel all background tasks."""
        async with self._disconnect_lock:
            ws = self._ws
            if ws is None and not self._connected:
                return

            self._connected = False
            current_task = asyncio.current_task()
            tasks = [
                task
                for task in (self._receive_task, self._keepalive_task)
                if task is not None and task is not current_task
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            for future in self._suback_waiters.values():
                if not future.done():
                    future.set_exception(MQTTConnectionError("MQTT client disconnected"))
            self._suback_waiters.clear()

            if ws is not None:
                try:
                    await ws.send(build_disconnect_packet())
                except Exception:
                    _LOGGER.debug("MQTT disconnect frame failed", exc_info=True)
                await ws.close()

            self._ws = None
            self._receive_task = None
            self._keepalive_task = None
            self._pending_packets.clear()

    async def subscribe(self, topics: list[tuple[str, int]]) -> None:
        """Subscribe to topics and await matching SUBACK."""
        if not self._connected:
            raise MQTTConnectionError("Cannot subscribe while disconnected")

        packet_id = self._next_packet_id()
        frame = build_subscribe_packet(packet_id, topics)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[list[int]] = loop.create_future()
        self._suback_waiters[packet_id] = waiter

        try:
            await self._send_packet(frame)
            return_codes = await asyncio.wait_for(waiter, timeout=self._connect_timeout)
            if len(return_codes) != len(topics):
                raise MQTTProtocolError("SUBACK return code count does not match SUBSCRIBE topic count")
            if any(code == 0x80 for code in return_codes):
                raise MQTTConnectionError("Broker rejected one or more topic subscriptions")
        finally:
            self._suback_waiters.pop(packet_id, None)

    async def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        """Publish binary payload on topic."""
        if not self._connected:
            raise MQTTConnectionError("Cannot publish while disconnected")

        packet_id = self._next_packet_id() if qos > 0 else None
        frame = build_publish_packet(topic=topic, payload=payload, qos=qos, retain=retain, packet_id=packet_id)
        await self._send_packet(frame)

    async def publish_shadow_update(self, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        """Publish control payload to GrowHub shadow update topic."""
        encoded_payload = payload.encode("utf-8") if isinstance(payload, str) else payload
        await self.publish(TOPIC_SHADOW_UPDATE.format(thing=self._thing), encoded_payload, qos=qos, retain=retain)

    async def _send_packet(self, packet: bytes) -> None:
        ws = self._ws
        if ws is None:
            raise MQTTConnectionError("Websocket session is not open")
        await ws.send(packet)

    async def _read_packet(self, *, wait_timeout: float | None = None) -> ParsedPacket:
        ws = self._ws
        if ws is None:
            raise MQTTConnectionError("Websocket session is not open")

        if self._pending_packets:
            return self._pending_packets.popleft()

        deadline = None if wait_timeout is None else asyncio.get_running_loop().time() + wait_timeout
        while True:
            parsed = self._parser.feed(b"")
            if parsed:
                self._pending_packets.extend(parsed)
                return self._pending_packets.popleft()

            if deadline is None:
                message = await ws.recv()
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise MQTTConnectionError("Timed out waiting for MQTT packet")
                message = await asyncio.wait_for(ws.recv(), timeout=remaining)

            data = message.encode("utf-8") if isinstance(message, str) else message
            parsed_packets = self._parser.feed(data)
            if parsed_packets:
                self._pending_packets.extend(parsed_packets)
                return self._pending_packets.popleft()

    async def _receive_loop(self) -> None:
        try:
            while self._connected:
                packet = await self._read_packet()
                await self._handle_packet(packet)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("MQTT receive loop failed")
            await self.disconnect()

    async def _handle_packet(self, packet: ParsedPacket) -> None:
        packet_bytes = (
            bytes([(packet.packet_type << 4) | packet.flags])
            + encode_remaining_length(len(packet.payload))
            + packet.payload
        )

        if packet.packet_type == _PacketType.SUBACK:
            packet_id, return_codes = parse_suback_packet(packet_bytes)
            waiter = self._suback_waiters.get(packet_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(return_codes)
            return

        if packet.packet_type == _PacketType.PUBLISH:
            publish = parse_publish_packet(packet_bytes)
            if publish.qos > 0:
                if publish.packet_id is None:
                    raise MQTTProtocolError("QoS publish missing packet identifier")
                await self._send_packet(build_puback_packet(publish.packet_id))
            await self._dispatch_publish(publish)
            return

        if packet.packet_type == _PacketType.PUBACK:
            _ = parse_puback_packet(packet_bytes)
            return

        if packet.packet_type == _PacketType.PINGRESP:
            if not is_pingresp_packet(packet_bytes):
                raise MQTTProtocolError("Malformed PINGRESP packet")
            self._ping_response_event.set()
            return

        if packet.packet_type == _PacketType.CONNACK:
            _LOGGER.debug("Ignoring unexpected CONNACK after initial connect")
            return

        raise MQTTProtocolError(f"Unsupported MQTT packet type received: {packet.packet_type}")

    async def _dispatch_publish(self, publish: ReceivedPublish) -> None:
        for callback in self._message_callbacks:
            result = callback(publish.topic, publish.payload, publish.qos)
            if inspect.isawaitable(result):
                await result

    async def _keepalive_loop(self) -> None:
        ping_timeout = max(5.0, self._keepalive * 0.75)
        try:
            while self._connected:
                await asyncio.sleep(self._keepalive)
                if not self._connected:
                    return
                self._ping_response_event.clear()
                await self._send_packet(build_pingreq_packet())
                try:
                    await asyncio.wait_for(self._ping_response_event.wait(), timeout=ping_timeout)
                except TimeoutError as err:
                    raise MQTTPingTimeoutError("Timed out waiting for PINGRESP") from err
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("MQTT keepalive loop failed")
            await self.disconnect()

    def _next_packet_id(self) -> int:
        self._packet_id = (self._packet_id % 0xFFFF) + 1
        return self._packet_id

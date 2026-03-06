"""Unit tests for WP-4 MQTT 3.1.1 codec and websocket transport."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.vivosun_growhub.const import (
    TOPIC_CHANNEL_APP,
    TOPIC_SHADOW_GET_ACCEPTED,
    TOPIC_SHADOW_UPDATE,
    TOPIC_SHADOW_UPDATE_ACCEPTED,
    TOPIC_SHADOW_UPDATE_DELTA,
    TOPIC_SHADOW_UPDATE_DOCUMENTS,
)
from custom_components.vivosun_growhub.mqtt_client import (
    MQTTClient,
    MQTTConnectionError,
    MQTTProtocolError,
    MQTTStreamParser,
    build_connect_packet,
    build_pingreq_packet,
    build_puback_packet,
    build_publish_packet,
    decode_remaining_length,
    encode_remaining_length,
    parse_connack_packet,
    parse_puback_packet,
    parse_publish_packet,
)


class _MockWebSocket:
    """Minimal async websocket used for transport tests."""

    def __init__(self, *, auto_pingresp: bool = False) -> None:
        self._incoming: asyncio.Queue[bytes | str | BaseException] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.closed = False
        self.auto_pingresp = auto_pingresp

    async def send(self, data: bytes) -> None:
        self.sent.append(data)
        if self.auto_pingresp and data == build_pingreq_packet():
            await self.feed_bytes(bytes([0xD0, 0x00]))

    async def recv(self) -> bytes | str:
        item = await self._incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    async def feed_bytes(self, data: bytes) -> None:
        await self._incoming.put(data)


def _connack_packet(return_code: int = 0) -> bytes:
    return bytes([0x20, 0x02, 0x00, return_code])


def build_suback_packet(packet_id: int, return_codes: list[int]) -> bytes:
    remaining = encode_remaining_length(2 + len(return_codes))
    payload = packet_id.to_bytes(2, "big") + bytes(return_codes)
    return bytes([0x90]) + remaining + payload


def _puback_packet(packet_id: int) -> bytes:
    return build_puback_packet(packet_id)


def _decode_subscribe_packet(packet: bytes) -> tuple[int, list[tuple[str, int]]]:
    assert packet[0] == 0x82
    remaining_length, consumed = decode_remaining_length(packet, 1)
    cursor = 1 + consumed
    packet_id = int.from_bytes(packet[cursor : cursor + 2], "big")
    cursor += 2
    end = cursor + (remaining_length - 2)
    topics: list[tuple[str, int]] = []

    while cursor < end:
        size = int.from_bytes(packet[cursor : cursor + 2], "big")
        cursor += 2
        topic = packet[cursor : cursor + size].decode("utf-8")
        cursor += size
        qos = packet[cursor]
        cursor += 1
        topics.append((topic, qos))

    return packet_id, topics


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"\x00"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (16383, b"\xff\x7f"),
        (16384, b"\x80\x80\x01"),
        (2097151, b"\xff\xff\x7f"),
        (2097152, b"\x80\x80\x80\x01"),
        (268435455, b"\xff\xff\xff\x7f"),
    ],
)
def test_remaining_length_varint_edge_cases(value: int, expected: bytes) -> None:
    encoded = encode_remaining_length(value)
    assert encoded == expected
    decoded, consumed = decode_remaining_length(encoded)
    assert decoded == value
    assert consumed == len(encoded)


def test_packet_encode_decode_supported_types() -> None:
    connect_packet = build_connect_packet(client_id="thing-1", keepalive=30)
    assert connect_packet[0] == 0x10

    session_present, return_code = parse_connack_packet(_connack_packet())
    assert session_present is False
    assert return_code == 0

    publish_packet = build_publish_packet(topic="a/b", payload=b'{"x":1}', qos=0, retain=True)
    publish = parse_publish_packet(publish_packet)
    assert publish.topic == "a/b"
    assert publish.payload == b'{"x":1}'
    assert publish.qos == 0
    assert publish.retain is True
    assert publish.packet_id is None

    publish_qos1_packet = build_publish_packet(topic="a/b", payload=b"q1", qos=1, packet_id=7)
    publish_qos1 = parse_publish_packet(publish_qos1_packet)
    assert publish_qos1.packet_id == 7

    assert parse_puback_packet(_puback_packet(11)) == 11

    assert build_pingreq_packet() == b"\xc0\x00"


def test_stream_parser_handles_fragmented_packets() -> None:
    parser = MQTTStreamParser()
    packet = _connack_packet() + bytes([0xD0, 0x00])

    assert parser.feed(packet[:2]) == []
    partial = parser.feed(packet[2:5])
    assert len(partial) == 1
    assert partial[0].packet_type == 2

    tail = parser.feed(packet[5:])
    assert len(tail) == 1
    assert tail[0].packet_type == 13


async def _connect_client_with_mock(ws: _MockWebSocket, *, keepalive: float = 60.0) -> MQTTClient:
    for packet in [_connack_packet(), build_suback_packet(1, [1, 1, 1, 1, 1])]:
        await ws.feed_bytes(packet)

    async def _connect_stub(uri: str, **kwargs: object) -> _MockWebSocket:
        _ = (uri, kwargs)
        return ws

    client = MQTTClient(
        websocket_url="wss://example/mqtt?sig=1",
        thing="thing-1",
        topic_prefix="prefix/123",
        keepalive_seconds=keepalive,
    )
    from custom_components.vivosun_growhub import mqtt_client

    original_connect = mqtt_client.websockets.connect
    mqtt_client.websockets.connect = _connect_stub
    try:
        await client.connect()
    finally:
        mqtt_client.websockets.connect = original_connect
    return client


async def test_connect_and_required_subscriptions_flow() -> None:
    ws = _MockWebSocket()
    client = await _connect_client_with_mock(ws)

    assert client.is_connected is True
    assert len(ws.sent) == 2
    assert ws.sent[0][0] == 0x10

    packet_id, subscribed_topics = _decode_subscribe_packet(ws.sent[1])
    assert packet_id == 1
    assert subscribed_topics == [
        (TOPIC_SHADOW_GET_ACCEPTED.format(thing="thing-1"), 1),
        (TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing="thing-1"), 1),
        (TOPIC_SHADOW_UPDATE_DOCUMENTS.format(thing="thing-1"), 1),
        (TOPIC_SHADOW_UPDATE_DELTA.format(thing="thing-1"), 1),
        (TOPIC_CHANNEL_APP.format(topic_prefix="prefix/123"), 1),
    ]

    await client.disconnect()
    assert ws.closed is True
    assert ws.sent[-1] == b"\xe0\x00"


async def test_connect_uses_explicit_client_id_when_provided() -> None:
    ws = _MockWebSocket()
    for packet in [_connack_packet(), build_suback_packet(1, [1, 1, 1, 1, 1])]:
        await ws.feed_bytes(packet)

    async def _connect_stub(uri: str, **kwargs: object) -> _MockWebSocket:
        _ = (uri, kwargs)
        return ws

    client = MQTTClient(
        websocket_url="wss://example/mqtt?sig=1",
        thing="thing-1",
        topic_prefix="prefix/123",
        client_id="custom-client-id",
    )
    from custom_components.vivosun_growhub import mqtt_client

    original_connect = mqtt_client.websockets.connect
    mqtt_client.websockets.connect = _connect_stub
    try:
        await client.connect()
    finally:
        mqtt_client.websockets.connect = original_connect

    connect_packet = ws.sent[0]
    assert b"custom-client-id" in connect_packet
    assert b"thing-1" not in connect_packet
    await client.disconnect()


async def test_publish_and_inbound_publish_callback_dispatch() -> None:
    ws = _MockWebSocket()
    client = await _connect_client_with_mock(ws)

    received: asyncio.Queue[tuple[str, bytes, int]] = asyncio.Queue()

    async def _callback(topic: str, payload: bytes, qos: int) -> None:
        await received.put((topic, payload, qos))

    client.add_message_callback(_callback)

    await ws.feed_bytes(build_publish_packet(topic="prefix/123/channel/app", payload=b"{}", qos=0))
    callback_topic, callback_payload, callback_qos = await asyncio.wait_for(received.get(), timeout=1.0)
    assert callback_topic == "prefix/123/channel/app"
    assert callback_payload == b"{}"
    assert callback_qos == 0

    await client.publish_shadow_update('{"state":{"desired":{}}}')
    assert ws.sent[-1] == build_publish_packet(
        topic=TOPIC_SHADOW_UPDATE.format(thing="thing-1"),
        payload=b'{"state":{"desired":{}}}',
        qos=0,
        retain=False,
    )

    await client.disconnect()


async def test_inbound_qos1_publish_sends_puback() -> None:
    ws = _MockWebSocket()
    client = await _connect_client_with_mock(ws)

    received: asyncio.Queue[tuple[str, bytes, int]] = asyncio.Queue()

    async def _callback(topic: str, payload: bytes, qos: int) -> None:
        await received.put((topic, payload, qos))

    client.add_message_callback(_callback)

    packet = build_publish_packet(topic="prefix/123/channel/app", payload=b'{"k":1}', qos=1, packet_id=321)
    await ws.feed_bytes(packet)

    callback_topic, callback_payload, callback_qos = await asyncio.wait_for(received.get(), timeout=1.0)
    assert callback_topic == "prefix/123/channel/app"
    assert callback_payload == b'{"k":1}'
    assert callback_qos == 1
    assert _puback_packet(321) in ws.sent

    await client.disconnect()


async def test_inbound_puback_is_ignored_without_receive_loop_crash() -> None:
    ws = _MockWebSocket()
    client = await _connect_client_with_mock(ws)

    received: asyncio.Queue[tuple[str, bytes, int]] = asyncio.Queue()

    async def _callback(topic: str, payload: bytes, qos: int) -> None:
        await received.put((topic, payload, qos))

    client.add_message_callback(_callback)

    await ws.feed_bytes(_puback_packet(1))
    await ws.feed_bytes(build_publish_packet(topic="prefix/123/channel/app", payload=b"{}", qos=0))

    callback_topic, callback_payload, callback_qos = await asyncio.wait_for(received.get(), timeout=1.0)
    assert callback_topic == "prefix/123/channel/app"
    assert callback_payload == b"{}"
    assert callback_qos == 0

    await client.disconnect()


async def test_keepalive_pingreq_and_pingresp() -> None:
    ws = _MockWebSocket(auto_pingresp=True)
    client = await _connect_client_with_mock(ws, keepalive=0.05)

    async def _wait_for_ping() -> None:
        for _ in range(40):
            if any(packet == b"\xc0\x00" for packet in ws.sent):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("PINGREQ was not sent")

    await asyncio.wait_for(_wait_for_ping(), timeout=1.0)
    await client.disconnect()


async def test_disconnect_idempotent_and_publish_requires_connection() -> None:
    ws = _MockWebSocket()
    client = await _connect_client_with_mock(ws)

    await client.disconnect()
    await client.disconnect()

    with pytest.raises(MQTTConnectionError):
        await client.publish("a/b", b"{}")


def test_invalid_remaining_length_raises_protocol_error() -> None:
    with pytest.raises(MQTTProtocolError):
        decode_remaining_length(b"\xff\xff\xff\xff\x01")

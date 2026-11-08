"""A22 native controls through real coordinator MQTT ingress and offline transport."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import time, timedelta
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.mqtt_client import MQTTConnectionError
from custom_components.vivosun_growhub.number import VivosunA22Number
from custom_components.vivosun_growhub.number import async_setup_entry as setup_numbers
from custom_components.vivosun_growhub.select import VivosunA22ModeSelect
from custom_components.vivosun_growhub.select import async_setup_entry as setup_selects
from custom_components.vivosun_growhub.switch import VivosunPortSwitch
from custom_components.vivosun_growhub.time import VivosunA22CycleStart
from custom_components.vivosun_growhub.time import async_setup_entry as setup_times

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _Mqtt:
    def __init__(self) -> None:
        self.is_connected = True
        self.published: list[tuple[str, dict[str, object]]] = []
        self.failure: Exception | None = None

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        assert qos == 1
        if self.failure:
            raise self.failure
        self.published.append((topic, json.loads(payload)))

    async def disconnect(self) -> None:
        self.is_connected = False


def _setup(hass: HomeAssistant) -> tuple[VivosunCoordinator, _Mqtt]:
    coordinator = VivosunCoordinator(hass, object(), email="offline@example.com", password="unused")
    coordinator._devices = [
        DeviceInfo("a22-1", "vivosun-VSPLUGA22-test", "a22/topic", "GrowHub A22", True, 42, "a22"),
        DeviceInfo("vgrow-1", "vivosun-VSCTLY18-test", "vgrow/topic", "VGrow", True, 42, "controller"),
    ]
    coordinator._build_topic_maps()
    mqtt = _Mqtt()
    coordinator._mqtt_client = cast("object", mqtt)
    coordinator.data = coordinator._build_state_snapshot()
    return coordinator, mqtt


async def _receive(coordinator: VivosunCoordinator, thing: str, suffix: str,
                   body: dict[str, object]) -> None:
    await coordinator._handle_mqtt_publish(
        f"$aws/things/{thing}/shadow/{suffix}", json.dumps(body).encode(), 1
    )


def _report(port: str, fields: dict[str, object], version: int = 100,
            timestamp: int = 1000) -> dict[str, object]:
    def metadata(raw: dict[str, object]) -> dict[str, object]:
        return {key: metadata(value) if isinstance(value, dict) else {"timestamp": timestamp}
                for key, value in raw.items()}

    return {
        "version": version,
        "state": {"reported": {"connected": True, port: fields}},
        "metadata": {"reported": {port: metadata(fields)}},
    }


_FIELDS: dict[str, object] = {
    "mode": 2, "inPlan": 0, "lv": 100,
    "cycle": {"tOffset": 61200, "onDur": 300, "offDur": 300},
    "auto": {"tMin": 0, "tMax": 2110, "hMin": 5000, "hMax": 0, "tHys": 150},
}


async def test_reported_per_port_proof_and_pair_preservation(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        settings = coordinator.a22_settings("a22-1", "usb1")
        assert (settings.cycle.start, settings.cycle.on, settings.cycle.off) == (61200, 300, 300)
        assert (settings.auto.temp_high, settings.auto.humidity_low) == (2110, 5000)
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 600)
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {
            "cycle": {"onDur": 600, "offDur": 300},
        }}}
        assert coordinator.a22_settings("a22-1", "usb1").cycle.on == 300
        # An ACK and cached/older GET cannot confirm our requested settings.
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "state": {"desired": {"usb1": {"cycle": {"onDur": 600}}}},
            "clientToken": mqtt.published[-1][1]["clientToken"],
        })
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"cycle": {"onDur": 120}}, 99, 900,
        ))
        assert coordinator.a22_settings("a22-1", "usb1").cycle.on == 300
        with pytest.raises(ServiceValidationError, match="pending"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"cycle": {"onDur": 600}}, 102, 1002,
        ))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {
            "cycle": {"onDur": 600, "offDur": 120},
        }}}
    finally:
        await coordinator.async_shutdown()


async def test_unsupported_unknown_and_disconnected_commands_fail_closed(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        with pytest.raises(ServiceValidationError):
            await coordinator.async_set_a22_mode("a22-1", "plug1", 1)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", {
            "state": {"reported": {"usb1": _FIELDS}},
        })
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 0)
        with pytest.raises(ServiceValidationError):
            await coordinator.async_set_a22_mode("vgrow-1", "usb1", 1)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "plug1", {**_FIELDS, "inPlan": 1}, 100, 1000,
        ))
        with pytest.raises(ServiceValidationError, match="plan"):
            await coordinator.async_set_a22_mode("a22-1", "plug1", 1)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "plug1", {"inPlan": 0}, 101, 1001,
        ))
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 102, "state": {"reported": {"connected": False}},
        })
        with pytest.raises(HomeAssistantError, match="unavailable"):
            await coordinator.async_set_a22_mode("a22-1", "plug1", 1)
        assert mqtt.published == []
    finally:
        await coordinator.async_shutdown()


async def test_native_platforms_include_all_ports_without_vgrow_controls(hass: HomeAssistant) -> None:
    coordinator, _ = _setup(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="a22", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, coordinator)
    selects: list[object] = []
    numbers: list[object] = []
    times: list[object] = []
    try:
        await setup_selects(hass, entry, selects.extend)
        await setup_numbers(hass, entry, numbers.extend)
        await setup_times(hass, entry, times.extend)
        assert len([item for item in selects if isinstance(item, VivosunA22ModeSelect)]) == 4
        assert len([item for item in numbers if isinstance(item, VivosunA22Number)]) == 24
        assert len([item for item in times if isinstance(item, VivosunA22CycleStart)]) == 4
        assert all("vgrow-1" not in item.unique_id for item in (*selects, *numbers, *times))
        assert len({item.unique_id for item in (*selects, *numbers, *times)}) == 32
        assert len({str(item.device_info["identifiers"]) for item in (*selects, *numbers, *times)}) == 1
    finally:
        await coordinator.async_shutdown()


async def test_a22_platforms_follow_later_discovery_without_a10_leakage(hass: HomeAssistant) -> None:
    coordinator, _ = _setup(hass)
    devices = coordinator.devices
    coordinator._devices = []
    entry = MockConfigEntry(domain=DOMAIN, title="a22", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, coordinator)
    selects: list[object] = []
    numbers: list[object] = []
    times: list[object] = []
    try:
        await setup_selects(hass, entry, selects.extend)
        await setup_numbers(hass, entry, numbers.extend)
        await setup_times(hass, entry, times.extend)
        assert not selects and not numbers and not times
        coordinator._devices = [DeviceInfo(
            "a10-1", "vivosun-VSPLUGA10-test", "a10/topic", "GrowHub A10", True, 42, "a10",
        ), *devices]
        for callback, cameras_only in tuple(coordinator._device_listeners):
            assert not cameras_only
            callback(coordinator.devices)
            callback(coordinator.devices)
        assert len(selects) == 4
        assert len(numbers) == 24
        assert len(times) == 4
        assert all("a10-1" not in entity.unique_id for entity in (*selects, *numbers, *times))
    finally:
        await coordinator.async_shutdown()


async def test_native_ha_time_number_select_services_and_manual_sequence(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="a22", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, coordinator)
    assert await async_setup_component(hass, "select", {})
    assert await async_setup_component(hass, "number", {})
    assert await async_setup_component(hass, "time", {})
    platforms: list[EntityPlatform] = []
    entity_ids: dict[str, str] = {}
    try:
        for domain, entity in (
            ("select", VivosunA22ModeSelect(coordinator, "a22-1", "usb1")),
            ("number", VivosunA22Number(coordinator, "a22-1", "usb1", "tMax")),
            ("time", VivosunA22CycleStart(coordinator, "a22-1", "usb1")),
        ):
            platform = EntityPlatform(
                hass=hass, logger=logging.getLogger(__name__), domain=domain,
                platform_name="vivosun_growhub", platform=None,
                scan_interval=timedelta(seconds=30), entity_namespace=None,
            )
            await platform.async_add_entities([entity])
            assert entity.entity_id is not None
            entity_ids[domain] = entity.entity_id
            platforms.append(platform)
        await hass.async_block_till_done()
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        assert coordinator.a22_settings("a22-1", "usb1").cycle.start == 61200
        assert VivosunA22CycleStart(coordinator, "a22-1", "usb1").native_value == time(17)
        assert VivosunA22Number(coordinator, "a22-1", "usb1", "tMax").native_value == 21.1
        await hass.services.async_call("time", "set_value", {
            "entity_id": entity_ids["time"], "time": "18:30:00",
        }, blocking=True)
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {"cycle": {"tOffset": 66600}}}}
        await hass.services.async_call("number", "set_value", {
            "entity_id": entity_ids["number"], "value": 23.5,
        }, blocking=True)
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {
            "auto": {"tMin": 0, "tMax": 2350},
        }}}
        await hass.services.async_call("select", "select_option", {
            "entity_id": entity_ids["select"], "option": "Manual",
        }, blocking=True)
        assert [entry[1]["state"] for entry in mqtt.published[-2:]] == [
            {"desired": {"usb1": {"mode": 0}}},
            {"desired": {"usb1": {"manu": {"lv": 100}}}},
        ]
    finally:
        await coordinator.async_shutdown()
        for platform in platforms:
            await platform.async_reset()


async def test_manual_first_and_second_publish_failures_do_not_retry(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        mqtt.failure = RuntimeError("offline send failed")
        with pytest.raises(RuntimeError, match="offline send failed"):
            await coordinator.async_set_a22_mode("a22-1", "usb1", 0)
        assert not mqtt.published
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_mode("a22-1", "usb1", 0)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"mode": 2}, 102, 1002,
        ))
        mqtt.failure = None
        original = coordinator.async_publish_shadow_update

        async def fail_second(payload: dict[str, object], *, device_id: str, qos: int) -> None:
            desired = payload["state"]["desired"]["usb1"]
            if "manu" in desired:
                raise RuntimeError("manual target failed")
            await original(payload, device_id=device_id, qos=qos)

        coordinator.async_publish_shadow_update = fail_second
        with pytest.raises(RuntimeError, match="manual target failed"):
            await coordinator.async_set_a22_mode("a22-1", "usb1", 0)
        assert len(mqtt.published) == 1
        assert mqtt.published[0][1]["state"] == {"desired": {"usb1": {"mode": 0}}}
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_mode("a22-1", "usb1", 1)
    finally:
        await coordinator.async_shutdown()


async def test_newer_manual_off_supersedes_delayed_mode_completion(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    assert await async_setup_component(hass, "switch", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="switch",
        platform_name="vivosun_growhub", platform=None,
        scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    switch = VivosunPortSwitch(coordinator, "a22-1", "usb1")
    await platform.async_add_entities([switch])
    await hass.async_block_till_done()
    assert switch.entity_id is not None
    mode_sent = asyncio.Event()
    resume = asyncio.Event()
    original = mqtt.publish

    async def gate(topic: str, payload: bytes, qos: int = 0) -> None:
        await original(topic, payload, qos)
        if json.loads(payload)["state"]["desired"]["usb1"].get("mode") == 0:
            mode_sent.set()
            await resume.wait()

    mqtt.publish = gate
    task: asyncio.Task[None] | None = None
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        task = asyncio.create_task(coordinator.async_set_a22_mode("a22-1", "usb1", 0))
        await asyncio.wait_for(mode_sent.wait(), timeout=2)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"mode": 0, "lv": 100}, 101, 1001,
        ))
        await hass.services.async_call("switch", "turn_off", {
            "entity_id": switch.entity_id,
        }, blocking=True)
        resume.set()
        await task
        assert [body["state"]["desired"]["usb1"] for _, body in mqtt.published] == [
            {"mode": 0}, {"manu": {"lv": 0}},
        ]
    finally:
        resume.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.async_shutdown()
        await platform.async_reset()


async def test_partial_metadata_never_reopens_stale_no_plan_guard(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "state": {"reported": {"usb1": {"inPlan": 1}}},
        })
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"inPlan": 0}, 102, 900,
        ))
        assert coordinator.a22_settings("a22-1", "usb1").status.in_plan == 1
        with pytest.raises(ServiceValidationError):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        assert mqtt.published == []
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"inPlan": 0}, 103, 1003,
        ))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("clear", [
    {"cycle": None, "auto": None},
    {"cycle": "invalid", "auto": []},
    {"cycle": {"onDur": None}, "auto": {"tMax": "invalid"}},
    None,
    "invalid",
])
async def test_explicit_port_parent_and_scalar_clears_revoke_pair_authority(
    hass: HomeAssistant, clear: object,
) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "state": {"reported": {"usb1": clear}},
            "metadata": {"reported": {"usb1": {"timestamp": 1001}}},
        })
        settings = coordinator.a22_settings("a22-1", "usb1")
        assert settings.cycle.on is None
        assert settings.auto.temp_high is None
        with pytest.raises(ServiceValidationError):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 600)
        with pytest.raises(ServiceValidationError):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "auto", "tMax", 2350)
        assert mqtt.published == []
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"cycle": {"onDur": 300}, "auto": {"tMax": 2110}}, 100, 900,
        ))
        assert coordinator.a22_settings("a22-1", "usb1").cycle.on is None
    finally:
        await coordinator.async_shutdown()


async def test_existing_a22_power_switch_uses_same_fresh_guard_as_mode(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    switch = VivosunPortSwitch(coordinator, "a22-1", "usb1")
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {**_FIELDS, "mode": 0}, 100, 1000,
        ))
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"mode": 2}, 101, 1001,
        ))
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"mode": 0}, 99, 900,
        ))
        assert switch.extra_state_attributes["mode_code"] == 2
        assert switch.is_on is True
        with pytest.raises(ServiceValidationError):
            await switch.async_turn_off()
        assert mqtt.published == []
    finally:
        await coordinator.async_shutdown()


async def test_reconnect_identical_genuine_get_restores_a22_write_authority(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        baseline = _report("usb1", _FIELDS)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", baseline)
        await coordinator._disconnect_mqtt_client()
        mqtt = _Mqtt()
        coordinator._mqtt_client = cast("object", mqtt)
        with pytest.raises(ServiceValidationError):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", baseline)
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


async def test_stale_high_version_get_and_mismatched_report_keep_pair_pending(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 600)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"cycle": {"onDur": 300, "offDur": 300}}, 150, 1001,
        ))
        with pytest.raises(ServiceValidationError, match="pending"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"cycle": {"onDur": 600, "offDur": 200}}, 151, 1002,
        ))
        with pytest.raises(ServiceValidationError, match="pending"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"cycle": {"onDur": 600, "offDur": 300}}, 152, 1003,
        ))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        assert len(mqtt.published) == 2
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("deny_optional", [False, True])
async def test_a22_live_connect_subscribes_rejections_best_effort(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, deny_optional: bool,
) -> None:
    coordinator, _ = _setup(hass)
    subscriptions: list[tuple[str, int]] = []
    attempted: list[tuple[str, int]] = []

    class _Transport:
        def __init__(self, **kwargs: object) -> None:
            self.is_connected = False

        async def connect(self) -> None:
            self.is_connected = True

        async def disconnect(self) -> None:
            self.is_connected = False

        def add_message_callback(self, callback: object) -> None:
            pass

        async def subscribe(self, topics: list[tuple[str, int]]) -> None:
            attempted.extend(topics)
            if deny_optional and any(topic.endswith("/update/rejected") for topic, _ in topics):
                raise MQTTConnectionError("optional subscription denied")
            subscriptions.extend(topics)

        async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
            assert topic.endswith("/shadow/get")

    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator.MQTTClient", _Transport)
    monkeypatch.setattr(coordinator, "_mqtt_websocket_url", lambda: "wss://offline.invalid")
    try:
        await coordinator._connect_mqtt()
        assert coordinator.is_mqtt_connected
        assert ("$aws/things/vivosun-VSPLUGA22-test/shadow/update/rejected", 1) in attempted
        if deny_optional:
            # A rejected optional subscription must not disable the required shadow topics.
            assert ("$aws/things/vivosun-VSPLUGA22-test/shadow/update/accepted", 1) in subscriptions
        else:
            assert ("$aws/things/vivosun-VSCTLY18-test/shadow/update/rejected", 1) in subscriptions
    finally:
        await coordinator.async_shutdown()


async def test_first_manual_publish_rejection_after_report_prevents_stale_second(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    mode_sent = asyncio.Event()
    resume = asyncio.Event()
    original = mqtt.publish

    async def gate(topic: str, payload: bytes, qos: int = 0) -> None:
        await original(topic, payload, qos)
        if json.loads(payload)["state"]["desired"]["usb1"].get("mode") == 0:
            mode_sent.set()
            await resume.wait()

    mqtt.publish = gate
    task: asyncio.Task[None] | None = None
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        task = asyncio.create_task(coordinator.async_set_a22_mode("a22-1", "usb1", 0))
        await asyncio.wait_for(mode_sent.wait(), 2)
        token = mqtt.published[-1][1]["clientToken"]
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"mode": 0}, 101, 1001,
        ))
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/rejected", {
            "clientToken": token, "code": 403,
        })
        resume.set()
        await task
        assert [body["state"]["desired"]["usb1"] for _, body in mqtt.published] == [{"mode": 0}]
    finally:
        resume.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.async_shutdown()


async def test_second_manual_publish_rejection_invalidates_output_proof(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    manual_sent = asyncio.Event()
    resume = asyncio.Event()
    original = mqtt.publish

    async def gate(topic: str, payload: bytes, qos: int = 0) -> None:
        await original(topic, payload, qos)
        if "manu" in json.loads(payload)["state"]["desired"]["usb1"]:
            manual_sent.set()
            await resume.wait()

    mqtt.publish = gate
    task: asyncio.Task[None] | None = None
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        task = asyncio.create_task(coordinator.async_set_a22_mode("a22-1", "usb1", 0))
        await asyncio.wait_for(manual_sent.wait(), 2)
        token = mqtt.published[-1][1]["clientToken"]
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/rejected", {
            "clientToken": token, "code": 403,
        })
        resume.set()
        await task
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_mode("a22-1", "usb1", 0)
        assert len(mqtt.published) == 2
    finally:
        resume.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.async_shutdown()


async def test_a22_reported_snapshot_preserves_unrelated_auto_and_accessory_fields(hass: HomeAssistant) -> None:
    coordinator, _ = _setup(hass)
    try:
        reported = {**_FIELDS, "dev": 501, "manu": {"lv": 0},
                    "auto": {**_FIELDS["auto"], "hHys": 400}}
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("plug1", reported))
        port = coordinator.data["shadows"]["a22-1"]["ports"]["plug1"]
        assert port["dev"] == 501
        assert port["manu"]["lv"] == 0
        assert port["auto"]["tHys"] == 150
        assert port["auto"]["hHys"] == 400
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "plug1", {"auto": {"tMax": 2350}}, 101, 1001,
        ))
        port = coordinator.data["shadows"]["a22-1"]["ports"]["plug1"]
        assert (port["auto"]["tMax"], port["auto"]["tHys"], port["auto"]["hHys"]) == (2350, 150, 400)
    finally:
        await coordinator.async_shutdown()


async def test_a22_pair_recovers_after_timeout_or_rejection_with_unchanged_current_read(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 600)
        key = ("a22-1", "usb1", ("cycle", "onDur"))
        first = coordinator._a22_pending[key]
        coordinator._a22_expire(key, first.token)
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"cycle": {"onDur": 300, "offDur": 300}}, 101, 1000,
        ))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        second = mqtt.published[-1][1]["clientToken"]
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/rejected", {
            "clientToken": second, "code": 403,
        })
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 600)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"cycle": {"onDur": 300, "offDur": 300}}, 102, 1000,
        ))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 600)
        assert len(mqtt.published) == 3
    finally:
        await coordinator.async_shutdown()


async def test_queued_mode_supersedes_old_manual_second_without_blocking_other_port(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    first_sent = asyncio.Event()
    resume = asyncio.Event()
    original = mqtt.publish

    async def gate(topic: str, payload: bytes, qos: int = 0) -> None:
        await original(topic, payload, qos)
        desired = json.loads(payload)["state"]["desired"]
        if desired.get("usb1", {}).get("mode") == 0:
            first_sent.set()
            await resume.wait()

    mqtt.publish = gate
    tasks: list[asyncio.Task[None]] = []
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("plug2", _FIELDS))
        tasks.append(asyncio.create_task(coordinator.async_set_a22_mode("a22-1", "usb1", 0)))
        await asyncio.wait_for(first_sent.wait(), 2)
        tasks.append(asyncio.create_task(coordinator.async_set_a22_mode("a22-1", "usb1", 1)))
        await coordinator.async_set_a22_setting("a22-1", "plug2", "cycle", "tOffset", 66600)
        assert len(mqtt.published) == 2  # Another port is not blocked by USB1's transport.
        resume.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert results[0] is None
        assert results[1] is None
        assert [entry[1]["state"]["desired"] for entry in mqtt.published] == [
            {"usb1": {"mode": 0}}, {"plug2": {"cycle": {"tOffset": 66600}}},
            {"usb1": {"mode": 1}},
        ]
    finally:
        resume.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await coordinator.async_shutdown()


async def test_rejected_pair_edit_does_not_supersede_existing_pending_token(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 600)
        token = mqtt.published[-1][1]["clientToken"]
        with pytest.raises(ServiceValidationError, match="pending"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "offDur", 120)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/rejected", {
            "clientToken": token, "code": 403,
        })
        assert ("a22-1", "usb1", ("cycle", "onDur")) not in coordinator._a22_pending
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 120)
    finally:
        await coordinator.async_shutdown()


async def test_concurrent_independent_pairs_keep_independent_rejection_correlation(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "auto", "tMax", 2350)
        first = mqtt.published[-1][1]["clientToken"]
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        second = mqtt.published[-1][1]["clientToken"]
        assert first != second and len(coordinator._a22_pending) == 2
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/rejected", {
            "clientToken": first, "code": 403,
        })
        assert ("a22-1", "usb1", ("auto", "tMin")) not in coordinator._a22_pending
        assert ("a22-1", "usb1", ("cycle", "tOffset")) in coordinator._a22_pending
    finally:
        await coordinator.async_shutdown()


async def test_stale_nested_null_get_cannot_clear_newer_cycle_auto_or_guard(hass: HomeAssistant) -> None:
    """An ignored stale parent clear must not mutate either the UI or write proof."""
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        for stale in ({"cycle": None, "auto": None}, {"cycle": 4, "auto": "invalid"}):
            await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
                "usb1", stale, 99, 900,
            ))
            values = coordinator.a22_settings("a22-1", "usb1")
            assert (values.cycle.on, values.auto.temp_high) == (300, 2110)
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {"cycle": {"tOffset": 66600}}}}
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"cycle": None, "auto": None}, 102, 1002,
        ))
        assert coordinator.a22_settings("a22-1", "usb1").cycle.on is None
        assert coordinator.a22_settings("a22-1", "usb1").auto.temp_high is None
    finally:
        await coordinator.async_shutdown()


async def test_a22_switch_service_cannot_use_cached_manual_facts_after_disconnect(hass: HomeAssistant) -> None:
    """An online transport alone never restores the previous session's write permission."""
    coordinator, _ = _setup(hass)
    assert await async_setup_component(hass, "switch", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="switch",
        platform_name="vivosun_growhub", platform=None,
        scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    switch = VivosunPortSwitch(coordinator, "a22-1", "usb1")
    await platform.async_add_entities([switch])
    await hass.async_block_till_done()
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {**_FIELDS, "mode": 0},
        ))
        await coordinator._disconnect_mqtt_client()
        replacement = _Mqtt()
        coordinator._mqtt_client = cast("object", replacement)
        assert not coordinator._a22_provenance
        with pytest.raises(ServiceValidationError, match="fresh"):
            await hass.services.async_call("switch", "turn_off", {
                "entity_id": switch.entity_id,
            }, blocking=True)
        assert replacement.published == []
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {**_FIELDS, "mode": 0},
        ))
        await hass.services.async_call("switch", "turn_off", {
            "entity_id": switch.entity_id,
        }, blocking=True)
        assert replacement.published[-1][1]["state"] == {"desired": {"usb1": {"manu": {"lv": 0}}}}
    finally:
        await coordinator.async_shutdown()
        await platform.async_reset()


async def test_same_second_reported_write_after_desired_ack_confirms_a22_start(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        payload = mqtt.published[-1][1]
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "clientToken": payload["clientToken"], "state": payload["state"],
            "metadata": {"desired": {"usb1": {"cycle": {"tOffset": {"timestamp": 1000}}}}},
        })
        assert coordinator.a22_settings("a22-1", "usb1").cycle.start == 61200
        assert len(coordinator._a22_pending) == 1
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"cycle": {"tOffset": 66600}}, 102, 1000,
        ))
        assert coordinator.a22_settings("a22-1", "usb1").cycle.start == 66600
        assert not coordinator._a22_pending
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 70000)
        assert len(mqtt.published) == 2
    finally:
        await coordinator.async_shutdown()


async def test_already_reported_a22_mode_and_pair_do_not_create_pending_timers(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_mode("a22-1", "usb1", 2)
        token = mqtt.published[-1][1]["clientToken"]
        assert not coordinator._a22_pending
        assert token in coordinator._a22_receipts
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "clientToken": token, "state": mqtt.published[-1][1]["state"],
        })
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"mode": 2}, 102, 1000,
        ))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 300)
        assert not coordinator._a22_pending
        assert len(coordinator._a22_receipts) >= 2
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 61200)
        assert not coordinator._a22_pending
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        assert len(coordinator._a22_pending) == 1
    finally:
        await coordinator.async_shutdown()


async def test_noop_replacement_of_pending_pair_is_tracked_not_left_blocking(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        first_token = mqtt.published[-1][1]["clientToken"]
        # The second explicit edit restores the genuinely reported value while the old command is in flight.
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 61200)
        second_token = mqtt.published[-1][1]["clientToken"]
        assert first_token != second_token
        assert len(coordinator._a22_pending) == 1
        assert next(iter(coordinator._a22_pending.values())).token == second_token
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/rejected", {
            "clientToken": first_token, "code": 403,
        })
        assert next(iter(coordinator._a22_pending.values())).token == second_token
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "clientToken": second_token, "state": mqtt.published[-1][1]["state"],
        })
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"cycle": {"tOffset": 61200}}, 102, 1000,
        ))
        assert not coordinator._a22_pending
    finally:
        await coordinator.async_shutdown()


async def test_timeout_recovery_reuses_fresh_unchanged_read_without_execution_claim(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        key, pending = next(iter(coordinator._a22_pending.items()))
        coordinator._a22_expire(key, pending.token)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"cycle": {"tOffset": 61200}}, 100, 1000,
        ))
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 70000)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"cycle": {"tOffset": 61200}}, 101, 900,
        ))
        with pytest.raises(ServiceValidationError, match="fresh"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 70000)
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"cycle": {"tOffset": 61200}}, 101, 1000,
        ))
        assert coordinator.a22_settings("a22-1", "usb1").cycle.start == 61200
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 70000)
        assert len(mqtt.published) == 2
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {"cycle": {"tOffset": 70000}}}}
    finally:
        await coordinator.async_shutdown()


async def test_a22_reconnect_ignores_mqtt_callbacks_from_previous_session(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old transport cannot reauthorize writes after the session boundary."""
    coordinator, _ = _setup(hass)

    class _Transport:
        instances: ClassVar[list[_Transport]] = []

        def __init__(self, **kwargs: object) -> None:
            self.is_connected = False
            self.callback: object = None
            self.instances.append(self)

        async def connect(self) -> None:
            self.is_connected = True

        async def disconnect(self) -> None:
            self.is_connected = False

        def add_message_callback(self, callback: object) -> None:
            self.callback = callback

        async def subscribe(self, topics: list[tuple[str, int]]) -> None:
            pass

        async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
            assert topic.endswith("/shadow/get")

        async def emit(self, suffix: str, message: dict[str, object]) -> None:
            assert callable(self.callback)
            await self.callback(
                f"$aws/things/vivosun-VSPLUGA22-test/shadow/{suffix}",
                json.dumps(message).encode(), 1,
            )

    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator.MQTTClient", _Transport)
    monkeypatch.setattr(coordinator, "_mqtt_websocket_url", lambda: "wss://offline.invalid")
    try:
        await coordinator._connect_mqtt()
        old = _Transport.instances[-1]
        await old.emit("get/accepted", _report("usb1", _FIELDS))
        await coordinator._connect_mqtt()
        current = _Transport.instances[-1]
        assert current is not old
        await old.emit("update/accepted", _report("usb1", {"cycle": {"tOffset": 66600}}, 102, 1002))
        assert coordinator.a22_settings("a22-1", "usb1").cycle.start == 61200
        assert ("a22-1", "usb1", ("cycle", "tOffset")) not in coordinator._a22_provenance
        await current.emit("get/accepted", _report("usb1", {"cycle": {"tOffset": 66600}}, 100, 1000))
        assert coordinator.a22_settings("a22-1", "usb1").cycle.start == 61200
        await current.emit("get/accepted", _report("usb1", _FIELDS))
        assert ("a22-1", "usb1", ("cycle", "tOffset")) in coordinator._a22_provenance
    finally:
        await coordinator.async_shutdown()


async def test_manual_power_send_failure_needs_unchanged_fresh_read_before_retry(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    switch = VivosunPortSwitch(coordinator, "a22-1", "usb1")
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {**_FIELDS, "mode": 0},
        ))
        mqtt.failure = RuntimeError("failed offline send")
        with pytest.raises(RuntimeError, match="failed offline send"):
            await switch.async_turn_off()
        mqtt.failure = None
        with pytest.raises(ServiceValidationError, match="fresh"):
            await switch.async_turn_off()
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {**_FIELDS, "mode": 0}, 101, 1000,
        ))
        await switch.async_turn_off()
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {"manu": {"lv": 0}}}}
    finally:
        await coordinator.async_shutdown()


async def test_already_reported_mode_replaces_pending_mode_without_false_noop_lock(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_mode("a22-1", "usb1", 1)
        original = mqtt.published[-1][1]["clientToken"]
        await coordinator.async_set_a22_mode("a22-1", "usb1", 2)
        latest = mqtt.published[-1][1]["clientToken"]
        assert original != latest
        assert len(coordinator._a22_pending) == 1
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/rejected", {
            "clientToken": original, "code": 403,
        })
        assert next(iter(coordinator._a22_pending.values())).token == latest
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "clientToken": latest, "state": mqtt.published[-1][1]["state"],
        })
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"mode": 2}, 102, 1000,
        ))
        assert not coordinator._a22_pending
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
    finally:
        await coordinator.async_shutdown()


async def test_noop_pair_replacement_needs_only_changed_leaf_to_confirm(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 600)
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "onDur", 300)
        token = mqtt.published[-1][1]["clientToken"]
        assert next(iter(coordinator._a22_pending.values())).confirmed == {("cycle", "offDur")}
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "clientToken": token, "state": mqtt.published[-1][1]["state"],
        })
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", _report(
            "usb1", {"cycle": {"onDur": 300}}, 102, 1000,
        ))
        assert not coordinator._a22_pending
    finally:
        await coordinator.async_shutdown()


async def test_rejected_a22_mode_revokes_legacy_manual_power_until_new_read(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    switch = VivosunPortSwitch(coordinator, "a22-1", "usb1")
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {**_FIELDS, "mode": 0},
        ))
        await coordinator.async_set_a22_mode("a22-1", "usb1", 1)
        token = mqtt.published[-1][1]["clientToken"]
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/rejected", {
            "clientToken": token, "code": 403,
        })
        with pytest.raises(ServiceValidationError, match="fresh"):
            await switch.async_turn_off()
        assert len(mqtt.published) == 1
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report(
            "usb1", {"mode": 0}, 101, 1000,
        ))
        await switch.async_turn_off()
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {"manu": {"lv": 0}}}}
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("timestamp", [1000, 1001])
@pytest.mark.parametrize("order", ["documents-first", "accepted-first", "documents-only"])
async def test_a22_reported_transition_confirms_in_either_topic_order(
    hass: HomeAssistant, timestamp: int, order: str,
) -> None:
    """The capture delivers genuine documents before their accepted counterpart."""
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        sent = mqtt.published[-1][1]
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "clientToken": sent["clientToken"], "state": sent["state"],
        })
        previous = _report("usb1", _FIELDS, 101, 1000)
        current = _report("usb1", {"cycle": {"tOffset": 66600}}, 102, timestamp)
        documents = {"previous": previous, "current": current}
        messages = [("update/documents", documents), ("update/accepted", current)]
        if order == "accepted-first":
            messages.reverse()
        elif order == "documents-only":
            messages = messages[:1]
        for suffix, body in messages:
            await _receive(coordinator, "vivosun-VSPLUGA22-test", suffix, body)
        assert coordinator.a22_settings("a22-1", "usb1").cycle.start == 66600
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 70000)
        assert mqtt.published[-1][1]["state"] == {"desired": {"usb1": {"cycle": {"tOffset": 70000}}}}
    finally:
        await coordinator.async_shutdown()


async def test_a22_unchanged_documents_do_not_confirm_a_pending_replacement(hass: HomeAssistant) -> None:
    coordinator, mqtt = _setup(hass)
    try:
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "get/accepted", _report("usb1", _FIELDS))
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 66600)
        await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 61200)
        sent = mqtt.published[-1][1]
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/accepted", {
            "version": 101, "clientToken": sent["clientToken"], "state": sent["state"],
        })
        await _receive(coordinator, "vivosun-VSPLUGA22-test", "update/documents", {
            "previous": _report("usb1", _FIELDS, 101, 1000),
            "current": _report("usb1", _FIELDS, 102, 1000),
        })
        with pytest.raises(ServiceValidationError, match="pending"):
            await coordinator.async_set_a22_setting("a22-1", "usb1", "cycle", "tOffset", 70000)
        assert len(mqtt.published) == 2
    finally:
        await coordinator.async_shutdown()

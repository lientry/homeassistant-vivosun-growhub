"""Manual setting memory through local entities and real shadow ingress."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
from homeassistant.components.fan import FanEntityFeature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import (
    CFAN_LEVEL_MAP,
    DFAN_LEVEL_MAP,
    DOMAIN,
    OPTION_CFAN_OSCILLATION_CONTROLLERS,
)
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.fan import VivosunCirculationFanEntity, VivosunDuctFanEntity
from custom_components.vivosun_growhub.light import VivosunLightEntity
from custom_components.vivosun_growhub.manual_preferences import ManualPreferences
from custom_components.vivosun_growhub.models import DeviceInfo
from custom_components.vivosun_growhub.select import VivosunSpectrumSelect

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _Mqtt:
    def __init__(self) -> None:
        self.is_connected = True
        self.published: list[tuple[str, dict[str, object]]] = []
        self.failure: Exception | None = None

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        assert qos == 0
        if self.failure:
            raise self.failure
        self.published.append((topic, json.loads(payload)))

    async def disconnect(self) -> None:
        self.is_connected = False


def _devices() -> list[DeviceInfo]:
    return [
        DeviceInfo(
            f"device-{i}", f"thing-{i}", f"channel-{i}", f"GrowHub {i}", True, 1,
            "controller",
        )
        for i in (1, 2)
    ]


async def _setup(
    hass: HomeAssistant, *, channels: tuple[tuple[str, str], ...] = (("device-1", "light"),),
    entry_id: str = "memory-integration",
) -> tuple[VivosunCoordinator, _Mqtt, ManualPreferences]:
    coordinator = VivosunCoordinator(hass, object(), email="none@example.invalid", password="unused")
    coordinator._devices = _devices()
    coordinator._build_topic_maps()
    mqtt = _Mqtt()
    coordinator._mqtt_client = cast("object", mqtt)
    prefs = ManualPreferences(hass, entry_id, bool(channels))
    await prefs.async_load()
    coordinator.manual_preferences = prefs
    coordinator.data = coordinator._build_state_snapshot()
    return coordinator, mqtt, prefs


async def _ingress(coordinator: VivosunCoordinator, device: int, suffix: str, payload: dict[str, object]) -> None:
    await coordinator._handle_mqtt_publish(
        f"$aws/things/thing-{device}/shadow/{suffix}", json.dumps(payload).encode(), 1
    )


async def _report(
    coordinator: VivosunCoordinator, device: int, channel: str, level: int,
    *, mode: int = 0, in_plan: object = 0, version: int = 1,
) -> None:
    await _ingress(coordinator, device, "get/accepted", {
        "version": version,
        "state": {"reported": {channel: {"lv": level, "mode": mode, "inPlan": in_plan}}},
    })


async def _ack(coordinator: VivosunCoordinator, device: int, command: dict[str, object], version: int) -> None:
    await _ingress(coordinator, device, "update/accepted", {
        "version": version, "clientToken": command["clientToken"],
        "state": {"desired": command["state"]["desired"]},  # type: ignore[index]
    })


async def test_store_reloads_valid_channels_and_rejects_malformed_saved_values(hass: HomeAssistant) -> None:
    prefs = ManualPreferences(hass, "round-trip", True)
    await prefs.async_load()
    await prefs.async_remember("device-1", "light", 60)
    await prefs.async_remember("device-1", "cFan", 70)

    loaded = ManualPreferences(hass, "round-trip", True)
    await loaded.async_load()
    assert loaded.selection("device-1", "light") == 60
    assert loaded.selection("device-1", "cFan") == 70
    assert loaded.selection("device-1", "dFan") is None
    assert loaded.enabled("device-2", "light")

    await loaded._store.async_save({"settings": {"device-1": {
        "light": {"kind": "level", "value": False},
        "cFan": {"kind": "level", "value": 200},
        "dFan": {"kind": "level", "value": "90"},
        "untrusted": {"kind": "preset", "value": "natural_wind"},
    }, "device-2": [50]}})
    corrupted = ManualPreferences(hass, "round-trip", True)
    await corrupted.async_load()
    assert corrupted.selection("device-1", "light") is None
    assert corrupted.selection("device-1", "cFan") is None
    assert corrupted.selection("device-1", "dFan") is None


async def test_store_migrates_version_one_numeric_preferences_to_typed_version_two(hass: HomeAssistant) -> None:
    key = f"{DOMAIN}_manual_settings_upgrade"
    legacy = Store[dict[str, object]](hass, 1, key)
    await legacy.async_save({"levels": {
        "device-1": {"light": 65, "cFan": 70, "dFan": 30},
        "device-2": {"light": 20, "cFan": 200, "dFan": True},
    }})
    upgraded = ManualPreferences(hass, "upgrade", True)
    await upgraded.async_load()
    assert [upgraded.selection("device-1", channel) for channel in ("light", "cFan", "dFan")] == [65, 70, 30]
    assert upgraded.selection("device-2", "light") is None
    assert upgraded.selection("device-2", "cFan") is None
    assert upgraded.selection("device-2", "dFan") is None

    raw = await Store[dict[str, object]](hass, 2, key).async_load()
    assert raw == {"settings": {"device-1": {
        "light": {"kind": "level", "value": 65},
        "cFan": {"kind": "level", "value": 70},
        "dFan": {"kind": "level", "value": 30},
    }}}
    assert upgraded._store.version == 2


async def test_store_persists_only_valid_typed_natural_wind_for_cfan(hass: HomeAssistant) -> None:
    prefs = ManualPreferences(hass, "wind-preference", True)
    await prefs.async_load()
    await prefs.async_remember("device-1", "cFan", "natural_wind")
    await prefs.async_remember("device-1", "dFan", "natural_wind")
    await prefs.async_remember("device-1", "light", True)
    await prefs.async_remember("device-2", "cFan", 200)
    reloaded = ManualPreferences(hass, "wind-preference", True)
    await reloaded.async_load()
    assert reloaded.selection("device-1", "cFan") == "natural_wind"
    assert reloaded.selection("device-1", "dFan") is None
    assert reloaded.selection("device-1", "light") is None
    assert reloaded.selection("device-2", "cFan") is None
    await reloaded.async_remember("device-1", "cFan", 60)
    assert reloaded.selection("device-1", "cFan") == 60
    await reloaded.async_remember("device-1", "cFan", "natural_wind")
    assert reloaded.selection("device-1", "cFan") == "natural_wind"


@pytest.mark.parametrize("channel", ("light", "cFan", "dFan"))
@pytest.mark.parametrize("facts", ("missing", "auto", "plan", "null", "stale", "manual"))
async def test_bare_on_uses_baseline_payload_when_memory_guard_fails(
    hass: HomeAssistant, channel: str, facts: str,
) -> None:
    """No remembered setting can turn a normally available ON into a validation error."""
    enabled, sent, saved = await _setup(hass, entry_id=f"enabled-{channel}-{facts}")
    disabled, reference, _ = await _setup(hass, entry_id=f"disabled-{channel}-{facts}", channels=())
    selection: int | str = "natural_wind" if channel == "cFan" else 70
    await saved.async_remember("device-1", channel, selection)
    for coordinator in (enabled, disabled):
        if facts == "missing":
            await _ingress(coordinator, 1, "get/accepted", {
                "version": 10, "state": {"reported": {channel: {"lv": 0}}},
            })
        elif facts == "stale":
            await _report(coordinator, 1, channel, 0, version=10)
            await _report(coordinator, 1, channel, 0, mode=1, version=11)
            await _report(coordinator, 1, channel, 0, version=9)
        else:
            await _report(
                coordinator, 1, channel, 0,
                mode=1 if facts == "auto" else 0,
                in_plan=1 if facts == "plan" else None if facts == "null" else 0,
                version=10,
            )
    cls = {"light": VivosunLightEntity, "cFan": VivosunCirculationFanEntity, "dFan": VivosunDuctFanEntity}[channel]
    try:
        await cls(enabled, "device-1").async_turn_on()
        await cls(disabled, "device-1").async_turn_on()
        desired = sent.published[-1][1]["state"]["desired"]
        baseline = reference.published[-1][1]["state"]["desired"]
        if facts == "manual":
            expected = 200 if channel == "cFan" else 70 if channel == "light" else DFAN_LEVEL_MAP[7]
            assert desired[channel]["manu"]["lv"] == expected  # type: ignore[index]
        else:
            assert desired == baseline
            expected = 25 if channel == "light" else (
                CFAN_LEVEL_MAP[1] if channel == "cFan" else DFAN_LEVEL_MAP[1]
            )
            assert desired[channel]["manu"]["lv"] == expected  # type: ignore[index]
            await _ack(enabled, 1, sent.published[-1][1], 20)
            assert saved.selection("device-1", channel) == selection
    finally:
        await enabled._disconnect_mqtt_client()
        await disabled._disconnect_mqtt_client()


@pytest.mark.parametrize("channel,raw", [("light", 65), ("cFan", 70), ("dFan", 60)])
async def test_positive_report_keeps_memory_disabled_behavior_even_during_plan(
    hass: HomeAssistant, channel: str, raw: int,
) -> None:
    active, mqtt, prefs = await _setup(hass)
    await prefs.async_remember("device-1", channel, 90)
    await _report(active, 1, channel, raw, mode=1, in_plan=1)
    entity = {
        "light": VivosunLightEntity, "cFan": VivosunCirculationFanEntity, "dFan": VivosunDuctFanEntity,
    }[channel](active, "device-1")
    try:
        await entity.async_turn_on()
        if channel == "light":
            assert not mqtt.published  # Already-on light never issues a new default command.
        else:
            assert mqtt.published[-1][1]["state"]["desired"][channel]["manu"]["lv"] == raw  # type: ignore[index]
        assert prefs.selection("device-1", channel) == 90
    finally:
        await active._disconnect_mqtt_client()


async def test_natural_wind_accepted_only_for_matching_token_value_and_newest_generation(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    fan = VivosunCirculationFanEntity(coordinator, "device-1")
    try:
        await fan.async_set_percentage(50)
        numeric = mqtt.published[-1][1]
        await _ack(coordinator, 1, numeric, 2)
        assert prefs.selection("device-1", "cFan") == 50

        await fan.async_set_preset_mode("natural_wind")
        wind = mqtt.published[-1][1]
        await _ack(coordinator, 2, wind, 3)  # Wrong device.
        await _ingress(coordinator, 1, "update/accepted", {
            "clientToken": wind["clientToken"], "version": 3,
            "state": {"desired": {"cFan": {"mode": 0, "manu": {"lv": 70}}}},
        })
        assert prefs.selection("device-1", "cFan") == 50
        await _ack(coordinator, 1, wind, 4)
        assert prefs.selection("device-1", "cFan") == "natural_wind"

        await fan.async_set_percentage(90)
        earlier_numeric = mqtt.published[-1][1]
        await fan.async_set_preset_mode("natural_wind")
        newer_wind = mqtt.published[-1][1]
        await _ack(coordinator, 1, newer_wind, 6)
        await _ack(coordinator, 1, earlier_numeric, 5)
        assert prefs.selection("device-1", "cFan") == "natural_wind"
    finally:
        await coordinator._disconnect_mqtt_client()


async def test_rejected_failed_expired_and_disconnected_natural_wind_do_not_train(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    fan = VivosunCirculationFanEntity(coordinator, "device-1")
    try:
        await prefs.async_remember("device-1", "cFan", 40)
        await fan.async_set_preset_mode("natural_wind")
        rejected = mqtt.published[-1][1]
        await _ingress(coordinator, 1, "update/rejected", {
            "clientToken": rejected["clientToken"], "code": 409,
        })
        await _ack(coordinator, 1, rejected, 2)
        assert prefs.selection("device-1", "cFan") == 40

        mqtt.failure = ConnectionError("offline")
        with pytest.raises(ConnectionError):
            await fan.async_set_preset_mode("natural_wind")
        assert prefs.selection("device-1", "cFan") == 40
        mqtt.failure = None

        await fan.async_set_preset_mode("natural_wind")
        expired = mqtt.published[-1][1]
        coordinator._discard_manual_choice(cast("str", expired["clientToken"]), "device-1")
        await _ack(coordinator, 1, expired, 3)
        assert prefs.selection("device-1", "cFan") == 40

        await fan.async_set_preset_mode("natural_wind")
        pending = mqtt.published[-1][1]
        await coordinator._disconnect_mqtt_client()
        await _ack(coordinator, 1, pending, 4)
        assert prefs.selection("device-1", "cFan") == 40
    finally:
        await coordinator._disconnect_mqtt_client()


async def test_natural_wind_loses_to_explicit_simultaneous_percentage_without_training(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    try:
        fan = VivosunCirculationFanEntity(coordinator, "device-1")
        await fan.async_turn_on(preset_mode="natural_wind", percentage=60)
        assert len(mqtt.published) == 1
        selected = mqtt.published[0][1]
        assert selected["state"]["desired"]["cFan"]["manu"]["lv"] == CFAN_LEVEL_MAP[6]  # type: ignore[index]
        await _ingress(coordinator, 1, "update/accepted", {
            "clientToken": "no-such-preset-token", "version": 2,
            "state": {"desired": {"cFan": {"mode": 0, "manu": {"lv": 200}}}},
        })
        assert prefs.selection("device-1", "cFan") is None
        await _ack(coordinator, 1, selected, 3)
        assert prefs.selection("device-1", "cFan") == 60
    finally:
        await coordinator._disconnect_mqtt_client()


@pytest.mark.parametrize(
    ("channel", "preset", "expected"),
    [
        ("cFan", "natural_wind", CFAN_LEVEL_MAP[6]),
        ("dFan", "auto", DFAN_LEVEL_MAP[6]),
        ("dFan", "manual", DFAN_LEVEL_MAP[6]),
    ],
)
async def test_real_ha_fan_turn_on_mixed_arguments_send_only_winning_speed(
    hass: HomeAssistant, channel: str, preset: str, expected: int,
) -> None:
    coordinator, mqtt, preferences = await _setup(hass)
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan", platform_name="vivosun_growhub",
        platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    fan = (VivosunCirculationFanEntity if channel == "cFan" else VivosunDuctFanEntity)(
        coordinator, "device-1",
    )
    await platform.async_add_entities([fan])
    await hass.async_block_till_done()
    assert fan.entity_id is not None
    try:
        await hass.services.async_call("fan", "turn_on", {
            "entity_id": fan.entity_id, "preset_mode": preset, "percentage": 55,
        }, blocking=True)
        assert len(mqtt.published) == 1
        command = mqtt.published[0][1]
        assert command["state"]["desired"][channel] == {  # type: ignore[index]
            "mode": 0, "manu": {"lv": expected},
        }
        assert list(coordinator._pending_manual_choices) == [command["clientToken"]]
        await _ingress(coordinator, 1, "update/accepted", {
            "version": 2, "clientToken": "nonexistent-losing-preset",
            "state": {"desired": {channel: {"mode": 0, "manu": {"lv": 200}}}},
        })
        assert preferences.selection("device-1", channel) is None
        await _ack(coordinator, 1, command, 3)
        assert preferences.selection("device-1", channel) == 60
    finally:
        await coordinator._disconnect_mqtt_client()
        await platform.async_reset()


@pytest.mark.parametrize("channel,preset", [("cFan", "natural_wind"), ("dFan", "auto")])
async def test_mixed_explicit_zero_uses_safe_on_default_without_preset_or_memory_training(
    hass: HomeAssistant, channel: str, preset: str,
) -> None:
    coordinator, mqtt, preferences = await _setup(hass)
    fan = (VivosunCirculationFanEntity if channel == "cFan" else VivosunDuctFanEntity)(
        coordinator, "device-1",
    )
    try:
        await preferences.async_remember("device-1", channel, 70)
        await fan.async_turn_on(preset_mode=preset, percentage=0)
        assert len(mqtt.published) == 1
        command = mqtt.published[0][1]
        assert command["state"]["desired"][channel] == {  # type: ignore[index]
            "mode": 0, "manu": {"lv": CFAN_LEVEL_MAP[1] if channel == "cFan" else DFAN_LEVEL_MAP[1]},
        }
        await _ack(coordinator, 1, command, 2)
        assert preferences.selection("device-1", channel) == 70
    finally:
        await coordinator._disconnect_mqtt_client()


@pytest.mark.parametrize("channel,preset", [("cFan", "auto"), ("dFan", "natural_wind")])
async def test_native_ha_fan_mixed_invalid_preset_does_not_publish(
    hass: HomeAssistant, channel: str, preset: str,
) -> None:
    coordinator, mqtt, preferences = await _setup(hass)
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan", platform_name="vivosun_growhub",
        platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    fan = (VivosunCirculationFanEntity if channel == "cFan" else VivosunDuctFanEntity)(
        coordinator, "device-1",
    )
    await platform.async_add_entities([fan])
    await hass.async_block_till_done()
    assert fan.entity_id is not None
    try:
        with pytest.raises((ValueError, ServiceValidationError)):
            await hass.services.async_call("fan", "turn_on", {
                "entity_id": fan.entity_id, "preset_mode": preset, "percentage": 60,
            }, blocking=True)
        assert mqtt.published == []
        assert preferences.selection("device-1", channel) is None
    finally:
        await coordinator._disconnect_mqtt_client()
        await platform.async_reset()


async def test_restarted_natural_wind_persists_without_startup_replay(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass, entry_id="wind-restart")
    await prefs.async_remember("device-1", "cFan", "natural_wind")
    await coordinator._disconnect_mqtt_client()
    fresh, later_mqtt, restored = await _setup(hass, entry_id="wind-restart")
    try:
        assert later_mqtt.published == []
        assert restored.selection("device-1", "cFan") == "natural_wind"
        await _report(fresh, 1, "cFan", 0)
        await VivosunCirculationFanEntity(fresh, "device-1").async_turn_on()
        assert later_mqtt.published[-1][1]["state"]["desired"]["cFan"]["manu"]["lv"] == 200  # type: ignore[index]
        assert mqtt.published == []
    finally:
        await fresh._disconnect_mqtt_client()


async def test_native_ha_fan_service_saves_and_restores_natural_wind(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan", platform_name="vivosun_growhub",
        platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    fan = VivosunCirculationFanEntity(coordinator, "device-1")
    await platform.async_add_entities([fan])
    await hass.async_block_till_done()
    assert fan.entity_id is not None
    try:
        await _report(coordinator, 1, "cFan", 0)
        await hass.services.async_call(
            "fan", "set_preset_mode", {"entity_id": fan.entity_id, "preset_mode": "natural_wind"},
            blocking=True,
        )
        selected = mqtt.published[-1][1]
        assert selected["state"]["desired"]["cFan"]["manu"]["lv"] == 200  # type: ignore[index]
        assert prefs.selection("device-1", "cFan") is None
        await _ack(coordinator, 1, selected, 2)
        assert prefs.selection("device-1", "cFan") == "natural_wind"
        await _report(coordinator, 1, "cFan", 200, version=3)
        await hass.services.async_call("fan", "turn_off", {"entity_id": fan.entity_id}, blocking=True)
        await _report(coordinator, 1, "cFan", 0, version=4)
        before = len(mqtt.published)
        await hass.services.async_call("fan", "turn_on", {"entity_id": fan.entity_id}, blocking=True)
        assert len(mqtt.published) == before + 1
        assert mqtt.published[-1][1]["state"]["desired"]["cFan"]["manu"]["lv"] == 200  # type: ignore[index]
        assert prefs.selection("device-1", "cFan") == "natural_wind"
    finally:
        await coordinator._disconnect_mqtt_client()
        await platform.async_reset()


async def test_light_acceptance_off_restore_and_second_device_isolation(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    light = VivosunLightEntity(coordinator, "device-1")
    other = VivosunLightEntity(coordinator, "device-2")
    await _report(coordinator, 1, "light", 0)
    await _report(coordinator, 2, "light", 0)

    await light.async_turn_on(brightness=153)  # 60 percent
    selected = mqtt.published[-1][1]
    assert selected["state"]["desired"]["light"]["manu"]["lv"] == 60  # type: ignore[index]
    assert prefs.selection("device-1", "light") is None
    await _ack(coordinator, 1, selected, 2)
    assert prefs.selection("device-1", "light") == 60

    await light.async_turn_off()
    assert prefs.selection("device-1", "light") == 60
    await _report(coordinator, 1, "light", 0, version=3)
    await light.async_turn_on()
    assert mqtt.published[-1][1]["state"]["desired"]["light"]["manu"]["lv"] == 60  # type: ignore[index]

    await other.async_turn_on()
    assert mqtt.published[-1][1]["state"]["desired"]["light"]["manu"]["lv"] == 25  # type: ignore[index]
    assert prefs.selection("device-2", "light") is None
    await coordinator._disconnect_mqtt_client()


@pytest.mark.parametrize("channel,raw_map", [("cFan", CFAN_LEVEL_MAP), ("dFan", DFAN_LEVEL_MAP)])
async def test_fan_normalized_selection_and_safe_restore(
    hass: HomeAssistant, channel: str, raw_map: tuple[int, ...],
) -> None:
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", channel),))
    fan = (VivosunCirculationFanEntity if channel == "cFan" else VivosunDuctFanEntity)(coordinator, "device-1")
    await _report(coordinator, 1, channel, 0)
    await fan.async_set_percentage(55)
    sent = mqtt.published[-1][1]
    assert sent["state"]["desired"][channel]["manu"]["lv"] == raw_map[6]  # type: ignore[index]
    assert prefs.selection("device-1", channel) is None
    await _ack(coordinator, 1, sent, 2)
    assert prefs.selection("device-1", channel) == 60

    await fan.async_turn_off()
    await _report(coordinator, 1, channel, 0, version=3)
    await fan.async_turn_on()
    assert mqtt.published[-1][1]["state"]["desired"][channel]["manu"]["lv"] == raw_map[6]  # type: ignore[index]
    assert prefs.selection("device-1", channel) == 60
    await coordinator._disconnect_mqtt_client()


async def test_rapid_acceptance_old_ack_and_rejected_new_request(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    try:
        light = VivosunLightEntity(coordinator, "device-1")
        await light.async_turn_on(brightness=102)
        earlier = mqtt.published[-1][1]
        await light.async_turn_on(brightness=179)
        later = mqtt.published[-1][1]
        await _ack(coordinator, 1, later, 3)
        await _ack(coordinator, 1, earlier, 2)
        assert prefs.selection("device-1", "light") == 70
        await light.async_turn_on(brightness=230)
        newest = mqtt.published[-1][1]
        await _ingress(coordinator, 1, "update/rejected", {"clientToken": newest["clientToken"], "code": 409})
        assert prefs.selection("device-1", "light") == 70
    finally:
        await coordinator._disconnect_mqtt_client()


async def test_no_ack_or_publish_failure_does_not_replace_last_accepted(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    light = VivosunLightEntity(coordinator, "device-1")
    await light.async_turn_on(brightness=128)
    await _ack(coordinator, 1, mqtt.published[-1][1], 2)
    await light.async_turn_on(brightness=204)
    assert prefs.selection("device-1", "light") == 50
    mqtt.failure = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        await light.async_turn_on(brightness=255)
    assert prefs.selection("device-1", "light") == 50
    await coordinator._disconnect_mqtt_client()
    assert prefs.selection("device-1", "light") == 50


async def test_external_report_and_auto_mode_never_train_or_replay(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", "dFan"),))
    fan = VivosunDuctFanEntity(coordinator, "device-1")
    await _report(coordinator, 1, "dFan", 0)
    await fan.async_set_percentage(70)
    await _ack(coordinator, 1, mqtt.published[-1][1], 2)
    assert prefs.selection("device-1", "dFan") == 70
    # A GET without per-field metadata cannot certify a late state change while
    # the last explicit HA request remains pending. Use a reported push here.
    await _ingress(coordinator, 1, "update/accepted", {
        "version": 3, "state": {"reported": {"dFan": {"lv": 0, "mode": 2, "inPlan": 1}}},
    })
    before = len(mqtt.published)
    await fan.async_turn_on()
    assert len(mqtt.published) == before + 1
    assert mqtt.published[-1][1]["state"]["desired"]["dFan"]["manu"]["lv"] == DFAN_LEVEL_MAP[1]  # type: ignore[index]
    assert prefs.selection("device-1", "dFan") == 70
    await coordinator._disconnect_mqtt_client()


async def test_missing_manual_evidence_prevents_implicit_restore(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    await prefs.async_remember("device-1", "light", 70)
    light = VivosunLightEntity(coordinator, "device-1")
    await light.async_turn_on()
    assert mqtt.published[-1][1]["state"]["desired"]["light"]["manu"]["lv"] == 25  # type: ignore[index]
    assert prefs.selection("device-1", "light") == 70
    await coordinator._disconnect_mqtt_client()


async def test_restart_loads_preference_without_publish_and_disabled_stays_inert(hass: HomeAssistant) -> None:
    _coordinator, mqtt, prefs = await _setup(hass)
    await prefs.async_remember("device-1", "light", 75)
    fresh, fresh_mqtt, restored = await _setup(hass, channels=())
    assert restored.selection("device-1", "light") == 75
    assert fresh_mqtt.published == []
    light = VivosunLightEntity(fresh, "device-1")
    await _report(fresh, 1, "light", 0)
    await light.async_turn_on()
    assert fresh_mqtt.published[-1][1]["state"]["desired"]["light"]["manu"]["lv"] == 25  # type: ignore[index]
    assert mqtt.published == []
    await fresh._disconnect_mqtt_client()


async def test_wrong_device_token_or_wrong_accepted_level_does_not_commit(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    light = VivosunLightEntity(coordinator, "device-1")
    await light.async_turn_on(brightness=153)
    requested = mqtt.published[-1][1]
    await _ack(coordinator, 2, requested, 2)
    assert prefs.selection("device-1", "light") is None
    await _ingress(coordinator, 1, "update/accepted", {
        "clientToken": requested["clientToken"], "version": 2,
        "state": {"desired": {"light": {"manu": {"lv": 70}}}},
    })
    assert prefs.selection("device-1", "light") is None
    await _ack(coordinator, 1, requested, 3)
    assert prefs.selection("device-1", "light") == 60
    await coordinator._disconnect_mqtt_client()


async def test_expired_manual_ack_is_ignored_after_previous_acceptance(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", "cFan"),))
    fan = VivosunCirculationFanEntity(coordinator, "device-1")
    await fan.async_set_percentage(40)
    await _ack(coordinator, 1, mqtt.published[-1][1], 2)
    await fan.async_set_percentage(80)
    abandoned = mqtt.published[-1][1]
    coordinator._discard_manual_choice(cast("str", abandoned["clientToken"]), "device-1")
    await _ack(coordinator, 1, abandoned, 3)
    assert prefs.selection("device-1", "cFan") == 40
    await coordinator._disconnect_mqtt_client()


async def test_explicit_natural_wind_replaces_speed_and_restores_once_after_off(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", "cFan"),))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        OPTION_CFAN_OSCILLATION_CONTROLLERS: ["device-1"],
    })
    fan = VivosunCirculationFanEntity(coordinator, "device-1", entry=entry)
    await _report(coordinator, 1, "cFan", 0)
    await fan.async_turn_on(percentage=80)
    selected = mqtt.published[-1][1]
    await _ack(coordinator, 1, selected, 2)
    assert prefs.selection("device-1", "cFan") == 80
    await fan.async_set_preset_mode("natural_wind")
    wind = mqtt.published[-1][1]
    assert wind["state"]["desired"]["cFan"]["manu"]["lv"] == 200  # type: ignore[index]
    assert prefs.selection("device-1", "cFan") == 80
    await _ack(coordinator, 1, wind, 3)
    assert prefs.selection("device-1", "cFan") == "natural_wind"
    await _report(coordinator, 1, "cFan", 200, version=3)
    assert fan.is_on and fan.percentage is None
    before = len(mqtt.published)
    await fan.async_turn_on()
    assert len(mqtt.published) == before  # Bare ON preserves active Natural Wind.
    assert prefs.selection("device-1", "cFan") == "natural_wind"
    before = len(mqtt.published)
    await fan.async_turn_on(preset_mode="natural_wind")
    assert len(mqtt.published) == before + 1
    assert prefs.selection("device-1", "cFan") == "natural_wind"
    await fan.async_oscillate(True)
    await fan.async_set_percentage(0)
    await _report(coordinator, 1, "cFan", 0, version=4)
    assert prefs.selection("device-1", "cFan") == "natural_wind"
    before = len(mqtt.published)
    await fan.async_turn_on()
    assert len(mqtt.published) == before + 1
    assert mqtt.published[-1][1]["state"]["desired"]["cFan"]["manu"]["lv"] == 200  # type: ignore[index]
    assert prefs.selection("device-1", "cFan") == "natural_wind"
    await fan.async_set_percentage(50)
    await _ack(coordinator, 1, mqtt.published[-1][1], 5)
    assert prefs.selection("device-1", "cFan") == 50
    await coordinator._disconnect_mqtt_client()


async def test_reported_manual_guard_needs_both_fields_and_fresh_component_evidence(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    await prefs.async_remember("device-1", "light", 80)
    await _ingress(coordinator, 1, "get/accepted", {
        "version": 5, "state": {"reported": {"light": {"mode": 0, "lv": 0}}},
    })
    assert not coordinator.manual_restore_allowed("device-1", "light")
    await _ingress(coordinator, 1, "update/accepted", {
        "version": 6, "state": {"desired": {"light": {"mode": 0, "inPlan": 0}}},
    })
    assert not coordinator.manual_restore_allowed("device-1", "light")
    await _report(coordinator, 1, "light", 0, version=7)
    assert coordinator.manual_restore_allowed("device-1", "light")
    await _report(coordinator, 1, "light", 0, mode=1, in_plan=1, version=9)
    assert not coordinator.manual_restore_allowed("device-1", "light")
    await _report(coordinator, 1, "light", 0, version=8)
    assert not coordinator.manual_restore_allowed("device-1", "light")
    await coordinator._disconnect_mqtt_client()
    assert not coordinator.manual_restore_allowed("device-1", "light")
    assert mqtt.published == []


async def test_fan_auto_rejection_and_incomplete_plan_reports_never_replay(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", "dFan"),))
    fan = VivosunDuctFanEntity(coordinator, "device-1")
    await _ingress(coordinator, 1, "get/accepted", {
        "version": 1, "state": {"reported": {"dFan": {"mode": 0, "lv": 0}}},
    })
    await fan.async_set_percentage(60)
    selected = mqtt.published[-1][1]
    await _ingress(coordinator, 1, "update/rejected", {
        "clientToken": selected["clientToken"], "code": 409,
    })
    assert prefs.selection("device-1", "dFan") is None
    await prefs.async_remember("device-1", "dFan", 40)
    before = len(mqtt.published)
    await fan.async_turn_on()
    assert len(mqtt.published) == before + 1
    assert mqtt.published[-1][1]["state"]["desired"]["dFan"]["manu"]["lv"] == DFAN_LEVEL_MAP[1]  # type: ignore[index]
    await _ingress(coordinator, 1, "get/accepted", {
        "version": 2, "state": {"reported": {"dFan": {"mode": 2, "inPlan": 0, "lv": 0}}},
    })
    await fan.async_turn_on()
    assert len(mqtt.published) == before + 2
    assert mqtt.published[-1][1]["state"]["desired"]["dFan"]["manu"]["lv"] == DFAN_LEVEL_MAP[1]  # type: ignore[index]
    await coordinator._disconnect_mqtt_client()


async def test_fan_accepted_newer_selection_wins_late_older_ack(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", "dFan"),))
    fan = VivosunDuctFanEntity(coordinator, "device-1")
    await fan.async_set_percentage(40)
    old = mqtt.published[-1][1]
    await fan.async_set_percentage(90)
    new = mqtt.published[-1][1]
    await _ack(coordinator, 1, new, 3)
    await _ack(coordinator, 1, old, 2)
    assert prefs.selection("device-1", "dFan") == 90
    await coordinator._disconnect_mqtt_client()


@pytest.mark.parametrize(
    "channel,percentage", [("cFan", 50), ("dFan", 60)],
)
async def test_already_reported_fan_selection_still_trains_memory_on_ack(
    hass: HomeAssistant, channel: str, percentage: int,
) -> None:
    """A confirmed raw level has no pending output, but explicit HA intent still gets a receipt."""
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", channel),))
    fan = (VivosunCirculationFanEntity if channel == "cFan" else VivosunDuctFanEntity)(
        coordinator, "device-1",
    )
    try:
        await _ingress(coordinator, 1, "get/accepted", {
            "version": 100, "state": {"reported": {channel: {
                "mode": 0, "inPlan": 0, "lv": 70,
            }}},
            "metadata": {"reported": {channel: {"lv": {"timestamp": 1000}}}},
        })
        await fan.async_set_percentage(percentage)
        command = mqtt.published[-1][1]
        assert command["state"]["desired"][channel]["manu"]["lv"] == 70  # type: ignore[index]
        assert coordinator._pending_fields == {}
        assert len(coordinator._already_confirmed_tokens) == 1
        assert prefs.selection("device-1", channel) is None

        await _ack(coordinator, 1, command, 101)
        assert coordinator._already_confirmed_tokens == {}
        assert prefs.selection("device-1", channel) == percentage
        assert len(mqtt.published) == 1
    finally:
        await coordinator._disconnect_mqtt_client()


async def test_real_ha_light_service_sets_explicit_level_and_records_cloud_ack(hass: HomeAssistant) -> None:
    coordinator, mqtt, prefs = await _setup(hass)
    assert await async_setup_component(hass, "light", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="light", platform_name="vivosun_growhub",
        platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    light = VivosunLightEntity(coordinator, "device-1")
    await platform.async_add_entities([light])
    await hass.async_block_till_done()
    assert light.entity_id is not None
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": light.entity_id, "brightness": 153}, blocking=True,
    )
    assert prefs.selection("device-1", "light") is None
    assert mqtt.published[-1][1]["state"]["desired"]["light"]["manu"]["lv"] == 60  # type: ignore[index]
    await _ack(coordinator, 1, mqtt.published[-1][1], 2)
    assert prefs.selection("device-1", "light") == 60
    await coordinator._disconnect_mqtt_client()
    await platform.async_reset()


async def test_ha_fan_oscillate_service_respects_options_and_device_route(hass: HomeAssistant) -> None:
    coordinator, mqtt, _prefs = await _setup(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        OPTION_CFAN_OSCILLATION_CONTROLLERS: ["device-2"],
    })
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan", platform_name="vivosun_growhub",
        platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    disabled = VivosunCirculationFanEntity(coordinator, "device-1", entry=entry)
    enabled = VivosunCirculationFanEntity(coordinator, "device-2", entry=entry)
    await platform.async_add_entities([disabled, enabled])
    await hass.async_block_till_done()
    assert not disabled.supported_features & FanEntityFeature.OSCILLATE
    assert enabled.supported_features & FanEntityFeature.OSCILLATE
    assert disabled.entity_id is not None and enabled.entity_id is not None

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "fan", "oscillate", {"entity_id": disabled.entity_id, "oscillating": True}, blocking=True,
        )
    assert mqtt.published == []

    await hass.services.async_call(
        "fan", "oscillate", {"entity_id": enabled.entity_id, "oscillating": True}, blocking=True,
    )
    assert len(mqtt.published) == 1
    assert mqtt.published[0][0] == "$aws/things/thing-2/shadow/update"
    assert mqtt.published[0][1]["state"]["desired"]["cFan"] == {"osc": 1}  # type: ignore[index]

    hass.config_entries.async_update_entry(entry, options={})
    with pytest.raises(ServiceValidationError, match="Oscillation control is disabled"):
        await enabled.async_oscillate(False)
    assert len(mqtt.published) == 1
    await coordinator._disconnect_mqtt_client()
    await platform.async_reset()


async def test_native_ha_cfan_service_exits_natural_wind_only_with_explicit_speed_or_off(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", "cFan"),))
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan", platform_name="vivosun_growhub",
        platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    fan = VivosunCirculationFanEntity(coordinator, "device-1")
    await platform.async_add_entities([fan])
    await hass.async_block_till_done()
    assert fan.entity_id is not None
    assert fan.preset_modes == ["natural_wind"]

    await _ingress(coordinator, 1, "get/accepted", {
        "version": 1, "state": {"reported": {"cFan": {
            "mode": 0, "inPlan": 0, "lv": 70, "manu": {"lv": 70}, "nw": 1,
        }}},
    })
    assert fan.preset_mode is None and fan.percentage == 50
    assert coordinator.data["shadows"]["device-1"]["reported_supported"]["cFan"]["nw"] == 1

    for removed_preset in ("normal", "night"):
        before = len(mqtt.published)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "fan", "set_preset_mode", {"entity_id": fan.entity_id, "preset_mode": removed_preset},
                blocking=True,
            )
        assert len(mqtt.published) == before

    await hass.services.async_call(
        "fan", "set_preset_mode", {"entity_id": fan.entity_id, "preset_mode": "natural_wind"},
        blocking=True,
    )
    assert len(mqtt.published) == 1
    assert mqtt.published[-1][1]["state"]["desired"]["cFan"] == {"mode": 0, "manu": {"lv": 200}}  # type: ignore[index]
    await _ingress(coordinator, 1, "update/accepted", {
        "version": 2, "state": {"reported": {"cFan": {"lv": 200, "manu": {"lv": 200}, "nw": 1}}},
    })
    assert fan.preset_mode == "natural_wind" and fan.percentage is None and fan.is_on
    await hass.services.async_call("fan", "turn_on", {"entity_id": fan.entity_id}, blocking=True)
    assert len(mqtt.published) == 1

    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": fan.entity_id, "percentage": 50}, blocking=True,
    )
    numeric = mqtt.published[-1][1]
    assert numeric["state"]["desired"]["cFan"] == {"mode": 0, "manu": {"lv": 70}}  # type: ignore[index]
    await _ack(coordinator, 1, numeric, 3)
    await _ingress(coordinator, 1, "update/accepted", {
        "version": 4, "state": {"reported": {"cFan": {"lv": 70, "manu": {"lv": 70}}}},
    })
    assert fan.preset_mode is None and fan.percentage == 50
    assert prefs.selection("device-1", "cFan") == 50

    await hass.services.async_call("fan", "turn_off", {"entity_id": fan.entity_id}, blocking=True)
    assert mqtt.published[-1][1]["state"]["desired"]["cFan"] == {"mode": 0, "manu": {"lv": 0}}  # type: ignore[index]
    await _report(coordinator, 1, "cFan", 0, version=5)
    assert fan.preset_mode is None and fan.percentage == 0
    assert prefs.selection("device-1", "cFan") == 50
    assert all("nw" not in item[1]["state"]["desired"]["cFan"] for item in mqtt.published)  # type: ignore[index]
    await coordinator._disconnect_mqtt_client()
    await platform.async_reset()


@pytest.mark.parametrize(
    ("channel", "fan_type", "raw_map", "auto_mode"),
    [
        ("cFan", VivosunCirculationFanEntity, CFAN_LEVEL_MAP, 1),
        ("dFan", VivosunDuctFanEntity, DFAN_LEVEL_MAP, 2),
    ],
)
async def test_capture_shaped_manual_fan_service_selection_off_and_restore(
    hass: HomeAssistant, channel: str, fan_type: type[VivosunCirculationFanEntity] | type[VivosunDuctFanEntity],
    raw_map: tuple[int, ...], auto_mode: int,
) -> None:
    """E42A/E25 reported mode+inPlan are independent facts, not a global plan label."""
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", channel),))
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan", platform_name="vivosun_growhub",
        platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    fan = fan_type(coordinator, "device-1")
    await platform.async_add_entities([fan])
    await hass.async_block_till_done()
    assert fan.entity_id is not None

    # Sanitized from genuine E42A and E25 shadow/read accepted fragments.
    # Real archives show mode=0 and inPlan=0 under BOTH fan components.
    await _ingress(coordinator, 1, "get/accepted", {
        "version": 10, "timestamp": 100,
        "state": {"reported": {channel: {"mode": 0, "inPlan": 0, "lv": 0, "manu": {"lv": 0}}}},
        "metadata": {"reported": {channel: {
            "mode": {"timestamp": 100}, "inPlan": {"timestamp": 100}, "lv": {"timestamp": 100},
        }}},
    })
    assert coordinator.manual_restore_allowed("device-1", channel)
    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": fan.entity_id, "percentage": 55}, blocking=True,
    )
    selected = mqtt.published[-1][1]
    assert selected["state"]["desired"][channel]["manu"]["lv"] == raw_map[6]  # type: ignore[index]
    assert prefs.selection("device-1", channel) is None
    await _ack(coordinator, 1, selected, 11)
    assert prefs.selection("device-1", channel) == 60

    await hass.services.async_call("fan", "turn_off", {"entity_id": fan.entity_id}, blocking=True)
    assert mqtt.published[-1][1]["state"]["desired"][channel]["manu"]["lv"] == 0  # type: ignore[index]
    assert prefs.selection("device-1", channel) == 60
    # Sparse OFF documents omit mode/inPlan, preserving previously reported facts.
    await _ingress(coordinator, 1, "update/documents", {
        "previous": {"version": 11, "state": {"reported": {channel: {"lv": raw_map[6]}}}},
        "current": {"version": 12, "state": {"reported": {channel: {"lv": 0}}}},
    })
    assert coordinator.manual_restore_allowed("device-1", channel)
    await hass.services.async_call("fan", "turn_on", {"entity_id": fan.entity_id}, blocking=True)
    restored = mqtt.published[-1][1]
    assert restored["state"]["desired"][channel]["manu"]["lv"] == raw_map[6]  # type: ignore[index]
    assert isinstance(restored.get("clientToken"), str)  # Correlation does not train preference.
    assert prefs.selection("device-1", channel) == 60

    # The E25 archive also contains an E42A dFan mode=2, inPlan=0.
    # Any nonmanual mode, even with no plan, revokes restoration permission.
    await _ingress(coordinator, 1, "update/accepted", {
        "version": 13,
        "state": {"reported": {channel: {"mode": auto_mode, "inPlan": 0, "lv": 0}}},
    })
    assert not coordinator.manual_restore_allowed("device-1", channel)
    command_count = len(mqtt.published)
    await hass.services.async_call("fan", "turn_on", {"entity_id": fan.entity_id}, blocking=True)
    assert len(mqtt.published) == command_count + 1
    assert mqtt.published[-1][1]["state"]["desired"][channel]["manu"]["lv"] == raw_map[1]  # type: ignore[index]
    await coordinator._disconnect_mqtt_client()
    await platform.async_reset()


@pytest.mark.parametrize("channel", ("cFan", "dFan"))
async def test_desired_only_fan_manual_flags_do_not_grant_restore(
    hass: HomeAssistant, channel: str,
) -> None:
    coordinator, mqtt, prefs = await _setup(hass, channels=(("device-1", channel),))
    await prefs.async_remember("device-1", channel, 50)
    await _ingress(coordinator, 1, "get/accepted", {
        "version": 10, "state": {"desired": {channel: {"mode": 0, "inPlan": 0, "manu": {"lv": 0}}}},
    })
    assert not coordinator.manual_restore_allowed("device-1", channel)
    fan = (VivosunCirculationFanEntity if channel == "cFan" else VivosunDuctFanEntity)(
        coordinator, "device-1",
    )
    await fan.async_turn_on()
    assert mqtt.published[-1][1]["state"]["desired"][channel]["manu"]["lv"] == (  # type: ignore[index]
        CFAN_LEVEL_MAP[1] if channel == "cFan" else DFAN_LEVEL_MAP[1]
    )
    await coordinator._disconnect_mqtt_client()


async def test_spectrum_and_fan_manual_facts_share_per_leaf_freshness_without_crossing_state(
    hass: HomeAssistant,
) -> None:
    """A later-version stale GET must not overwrite independently newer field timestamps."""
    coordinator, mqtt, _prefs = await _setup(hass, channels=(("device-1", "dFan"),))
    spectrum = VivosunSpectrumSelect(coordinator, "device-1")

    async def report(
        suffix: str, version: int, timestamp: int, light: dict[str, object], duct: dict[str, object],
    ) -> None:
        reported = {"light": light, "dFan": duct}
        metadata = {channel: {
            field: {"timestamp": timestamp} for field in component
        } for channel, component in reported.items()}
        document = {
            "version": version, "state": {"reported": reported},
            "metadata": {"reported": metadata},
        }
        await _ingress(
            coordinator, 1, suffix,
            {"current": document} if suffix == "update/documents" else document,
        )

    await report("get/accepted", 10, 100,
                 {"mode": 0, "inPlan": 0, "spec": 50}, {"mode": 0, "inPlan": 0, "lv": 0})
    assert spectrum.current_option == "Vegetative"
    assert coordinator.manual_restore_allowed("device-1", "light")
    assert coordinator.manual_restore_allowed("device-1", "dFan")

    await report("update/accepted", 12, 200,
                 {"mode": 1, "inPlan": 1, "spec": 100}, {"mode": 2, "inPlan": 0})
    await report("get/accepted", 13, 190,
                 {"mode": 0, "inPlan": 0, "spec": 20}, {"mode": 0, "inPlan": 0})
    assert spectrum.current_option == "Bloom"
    assert not coordinator.manual_restore_allowed("device-1", "light")
    assert not coordinator.manual_restore_allowed("device-1", "dFan")

    # Sparse, fresh reports can update distinct fields without clearing omitted facts.
    await report("update/documents", 14, 210, {"mode": 0, "spec": None}, {"mode": 0})
    assert spectrum.current_option is None
    assert not coordinator.manual_restore_allowed("device-1", "light")
    assert coordinator.manual_restore_allowed("device-1", "dFan")
    await report("update/accepted", 14, 210, {"inPlan": 0}, {"inPlan": None})
    assert coordinator.manual_restore_allowed("device-1", "light")
    assert not coordinator.manual_restore_allowed("device-1", "dFan")

    # A stale GET cannot revive an explicitly invalidated plan fact.
    await report("get/accepted", 15, 205, {"spec": 50}, {"inPlan": 0})
    assert spectrum.current_option is None
    assert not coordinator.manual_restore_allowed("device-1", "dFan")
    assert mqtt.published == []
    await coordinator._disconnect_mqtt_client()

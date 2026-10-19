"""Offline MQTT fan command feedback through real HA entities."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.setup import async_setup_component

from custom_components.vivosun_growhub.fan import VivosunCirculationFanEntity, VivosunDuctFanEntity
from custom_components.vivosun_growhub.models import DeviceInfo
from custom_components.vivosun_growhub.shadow import parse_shadow_document
from tests.test_light_pending import _receive, _setup

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@pytest.mark.parametrize(
    "channel,raw,percentage", [("cFan", 70, 50), ("dFan", 50, 40), ("cFan", 200, None), ("dFan", 0, 0)],
)
async def test_fan_desired_ack_and_old_documents_do_not_bounce(
    hass: HomeAssistant, channel: str, raw: int, percentage: int | None,
) -> None:
    coordinator, mqtt, _ = _setup(hass)
    entity = (VivosunCirculationFanEntity(coordinator, "light-0") if channel == "cFan"
              else VivosunDuctFanEntity(coordinator, "light-0"))
    baseline = 0 if raw else 50
    await _receive(coordinator, "thing-0", "get/accepted", {
        "state": {"reported": {channel: {"lv": baseline, "mode": 0, "inPlan": 0}}},
        "metadata": {"reported": {channel: {"lv": {"timestamp": 1000}}}}, "version": 100,
    })
    try:
        if raw == 200:
            await entity.async_set_preset_mode("natural_wind")
        else:
            assert percentage is not None
            await entity.async_set_percentage(percentage)
        assert entity.preset_mode == "natural_wind" if raw == 200 else entity.percentage == percentage
        token = mqtt.published[-1][1]["clientToken"]
        await _receive(coordinator, "thing-0", "update/accepted", {
            "clientToken": token, "version": 101,
            "state": {"desired": {channel: {"manu": {"lv": raw}}}},
            "metadata": {"desired": {channel: {"manu": {"lv": {"timestamp": 1010}}}}},
        })
        await _receive(coordinator, "thing-0", "update/documents", {
            "previous": {"version": 100, "state": {"reported": {channel: {"lv": baseline}}}},
            "current": {"version": 101, "state": {"reported": {channel: {"lv": baseline}}},
                        "metadata": {"reported": {channel: {"lv": {"timestamp": 1000}}}}},
        })
        assert entity.preset_mode == "natural_wind" if raw == 200 else entity.percentage == percentage
        await _receive(coordinator, "thing-0", "update/accepted", {
            "version": 102, "state": {"reported": {channel: {"lv": raw}}},
            "metadata": {"reported": {channel: {"lv": {"timestamp": 1011}}}},
        })
        assert entity.preset_mode == "natural_wind" if raw == 200 else entity.percentage == percentage
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("channel", ["cFan", "dFan"])
async def test_fan_supersession_rejection_and_timeout_restore_reported(
    hass: HomeAssistant, channel: str,
) -> None:
    coordinator, mqtt, _ = _setup(hass)
    entity = (VivosunCirculationFanEntity(coordinator, "light-0") if channel == "cFan"
              else VivosunDuctFanEntity(coordinator, "light-0"))
    await _receive(coordinator, "thing-0", "get/accepted", {
        "version": 100, "state": {"reported": {channel: {"lv": 0}}},
        "metadata": {"reported": {channel: {"lv": {"timestamp": 1000}}}},
    })
    try:
        await entity.async_set_percentage(40)
        first = mqtt.published[-1][1]["clientToken"]
        await entity.async_set_percentage(70)
        latest = mqtt.published[-1][1]["clientToken"]
        assert first != latest and entity.percentage == 70
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": first, "code": 403})
        assert entity.percentage == 70
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": latest, "code": 403})
        assert entity.percentage == 0
        await entity.async_set_percentage(50)
        pending = coordinator._pending_fields[("light-0", channel, "level")]
        coordinator._expire_pending_field(("light-0", channel, "level"), pending.generation - 1)
        assert entity.percentage == 50
        coordinator._expire_pending_field(("light-0", channel, "level"), pending.generation)
        assert entity.percentage == 0
        assert len(mqtt.published) == 3
    finally:
        await coordinator.async_shutdown()


async def test_fan_same_controller_channel_isolation(hass: HomeAssistant) -> None:
    coordinator, mqtt, _ = _setup(hass, count=2)
    fans = [
        VivosunCirculationFanEntity(coordinator, "light-0"),
        VivosunDuctFanEntity(coordinator, "light-0"),
        VivosunCirculationFanEntity(coordinator, "light-1"),
    ]
    try:
        await fans[0].async_set_percentage(50)
        await fans[1].async_set_percentage(40)
        await fans[2].async_set_preset_mode("natural_wind")
        assert (fans[0].percentage, fans[1].percentage, fans[2].preset_mode) == (50, 40, "natural_wind")
        assert len(coordinator._pending_fields) == 4
        assert len({topic for topic, _ in mqtt.published}) == 2
        first = mqtt.published[0][1]["clientToken"]
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": first, "code": 403})
        assert fans[0].percentage is None
        assert fans[1].percentage == 40 and fans[2].preset_mode == "natural_wind"
    finally:
        await coordinator.async_shutdown()


async def test_duct_auto_is_zero_output_and_bare_on_changes_mode(hass: HomeAssistant) -> None:
    """Record the unresolved power-button policy without imposing a new Auto rule."""
    coordinator, mqtt, _ = _setup(hass)
    # The controller identity determines Auto=2 instead of the legacy Auto=1.
    coordinator._devices = [DeviceInfo(
        "light-0", "vivosun-VSCTLE42A-test", "topic/0", "GrowHub E42A",
        True, 66078, "controller",
    )]
    coordinator._build_topic_maps()
    duct = VivosunDuctFanEntity(coordinator, "light-0")
    try:
        await _receive(coordinator, "vivosun-VSCTLE42A-test", "get/accepted", {
            "version": 100, "state": {"reported": {"dFan": {"mode": 2, "lv": 0, "inPlan": 0}}},
        })
        assert duct.preset_mode == "auto" and duct.percentage == 0
        await duct.async_turn_on()
        assert mqtt.published[-1][1]["state"]["desired"]["dFan"]["mode"] == 0
        assert mqtt.published[-1][0].startswith("$aws/things/vivosun-VSCTLE42A-test/")
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("channel,percentage,raw", [("cFan", 50, 70), ("dFan", 40, 50)])
async def test_native_ha_fan_service_keeps_latest_intent_across_old_report(
    hass: HomeAssistant, channel: str, percentage: int, raw: int,
) -> None:
    """Real Home Assistant fan service, coordinator ingress and fake MQTT transport."""
    coordinator, mqtt, _ = _setup(hass)
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan",
        platform_name="vivosun_growhub", platform=None,
        scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    fan = (VivosunCirculationFanEntity(coordinator, "light-0") if channel == "cFan"
           else VivosunDuctFanEntity(coordinator, "light-0"))
    await platform.async_add_entities([fan])
    await hass.async_block_till_done()
    assert fan.entity_id is not None
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {channel: {"lv": 0, "mode": 0, "inPlan": 0}}},
            "metadata": {"reported": {channel: {"lv": {"timestamp": 1000}}}},
        })
        await hass.services.async_call(
            "fan", "set_percentage", {"entity_id": fan.entity_id, "percentage": percentage},
            blocking=True,
        )
        assert fan.percentage == percentage
        command = mqtt.published[-1][1]
        assert command["state"]["desired"][channel]["manu"]["lv"] == raw
        await _receive(coordinator, "thing-0", "update/accepted", {
            "version": 101, "clientToken": command["clientToken"],
            "state": {"desired": {channel: {"manu": {"lv": raw}}}},
            "metadata": {"desired": {channel: {"manu": {"lv": {"timestamp": 1010}}}}},
        })
        await _receive(coordinator, "thing-0", "update/documents", {
            "previous": {"version": 100, "state": {"reported": {channel: {"lv": 0}}}},
            "current": {"version": 101, "state": {"reported": {channel: {"lv": 0}}},
                        "metadata": {"reported": {channel: {"lv": {"timestamp": 1000}}}}},
        })
        assert fan.percentage == percentage
        await _receive(coordinator, "thing-0", "update/accepted", {
            "version": 102, "state": {"reported": {channel: {"lv": raw}}},
            "metadata": {"reported": {channel: {"lv": {"timestamp": 1011}}}},
        })
        assert fan.percentage == percentage
        assert ("light-0", channel, "level") not in coordinator._pending_fields
        assert len(mqtt.published) == 1
    finally:
        await coordinator._disconnect_mqtt_client()
        await platform.async_reset()


@pytest.mark.parametrize("channel,percentage", [("cFan", 50), ("dFan", 60)])
@pytest.mark.parametrize("include_version", [True, False], ids=["versioned", "version-absent"])
async def test_fan_report_without_metadata_keeps_previous_field_freshness(
    hass: HomeAssistant, channel: str, percentage: int, include_version: bool,
) -> None:
    """A real report without metadata cannot permit an older field timestamp to replace it."""
    coordinator, _, _ = _setup(hass)
    fan = (VivosunCirculationFanEntity(coordinator, "light-0") if channel == "cFan"
           else VivosunDuctFanEntity(coordinator, "light-0"))
    key = ("light-0", channel, "level")
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {channel: {"lv": 70, "mode": 0}}},
            "metadata": {"reported": {channel: {
                "lv": {"timestamp": 1000}, "mode": {"timestamp": 1000},
            }}},
        })
        assert fan.percentage == percentage
        update: dict[str, object] = {"state": {"reported": {channel: {"lv": 70}}}}
        if include_version:
            update["version"] = 101
        await _receive(coordinator, "thing-0", "update/accepted", update)
        current = coordinator._reported_fields[key]
        assert current.version == (101 if include_version else 100)
        assert current.timestamp == 1000

        # The GET has a newer shadow version but older selected-field metadata.
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 102, "state": {"reported": {channel: {"lv": 0}}},
            "metadata": {"reported": {channel: {"lv": {"timestamp": 900}}}},
        })
        assert fan.percentage == percentage
        assert coordinator._shadow_states["light-0"][channel]["level"] == 70
        assert coordinator._reported_fields[key] == current
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("channel", ["cFan", "dFan"])
@pytest.mark.parametrize("root_level", [None, "invalid", "missing"])
async def test_fan_report_uses_parser_selected_level_and_its_own_metadata(
    hass: HomeAssistant, channel: str, root_level: object,
) -> None:
    """cFan falls back to manual on invalid root; dFan retains explicit root null."""
    coordinator, _, _ = _setup(hass)
    fan = (VivosunCirculationFanEntity(coordinator, "light-0") if channel == "cFan"
           else VivosunDuctFanEntity(coordinator, "light-0"))
    key = ("light-0", channel, "level")
    component: dict[str, object] = {"manu": {"lv": 70}}
    if root_level != "missing":
        component["lv"] = root_level
    raw = {"state": {"reported": {channel: component}}}
    parsed = parse_shadow_document(raw)
    expected = 70 if channel == "cFan" or root_level == "missing" else None
    assert parsed[channel]["level"] == expected
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {channel: {"lv": 50}}},
            "metadata": {"reported": {channel: {"lv": {"timestamp": 1000}}}},
        })
        # Later shadow version, but older root leaf and newer manual leaf.
        await _receive(coordinator, "thing-0", "update/accepted", {
            **raw, "version": 101, "metadata": {
                "reported": {channel: {
                    "lv": {"timestamp": 900}, "manu": {"lv": {"timestamp": 1100}},
                }},
            },
        })
        effective = 70 if expected == 70 else 50
        assert coordinator._shadow_states["light-0"][channel]["level"] == effective
        expected_percentage = {("cFan", 70): 50, ("cFan", 50): 20, ("dFan", 70): 60, ("dFan", 50): 40}
        assert fan.percentage == expected_percentage[(channel, effective)]
        assert coordinator._reported_fields[key].timestamp == (1100 if expected == 70 else 1000)
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("channel,percentage", [("cFan", 50), ("dFan", 60)])
async def test_already_confirmed_fan_speed_sends_without_false_pending_or_warning(
    hass: HomeAssistant, channel: str, percentage: int,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated identical requested speeds only need bounded rejection receipts."""
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._FIELD_CONFIRMATION_TIMEOUT", 0.01)
    coordinator, mqtt, _ = _setup(hass)
    fan = (VivosunCirculationFanEntity(coordinator, "light-0") if channel == "cFan"
           else VivosunDuctFanEntity(coordinator, "light-0"))
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {channel: {"lv": 70, "mode": 0}}},
            "metadata": {"reported": {channel: {
                "lv": {"timestamp": 1000}, "mode": {"timestamp": 1000},
            }}},
        })
        for _ in range(2):
            await fan.async_set_percentage(percentage)
            assert coordinator._pending_fields == {}
            assert fan.percentage == percentage
        tokens = [command["clientToken"] for _, command in mqtt.published]
        assert len(set(tokens)) == 2
        assert len(coordinator._already_confirmed_tokens) == 2
        await asyncio.sleep(0.03)
        assert "remains unconfirmed" not in caplog.text
        assert coordinator._already_confirmed_tokens == {}
        assert coordinator._pending_fields == {}

        await fan.async_set_percentage(percentage)
        token = mqtt.published[-1][1]["clientToken"]
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": token, "code": 403})
        assert f"Cloud rejected {channel} command" in caplog.text
        assert fan.percentage == percentage
        assert coordinator._already_confirmed_tokens == {}
        assert len(mqtt.published) == 3  # No automatic resend.

        await fan.async_set_percentage(percentage)
        assert len(coordinator._already_confirmed_tokens) == 1
        await coordinator._disconnect_mqtt_client()
        assert coordinator._already_confirmed_tokens == {}
        assert coordinator._pending_fields == {}
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("channel,percentage", [("cFan", 50), ("dFan", 60)])
@pytest.mark.parametrize("already_confirmed", [True, False])
async def test_fan_publish_failure_clears_pending_or_confirmed_receipt(
    hass: HomeAssistant, channel: str, percentage: int, already_confirmed: bool,
) -> None:
    """Offline send failure leaves the actual report intact and no receipt/timer behind."""
    coordinator, mqtt, _ = _setup(hass)
    fan = (VivosunCirculationFanEntity(coordinator, "light-0") if channel == "cFan"
           else VivosunDuctFanEntity(coordinator, "light-0"))
    baseline = 70 if already_confirmed else 0
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {channel: {"lv": baseline}}},
            "metadata": {"reported": {channel: {"lv": {"timestamp": 1000}}}},
        })
        mqtt.publish_error = RuntimeError("offline transport")
        with pytest.raises(RuntimeError, match="offline transport"):
            await fan.async_set_percentage(percentage)
        assert mqtt.published == []
        assert coordinator._pending_fields == {}
        assert coordinator._already_confirmed_tokens == {}
        assert coordinator._shadow_states["light-0"][channel]["level"] == baseline
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "client_id,auto_code", [("vivosun-VSCTLE42A-test", 2), ("thing-0", 1)],
)
@pytest.mark.parametrize("reported_level", [0, 70], ids=["auto-zero", "auto-positive"])
async def test_native_ha_auto_toggle_off_projects_mode_and_level_before_report(
    hass: HomeAssistant, client_id: str, auto_code: int, reported_level: int,
) -> None:
    """A duct Auto fan is logically On even if its measured speed is zero."""
    coordinator, mqtt, _ = _setup(hass)
    coordinator._devices = [DeviceInfo(
        "light-0", client_id, "topic/0", "Test controller", True, 66078, "controller",
    )]
    coordinator._build_topic_maps()
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan",
        platform_name="vivosun_growhub", platform=None,
        scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    fan = VivosunDuctFanEntity(coordinator, "light-0")
    await platform.async_add_entities([fan])
    await hass.async_block_till_done()
    assert fan.entity_id is not None
    try:
        await _receive(coordinator, client_id, "get/accepted", {
            "version": 100,
            "state": {"reported": {"dFan": {"mode": auto_code, "lv": reported_level, "inPlan": 0}}},
            "metadata": {"reported": {"dFan": {
                "mode": {"timestamp": 1000}, "lv": {"timestamp": 1000},
            }}},
        })
        assert fan.is_on and fan.preset_mode == "auto"
        assert fan.percentage == (0 if reported_level == 0 else 60)
        assert hass.states.get(fan.entity_id).state == "on"

        await hass.services.async_call("fan", "toggle", {"entity_id": fan.entity_id}, blocking=True)
        assert len(mqtt.published) == 1
        topic, sent = mqtt.published[0]
        assert topic == f"$aws/things/{client_id}/shadow/update"
        assert sent["state"] == {"desired": {"dFan": {"mode": 0, "manu": {"lv": 0}}}}
        assert isinstance(sent.get("clientToken"), str)
        assert not fan.is_on and fan.percentage == 0
        assert fan.preset_mode == "manual"
        assert hass.states.get(fan.entity_id).state == "off"

        # Desired acceptance and an old cached snapshot do not certify either output or mode.
        await _receive(coordinator, client_id, "update/accepted", {
            "clientToken": sent["clientToken"], "version": 101,
            "state": {"desired": {"dFan": {"mode": 0, "manu": {"lv": 0}}}},
            "metadata": {"desired": {"dFan": {
                "mode": {"timestamp": 1010}, "manu": {"lv": {"timestamp": 1010}},
            }}},
        })
        await _receive(coordinator, client_id, "get/accepted", {
            "version": 101, "state": {"reported": {"dFan": {
                "mode": auto_code, "lv": reported_level,
            }}},
            "metadata": {"reported": {"dFan": {
                "mode": {"timestamp": 1000}, "lv": {"timestamp": 1000},
            }}},
        })
        assert not fan.is_on and fan.preset_mode == "manual" and fan.percentage == 0
        assert len(mqtt.published) == 1
    finally:
        await coordinator._disconnect_mqtt_client()
        await platform.async_reset()


@pytest.mark.parametrize("first_field", ["level", "mode"])
async def test_duct_off_confirmation_keeps_other_field_provisional(
    hass: HomeAssistant, first_field: str,
) -> None:
    """Partial reports cannot settle the other locally commanded field."""
    coordinator, mqtt, _ = _setup(hass)
    fan = VivosunDuctFanEntity(coordinator, "light-0")
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {"dFan": {"mode": 1, "lv": 70}}},
            "metadata": {"reported": {"dFan": {
                "mode": {"timestamp": 1000}, "lv": {"timestamp": 1000},
            }}},
        })
        assert fan.is_on and fan.preset_mode == "auto"
        await fan.async_turn_off()
        assert fan.preset_mode == "manual" and fan.percentage == 0 and not fan.is_on
        token = mqtt.published[-1][1]["clientToken"]
        await _receive(coordinator, "thing-0", "update/accepted", {
            "clientToken": token, "version": 101,
            "state": {"desired": {"dFan": {"mode": 0, "manu": {"lv": 0}}}},
            "metadata": {"desired": {"dFan": {
                "mode": {"timestamp": 1010}, "manu": {"lv": {"timestamp": 1010}},
            }}},
        })
        value = 0
        await _receive(coordinator, "thing-0", "update/accepted", {
            "version": 102, "state": {"reported": {"dFan": {first_field.replace("level", "lv"): value}}},
            "metadata": {"reported": {"dFan": {
                first_field.replace("level", "lv"): {"timestamp": 1011},
            }}},
        })
        assert fan.preset_mode == "manual" and fan.percentage == 0 and not fan.is_on
        assert ("light-0", "dFan", first_field) not in coordinator._pending_fields
        remaining = "mode" if first_field == "level" else "level"
        assert ("light-0", "dFan", remaining) in coordinator._pending_fields

        # Old GET with a newer whole-shadow version must not undo still-pending output.
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 103, "state": {"reported": {"dFan": {"mode": 1, "lv": 70}}},
            "metadata": {"reported": {"dFan": {
                "mode": {"timestamp": 1000}, "lv": {"timestamp": 1000},
            }}},
        })
        assert fan.preset_mode == "manual" and fan.percentage == 0
        await _receive(coordinator, "thing-0", "update/accepted", {
            "version": 104,
            "state": {"reported": {"dFan": {"mode" if remaining == "mode" else "lv": 0}}},
            "metadata": {"reported": {"dFan": {
                "mode" if remaining == "mode" else "lv": {"timestamp": 1012},
            }}},
        })
        assert ("light-0", "dFan", remaining) not in coordinator._pending_fields
        assert fan.preset_mode == "manual" and not fan.is_on and fan.percentage == 0
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


async def test_mode_only_auto_is_provisional_without_inventing_output(hass: HomeAssistant) -> None:
    coordinator, mqtt, _ = _setup(hass)
    fan = VivosunDuctFanEntity(coordinator, "light-0")
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {"dFan": {"mode": 0, "lv": 0, "inPlan": 0}}},
            "metadata": {"reported": {"dFan": {
                "mode": {"timestamp": 1000}, "lv": {"timestamp": 1000},
            }}},
        })
        await fan.async_set_preset_mode("auto")
        assert fan.preset_mode == "auto" and fan.is_on
        assert fan.percentage == 0
        token = mqtt.published[-1][1]["clientToken"]
        assert mqtt.published[-1][1]["state"] == {"desired": {"dFan": {"mode": 1}}}
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": token, "code": 403})
        assert fan.preset_mode == "manual" and not fan.is_on and fan.percentage == 0
    finally:
        await coordinator.async_shutdown()


async def test_mode_only_auto_supersedes_old_off_level_without_faking_output(hass: HomeAssistant) -> None:
    coordinator, mqtt, _ = _setup(hass)
    duct = VivosunDuctFanEntity(coordinator, "light-0")
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {"dFan": {"mode": 0, "lv": 70}}},
            "metadata": {"reported": {"dFan": {
                "mode": {"timestamp": 1000}, "lv": {"timestamp": 1000},
            }}},
        })
        await duct.async_turn_off()
        older_token = mqtt.published[-1][1]["clientToken"]
        assert duct.percentage == 0 and not duct.is_on
        assert ("light-0", "dFan", "level") in coordinator._pending_fields

        await duct.async_set_preset_mode("auto")
        latest_token = mqtt.published[-1][1]["clientToken"]
        assert latest_token != older_token
        assert duct.preset_mode == "auto" and duct.is_on
        assert duct.percentage == 60  # Mode-only command uses the genuine report.
        assert ("light-0", "dFan", "level") not in coordinator._pending_fields
        assert ("light-0", "dFan", "mode") in coordinator._pending_fields
        assert mqtt.published[-1][1]["state"] == {"desired": {"dFan": {"mode": 1}}}

        await _receive(coordinator, "thing-0", "update/rejected", {
            "clientToken": older_token, "code": 409,
        })
        assert duct.preset_mode == "auto" and duct.percentage == 60
        await _receive(coordinator, "thing-0", "update/accepted", {
            "clientToken": latest_token, "version": 102,
            "state": {"desired": {"dFan": {"mode": 1}}},
            "metadata": {"desired": {"dFan": {"mode": {"timestamp": 1010}}}},
        })
        assert ("light-0", "dFan", "mode") in coordinator._pending_fields
        await _receive(coordinator, "thing-0", "update/accepted", {
            "version": 103, "state": {"reported": {"dFan": {"mode": 1}}},
            "metadata": {"reported": {"dFan": {"mode": {"timestamp": 1011}}}},
        })
        assert coordinator._pending_fields == {}
        assert duct.preset_mode == "auto" and duct.percentage == 60
        assert len(mqtt.published) == 2
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("outcome", ["rejected", "expired", "disconnect", "send-failed"])
async def test_mode_only_auto_rolls_back_to_real_mode(
    hass: HomeAssistant, outcome: str, caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator, mqtt, _ = _setup(hass)
    duct = VivosunDuctFanEntity(coordinator, "light-0")
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {"dFan": {"mode": 0, "lv": 0, "inPlan": 0}}},
            "metadata": {"reported": {"dFan": {
                "mode": {"timestamp": 1000}, "lv": {"timestamp": 1000},
            }}},
        })
        if outcome == "send-failed":
            mqtt.publish_error = RuntimeError("offline transport")
            with pytest.raises(RuntimeError, match="offline transport"):
                await duct.async_set_preset_mode("auto")
            assert mqtt.published == []
        else:
            await duct.async_set_preset_mode("auto")
            assert duct.preset_mode == "auto" and duct.is_on and duct.percentage == 0
            assert ("light-0", "dFan", "mode") in coordinator._pending_fields
            if outcome == "rejected":
                await _receive(coordinator, "thing-0", "update/rejected", {
                    "clientToken": mqtt.published[-1][1]["clientToken"], "code": 403,
                })
                assert "Cloud rejected dFan command" in caplog.text
            elif outcome == "expired":
                key = ("light-0", "dFan", "mode")
                coordinator._expire_pending_field(key, coordinator._pending_fields[key].generation)
                assert "dFan mode command remains unconfirmed" in caplog.text
            else:
                await coordinator._disconnect_mqtt_client()
        assert duct.preset_mode == "manual" and not duct.is_on and duct.percentage == 0
        assert coordinator._pending_fields == {}
        assert len(mqtt.published) == (0 if outcome == "send-failed" else 1)
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("outcome", ["rejected", "send-failed", "expired"])
async def test_duct_auto_off_rolls_back_both_fields_without_retry(
    hass: HomeAssistant, outcome: str,
) -> None:
    coordinator, mqtt, _ = _setup(hass)
    duct = VivosunDuctFanEntity(coordinator, "light-0")
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {"dFan": {"mode": 1, "lv": 70}}},
            "metadata": {"reported": {"dFan": {
                "mode": {"timestamp": 1000}, "lv": {"timestamp": 1000},
            }}},
        })
        if outcome == "send-failed":
            mqtt.publish_error = RuntimeError("transport offline")
            with pytest.raises(RuntimeError, match="transport offline"):
                await duct.async_turn_off()
            assert mqtt.published == []
        else:
            await duct.async_turn_off()
            assert duct.preset_mode == "manual" and not duct.is_on and duct.percentage == 0
            assert len(coordinator._pending_fields) == 2
            if outcome == "rejected":
                await _receive(coordinator, "thing-0", "update/rejected", {
                    "clientToken": mqtt.published[-1][1]["clientToken"], "code": 403,
                })
            else:
                for key in tuple(coordinator._pending_fields):
                    generation = coordinator._pending_fields[key].generation
                    coordinator._expire_pending_field(key, generation)
        assert coordinator._pending_fields == {}
        assert duct.preset_mode == "auto" and duct.is_on and duct.percentage == 60
        assert len(mqtt.published) == (0 if outcome == "send-failed" else 1)
    finally:
        await coordinator.async_shutdown()


async def test_duct_manual_preset_uses_reported_level_without_speed_write(hass: HomeAssistant) -> None:
    coordinator, mqtt, _ = _setup(hass)
    duct = VivosunDuctFanEntity(coordinator, "light-0")
    try:
        await _receive(coordinator, "thing-0", "get/accepted", {
            "version": 100, "state": {"reported": {"dFan": {"mode": 1, "lv": 70}}},
            "metadata": {"reported": {"dFan": {"mode": {"timestamp": 1000}}}},
        })
        await duct.async_set_preset_mode("manual")
        assert duct.preset_mode == "manual" and duct.is_on and duct.percentage == 60
        assert mqtt.published[-1][1]["state"] == {"desired": {"dFan": {"mode": 0}}}
        assert set(coordinator._pending_fields) == {("light-0", "dFan", "mode")}
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


async def test_duct_mode_pending_is_isolated_from_light_and_other_controller(hass: HomeAssistant) -> None:
    coordinator, mqtt, lights = _setup(hass, count=2)
    first = VivosunDuctFanEntity(coordinator, "light-0")
    second = VivosunDuctFanEntity(coordinator, "light-1")
    try:
        for i in (0, 1):
            await _receive(coordinator, f"thing-{i}", "get/accepted", {
                "version": 100, "state": {"reported": {
                    "dFan": {"mode": 0, "lv": 0}, "light": {"lv": 25},
                }},
                "metadata": {"reported": {
                    "dFan": {"mode": {"timestamp": 1000}, "lv": {"timestamp": 1000}},
                    "light": {"lv": {"timestamp": 1000}},
                }},
            })
        await first.async_set_preset_mode("auto")
        first_token = mqtt.published[-1][1]["clientToken"]
        await second.async_set_preset_mode("auto")
        second_token = mqtt.published[-1][1]["clientToken"]
        await lights[0].async_turn_on(brightness=153)
        assert lights[0].brightness is not None and round(lights[0].brightness * 100 / 255) == 60
        assert first.preset_mode == second.preset_mode == "auto"

        # A matching token on a different controller must not affect this request.
        await _receive(coordinator, "thing-0", "update/rejected", {
            "clientToken": second_token, "code": 403,
        })
        assert first.preset_mode == second.preset_mode == "auto"
        await _receive(coordinator, "thing-0", "update/rejected", {
            "clientToken": first_token, "code": 403,
        })
        assert first.preset_mode == "manual" and not first.is_on
        assert second.preset_mode == "auto" and second.is_on
        assert ("light-0", "light", "level") in coordinator._pending_fields
        assert ("light-1", "dFan", "mode") in coordinator._pending_fields
        assert len(mqtt.published) == 3
    finally:
        await coordinator.async_shutdown()

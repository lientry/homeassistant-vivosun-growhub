"""E42A mode-only programs follow genuine reports and preserve saved settings."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.fan import VivosunCirculationFanEntity
from custom_components.vivosun_growhub.light import VivosunLightEntity
from custom_components.vivosun_growhub.models import RuntimeData
from custom_components.vivosun_growhub.select import VivosunLightModeSelect, async_setup_entry
from tests.test_fan import _controller_device, _StubCoordinator
from tests.test_light_pending import _receive, _setup

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_light_mode_entity_discovery_is_exact_and_has_stable_identity(hass: HomeAssistant) -> None:
    devices = [_controller_device("supported"), replace(
        _controller_device("legacy"), client_id="vivosun-VSCTL001-legacy",
    )]
    coordinator = _StubCoordinator(devices)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    modes = [entity for entity in added if isinstance(entity, VivosunLightModeSelect)]
    assert len(modes) == 1
    assert modes[0].unique_id == "vivosun_growhub_supported_light_mode"
    assert modes[0].options == ["Manual", "Cycle"]
    assert modes[0].current_option is None


async def test_light_and_circulation_modes_are_mode_only_with_reported_authority(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt, _ = _setup(hass)
    coordinator._devices = [replace(coordinator._devices[0], client_id="vivosun-VSCTLE42A-test")]
    coordinator._build_topic_maps()
    light = VivosunLightModeSelect(coordinator, "light-0")
    fan = VivosunCirculationFanEntity(coordinator, "light-0")
    thing = "vivosun-VSCTLE42A-test"
    try:
        await _receive(coordinator, thing, "get/accepted", {
            "version": 100, "state": {"reported": {
                "light": {"mode": 0, "inPlan": 0, "lv": 0, "cycle": {"onDur": 64800}},
                "cFan": {"mode": 0, "inPlan": 0, "lv": 200, "cycle": {"onDur": 3000}},
            }},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 1000}},
                                      "cFan": {"mode": {"timestamp": 1000}}}},
        })
        assert light.current_option == "Manual"
        assert fan.preset_modes == ["manual", "cycle", "natural_wind"]
        assert fan.preset_mode == "natural_wind"

        await light.async_select_option("Cycle")
        await fan.async_turn_on(preset_mode="cycle")
        assert [payload["state"]["desired"] for _, payload in mqtt.published] == [
            {"light": {"mode": 1}}, {"cFan": {"mode": 1}},
        ]
        assert ("light-0", "light", "mode") in coordinator._pending_fields
        assert ("light-0", "cFan", "mode") in coordinator._pending_fields
        assert light.current_option == "Manual"
        assert fan.preset_mode == "natural_wind"

        for channel, token in (("light", mqtt.published[0][1]["clientToken"]),
                               ("cFan", mqtt.published[1][1]["clientToken"])):
            await _receive(coordinator, thing, "update/accepted", {
                "clientToken": token, "version": 101,
                "state": {"desired": {channel: {"mode": 1}}},
            })
        assert light.current_option == "Manual" and fan.preset_mode == "natural_wind"
        await _receive(coordinator, thing, "update/accepted", {
            "version": 102, "state": {"reported": {"light": {"mode": 1}, "cFan": {"mode": 1}}},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 1010}},
                                      "cFan": {"mode": {"timestamp": 1010}}}},
        })
        assert light.current_option == "Cycle" and fan.preset_mode == "cycle"
        assert not [key for key in coordinator._pending_fields if key[-1] == "mode"]

        await light.async_select_option("Manual")
        await fan.async_set_preset_mode("manual")
        assert [payload["state"]["desired"] for _, payload in mqtt.published[2:]] == [
            {"light": {"mode": 0}}, {"cFan": {"mode": 0}},
        ]
        assert light.current_option == "Cycle" and fan.preset_mode == "cycle"
        await _receive(coordinator, thing, "update/accepted", {
            "version": 103, "state": {"reported": {"light": {"mode": 0}, "cFan": {"mode": 0}}},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 1020}},
                                      "cFan": {"mode": {"timestamp": 1020}}}},
        })
        assert light.current_option == "Manual" and fan.preset_mode == "natural_wind"
        assert fan.extra_state_attributes["cycle_settings"]["on_minutes"] == 50
        assert len(mqtt.published) == 4
    finally:
        await coordinator.async_shutdown()


async def test_mode_latest_intent_rejection_timeout_and_unsupported_models(hass: HomeAssistant) -> None:
    coordinator, mqtt, _ = _setup(hass)
    coordinator._devices = [replace(coordinator._devices[0], client_id="vivosun-VSCTLE42A-test")]
    coordinator._build_topic_maps()
    light = VivosunLightModeSelect(coordinator, "light-0")
    fan = VivosunCirculationFanEntity(coordinator, "light-0")
    thing = "vivosun-VSCTLE42A-test"
    try:
        await _receive(coordinator, thing, "get/accepted", {
            "version": 100, "state": {"reported": {"light": {"mode": 0}, "cFan": {"mode": 0}}},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 1000}},
                                      "cFan": {"mode": {"timestamp": 1000}}}},
        })
        await light.async_select_option("Cycle")
        first = mqtt.published[-1][1]["clientToken"]
        await light.async_select_option("Manual")
        second = mqtt.published[-1][1]["clientToken"]
        assert first != second
        await _receive(coordinator, thing, "update/rejected", {"clientToken": first, "code": 403})
        key = ("light-0", "light", "mode")
        assert coordinator._pending_fields[key].token == second
        await _receive(coordinator, thing, "update/rejected", {"clientToken": second, "code": 403})
        assert key not in coordinator._pending_fields and light.current_option == "Manual"
        with pytest.raises(ServiceValidationError):
            await light.async_select_option("Auto")

        await light.async_select_option("Cycle")
        await light.async_select_option("Manual")
        newest = coordinator._pending_fields[key].token
        await _receive(coordinator, thing, "update/accepted", {
            "version": 101, "state": {"reported": {"light": {"mode": 1}}},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 1010}}}},
        })
        # The old genuine state may be visible, but cannot retire newer Manual intent.
        assert light.current_option == "Cycle" and coordinator._pending_fields[key].token == newest
        await _receive(coordinator, thing, "update/accepted", {
            "version": 102, "state": {"reported": {"light": {"mode": 0}}},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 1020}}}},
        })
        assert light.current_option == "Manual" and key not in coordinator._pending_fields

        await fan.async_set_preset_mode("cycle")
        key = ("light-0", "cFan", "mode")
        pending = coordinator._pending_fields[key]
        coordinator._expire_pending_field(key, pending.generation - 1)
        assert key in coordinator._pending_fields
        coordinator._expire_pending_field(key, pending.generation)
        assert key not in coordinator._pending_fields and fan.preset_mode == "manual"
    finally:
        await coordinator.async_shutdown()

    legacy = _StubCoordinator([replace(_controller_device("legacy"),
                                       client_id="vivosun-VSCTL001-legacy")])
    with pytest.raises(ServiceValidationError):
        await VivosunLightModeSelect(cast("object", legacy), "legacy").async_select_option("Cycle")
    with pytest.raises(ValueError):
        await VivosunCirculationFanEntity(cast("object", legacy), "legacy").async_set_preset_mode("cycle")
    legacy.async_publish_shadow_update.assert_not_awaited()


async def test_raw_light_mode_does_not_track_untokened_level(hass: HomeAssistant) -> None:
    coordinator, mqtt, _ = _setup(hass)
    coordinator._devices = [replace(coordinator._devices[0], client_id="vivosun-VSCTLE42A-test")]
    coordinator._build_topic_maps()
    try:
        await coordinator.async_publish_shadow_update(
            {"state": {"desired": {"light": {"mode": 1, "manu": {"lv": 25}}}}}, device_id="light-0",
        )
        assert mqtt.published[-1][1]["state"] == {"desired": {"light": {"mode": 1, "manu": {"lv": 25}}}}
        assert ("light-0", "light", "mode") in coordinator._pending_fields
        assert ("light-0", "light", "level") not in coordinator._pending_fields
    finally:
        await coordinator.async_shutdown()


async def test_mode_confirmation_is_independent_of_newer_light_output(hass: HomeAssistant) -> None:
    coordinator, mqtt, _ = _setup(hass)
    coordinator._devices = [replace(coordinator._devices[0], client_id="vivosun-VSCTLE42A-test")]
    coordinator._build_topic_maps()
    thing = "vivosun-VSCTLE42A-test"
    mode = VivosunLightModeSelect(coordinator, "light-0")
    try:
        await _receive(coordinator, thing, "get/accepted", {
            "version": 98, "state": {"reported": {"light": {"mode": 0, "lv": 0}}},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 980}, "lv": {"timestamp": 980}}}},
        })
        await mode.async_select_option("Cycle")
        await _receive(coordinator, thing, "update/accepted", {
            "version": 99, "clientToken": mqtt.published[-1][1]["clientToken"],
            "state": {"desired": {"light": {"mode": 1}}},
        })
        await _receive(coordinator, thing, "update/accepted", {
            "version": 102, "state": {"reported": {"light": {"lv": 50}}},
            "metadata": {"reported": {"light": {"lv": {"timestamp": 1020}}}},
        })
        await _receive(coordinator, thing, "update/accepted", {
            "version": 101, "state": {"reported": {"light": {"mode": 1, "lv": 0}}},
            "metadata": {"reported": {"light": {"mode": {"timestamp": 1010}, "lv": {"timestamp": 1000}}}},
        })
        assert mode.current_option == "Cycle"
        assert ("light-0", "light", "mode") not in coordinator._pending_fields
        assert VivosunLightEntity(coordinator, "light-0").brightness == 128
    finally:
        await coordinator.async_shutdown()

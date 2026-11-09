"""Tests for Vivosun fan platform."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from discovery_stubs import DiscoveryCoordinatorStub
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN, OPTION_CFAN_OSCILLATION_CONTROLLERS
from custom_components.vivosun_growhub.fan import (
    VivosunCirculationFanEntity,
    VivosunDuctFanEntity,
    async_setup_entry,
)
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.shadow import (
    build_cfan_level_payload,
    build_cfan_oscillate_payload,
    build_dfan_auto_mode_payload,
    build_dfan_auto_threshold_payload,
    build_dfan_level_payload,
    parse_shadow_document,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
from homeassistant.components.fan import FanEntityFeature

_DEV_ID = "dev-1"
_DEV_ID_2 = "dev-2"


def _controller_device(device_id: str) -> DeviceInfo:
    return DeviceInfo(
        device_id=device_id,
        client_id=f"vivosun-VSCTLE42A-acc-{device_id}",
        topic_prefix=f"prefix/{device_id}",
        name=f"GrowHub {device_id}",
        online=True,
        scene_id=66078,
        device_type="controller",
    )


class _StubCoordinator(DiscoveryCoordinatorStub):
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self.data: dict[str, object] = {}
        self._devices = devices or [_controller_device(_DEV_ID)]
        self.is_mqtt_connected = True
        self.async_publish_shadow_update = AsyncMock()
        self.async_publish_selected_fan_level = AsyncMock()

    def manual_memory_enabled(self, device_id: str, channel: str) -> bool:
        """Options default off for these legacy fan behavior tests."""
        return False

    def remembered_manual_selection(self, device_id: str, channel: str) -> int | str | None:
        return None

    def manual_restore_allowed(self, device_id: str, channel: str) -> bool:
        return False

    @property
    def device(self) -> DeviceInfo:
        return self._devices[0]

    @property
    def devices(self) -> list[DeviceInfo]:
        return self._devices

    def get_device(self, device_id: str) -> DeviceInfo | None:
        for device in self._devices:
            if device_id == device.device_id:
                return device
        return None


def _make_cfan(coordinator: _StubCoordinator) -> VivosunCirculationFanEntity:
    return VivosunCirculationFanEntity(cast("object", coordinator), _DEV_ID)


def _make_dfan(coordinator: _StubCoordinator) -> VivosunDuctFanEntity:
    return VivosunDuctFanEntity(cast("object", coordinator), _DEV_ID)


async def test_fan_setup_creates_two_entities(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[object] = []

    def _add(entities: list[object]) -> None:
        added.extend(entities)

    platform = MagicMock()
    platform.async_register_entity_service = MagicMock()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("custom_components.vivosun_growhub.fan.async_get_current_platform", lambda: platform)
        await async_setup_entry(hass, entry, _add)

    assert len(added) == 2
    assert isinstance(added[0], VivosunCirculationFanEntity)
    assert isinstance(added[1], VivosunDuctFanEntity)
    platform.async_register_entity_service.assert_called_once()


async def test_fan_setup_creates_entities_for_each_controller(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator([_controller_device(_DEV_ID), _controller_device(_DEV_ID_2)])
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={
        OPTION_CFAN_OSCILLATION_CONTROLLERS: [_DEV_ID_2],
    })
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[object] = []

    def _add(entities: list[object]) -> None:
        added.extend(entities)

    platform = MagicMock()
    platform.async_register_entity_service = MagicMock()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("custom_components.vivosun_growhub.fan.async_get_current_platform", lambda: platform)
        await async_setup_entry(hass, entry, _add)

    assert len(added) == 4
    assert {entity.unique_id for entity in added} == {
        f"vivosun_growhub_{_DEV_ID}_cfan",
        f"vivosun_growhub_{_DEV_ID}_dfan",
        f"vivosun_growhub_{_DEV_ID_2}_cfan",
        f"vivosun_growhub_{_DEV_ID_2}_dfan",
    }
    by_id = {entity.unique_id: entity for entity in added}
    assert not by_id[f"vivosun_growhub_{_DEV_ID}_cfan"].supported_features & FanEntityFeature.OSCILLATE
    assert by_id[f"vivosun_growhub_{_DEV_ID_2}_cfan"].supported_features & FanEntityFeature.OSCILLATE
    platform.async_register_entity_service.assert_called_once()


async def test_circulation_fan_state_and_commands() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadows": {
            _DEV_ID: {
                "cFan": {"level": 70, "oscillating": True},
                "connection": {"connected": True},
            }
        }
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        OPTION_CFAN_OSCILLATION_CONTROLLERS: [_DEV_ID],
    })
    entity = VivosunCirculationFanEntity(cast("object", coordinator), _DEV_ID, entry=entry)

    assert entity.unique_id == f"vivosun_growhub_{_DEV_ID}_cfan"
    assert entity.percentage == 50
    assert entity.oscillating is True
    assert entity.preset_mode is None
    assert entity.available is True

    await entity.async_set_percentage(70)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_level_payload(80), device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_oscillate(False)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_oscillate_payload(False), device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_preset_mode("natural_wind")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_level_payload(200), device_id=_DEV_ID
    )

    with pytest.raises(ValueError):
        await entity.async_set_preset_mode("invalid")

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_level_payload(70), device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_level_payload(0), device_id=_DEV_ID
    )


async def test_duct_fan_state_and_commands() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadows": {
            _DEV_ID: {
                "dFan": {
                    "level": 60,
                    "auto_enabled": True,
                    "auto": {
                        "lvMin": 20,
                        "lvMax": 90,
                        "tMin": None,
                        "tMax": 2800,
                        "hMin": None,
                        "hMax": 7000,
                        "vpdMin": None,
                        "vpdMax": 180,
                        "tStep": 90,
                        "hStep": 65,
                        "vpdStep": 1,
                        "exChk": 1,
                    },
                },
                "connection": {"connected": True},
            }
        }
    }
    entity = _make_dfan(coordinator)

    assert entity.unique_id == f"vivosun_growhub_{_DEV_ID}_dfan"
    assert entity.percentage == 50
    assert entity.preset_mode == "auto"
    assert entity.extra_state_attributes["lvMin"] == 20
    assert entity.extra_state_attributes["tMin"] is None

    await entity.async_set_percentage(55)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_level_payload(70), device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_preset_mode("auto")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"dFan": {"mode": 2}}}}, device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_preset_mode("manual")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_auto_mode_payload(False), device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_auto_threshold("tMax", 3000)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_auto_threshold_payload("tMax", 3000), device_id=_DEV_ID
    )

    with pytest.raises(ValueError):
        await entity.async_set_auto_threshold("invalid", 1)

    with pytest.raises(ValueError):
        await entity.async_set_preset_mode("invalid")

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_level_payload(60), device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_level_payload(0), device_id=_DEV_ID
    )


async def test_duct_fan_auto_turn_on_preserves_preset_and_legacy_mode() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_DEV_ID: {"dFan": {"level": 60, "auto_enabled": False}}}}
    entity = _make_dfan(coordinator)

    await entity.async_turn_on(preset_mode="auto")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"dFan": {"mode": 2}}}},
        device_id=_DEV_ID,
    )

    legacy = replace(_controller_device(_DEV_ID), client_id="vivosun-VSCTL001-acc-dev-1")
    old_coordinator = _StubCoordinator([legacy])
    old_coordinator.data = {"shadows": {_DEV_ID: {"dFan": {"level": 60, "auto_enabled": False}}}}
    await _make_dfan(old_coordinator).async_turn_on(preset_mode="auto")
    old_coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"dFan": {"mode": 1}}}},
        device_id=_DEV_ID,
    )


async def test_circulation_fan_natural_wind_maps_to_preset_and_on_state() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadows": {
            _DEV_ID: {
                "cFan": {"level": 200, "oscillating": False},
                "connection": {"connected": True},
            }
        }
    }
    entity = _make_cfan(coordinator)

    assert entity.percentage is None
    assert entity.preset_mode == "natural_wind"
    assert entity.is_on is True


@pytest.mark.parametrize(
    ("level", "nw", "preset"),
    [(70, 0, None), (70, 1, None), (0, 1, None), (200, 0, "natural_wind"), (200, 1, "natural_wind")],
)
async def test_circulation_fan_native_preset_is_only_natural_wind(
    level: int, nw: int, preset: str | None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_DEV_ID: parse_shadow_document({
        "state": {"reported": {"cFan": {"mode": 0, "lv": level, "nw": nw}}}
    })}}
    entity = _make_cfan(coordinator)

    assert entity.preset_modes == ["natural_wind"]
    assert entity.preset_mode == preset
    assert "night_mode" not in coordinator.data["shadows"][_DEV_ID]["cFan"]


async def test_circulation_fan_legacy_normal_and_night_preset_requests_are_rejected() -> None:
    coordinator = _StubCoordinator()
    entity = _make_cfan(coordinator)
    for preset in ("normal", "night"):
        with pytest.raises(ValueError, match="Unsupported circulation fan preset"):
            await entity.async_set_preset_mode(preset)
        with pytest.raises(ValueError, match="Unsupported circulation fan preset"):
            await entity.async_turn_on(preset_mode=preset)
    coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_circulation_fan_bare_on_does_not_cancel_active_natural_wind() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_DEV_ID: {"cFan": {"level": 200}}}}
    entity = _make_cfan(coordinator)

    await entity.async_turn_on()

    coordinator.async_publish_shadow_update.assert_not_awaited()
    coordinator.async_publish_selected_fan_level.assert_not_awaited()


async def test_circulation_fan_oscillation_requires_current_controller_opt_in(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_DEV_ID: {"cFan": {"level": 70, "oscillating": True}}}}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entity = VivosunCirculationFanEntity(cast("object", coordinator), _DEV_ID, entry=entry)

    assert not entity.supported_features & FanEntityFeature.OSCILLATE
    assert entity.oscillating is None
    with pytest.raises(ServiceValidationError, match="Oscillation control is disabled"):
        await entity.async_oscillate(True)
    coordinator.async_publish_shadow_update.assert_not_awaited()

    hass.config_entries.async_update_entry(entry, options={OPTION_CFAN_OSCILLATION_CONTROLLERS: [_DEV_ID_2]})
    assert not entity.supported_features & FanEntityFeature.OSCILLATE
    hass.config_entries.async_update_entry(entry, options={OPTION_CFAN_OSCILLATION_CONTROLLERS: [_DEV_ID]})
    assert entity.supported_features & FanEntityFeature.OSCILLATE
    assert entity.oscillating is True
    await entity.async_oscillate(False)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_oscillate_payload(False), device_id=_DEV_ID
    )

    # A stale reference must not publish after Options disables the feature.
    hass.config_entries.async_update_entry(entry, options={})
    assert not entity.supported_features & FanEntityFeature.OSCILLATE
    assert entity.oscillating is None
    with pytest.raises(ServiceValidationError, match="Oscillation control is disabled"):
        await entity.async_oscillate(True)
    assert coordinator.async_publish_shadow_update.await_count == 1


async def test_circulation_fan_turn_on_with_natural_wind_preset_does_not_override_level() -> None:
    coordinator = _StubCoordinator()
    entity = _make_cfan(coordinator)

    await entity.async_turn_on(preset_mode="natural_wind")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_level_payload(200), device_id=_DEV_ID
    )


@pytest.mark.parametrize(
    "reported_level",
    [70, 200, None],
    ids=["known-manual-speed", "already-natural-wind", "unknown-speed"],
)
async def test_circulation_fan_preset_only_natural_wind_never_replays_old_speed(
    reported_level: int | None,
) -> None:
    coordinator = _StubCoordinator()
    if reported_level is not None:
        coordinator.data = {
            "shadows": {
                _DEV_ID: parse_shadow_document(
                    {"state": {"reported": {"cFan": {
                        "mode": 0,
                        "lv": reported_level,
                        "manu": {"lv": reported_level},
                        "nw": 0,
                        "inPlan": 0,
                    }}}}
                ),
            }
        }
    entity = _make_cfan(coordinator)

    await entity.async_turn_on(preset_mode="natural_wind")

    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_level_payload(200), device_id=_DEV_ID
    )


async def test_circulation_fan_explicit_percentage_still_wins_over_simultaneous_preset() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {"shadows": {_DEV_ID: {"cFan": {"level": 70}}}}
    entity = _make_cfan(coordinator)

    await entity.async_turn_on(preset_mode="natural_wind", percentage=60)

    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_level_payload(75), device_id=_DEV_ID,
    )


@pytest.mark.parametrize("preset", ("auto", "manual"))
async def test_duct_fan_explicit_percentage_skips_losing_preset(preset: str) -> None:
    coordinator = _StubCoordinator()
    entity = _make_dfan(coordinator)

    await entity.async_turn_on(preset_mode=preset, percentage=60)

    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_level_payload(70), device_id=_DEV_ID,
    )


@pytest.mark.parametrize(
    ("channel", "preset"),
    [("cFan", "auto"), ("dFan", "natural_wind"), ("dFan", "unsupported")],
)
async def test_unsupported_preset_with_percentage_fails_before_publishing(
    channel: str, preset: str,
) -> None:
    coordinator = _StubCoordinator()
    entity = _make_cfan(coordinator) if channel == "cFan" else _make_dfan(coordinator)
    with pytest.raises(ValueError, match="Unsupported"):
        await entity.async_turn_on(preset_mode=preset, percentage=60)
    coordinator.async_publish_shadow_update.assert_not_awaited()
    coordinator.async_publish_selected_fan_level.assert_not_awaited()


async def test_fan_turn_on_without_existing_level_uses_minimum_safe_default() -> None:
    coordinator = _StubCoordinator()
    cfan = _make_cfan(coordinator)
    dfan = _make_dfan(coordinator)

    await cfan.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_cfan_level_payload(44), device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    await dfan.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_level_payload(30), device_id=_DEV_ID
    )


async def test_fan_toggle_bridges_plain_on_off_actions() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadows": {
            _DEV_ID: {
                "dFan": {"level": 0, "auto_enabled": False},
                "connection": {"connected": True},
            }
        }
    }
    entity = _make_dfan(coordinator)

    await entity.async_toggle()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_level_payload(30), device_id=_DEV_ID
    )

    coordinator.async_publish_shadow_update.reset_mock()
    coordinator.data["shadows"][_DEV_ID] = {
        "dFan": {"level": 60, "auto_enabled": False},
        "connection": {"connected": True},
    }

    await entity.async_toggle()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        build_dfan_level_payload(0), device_id=_DEV_ID
    )


def test_fans_advertise_explicit_turn_on_off_support() -> None:
    coordinator = _StubCoordinator()
    cfan = _make_cfan(coordinator)
    dfan = _make_dfan(coordinator)

    turn_on_feature = getattr(FanEntityFeature, "TURN_ON", FanEntityFeature(0))
    turn_off_feature = getattr(FanEntityFeature, "TURN_OFF", FanEntityFeature(0))

    if turn_on_feature != FanEntityFeature(0):
        assert cfan.supported_features & turn_on_feature
        assert dfan.supported_features & turn_on_feature

    if turn_off_feature != FanEntityFeature(0):
        assert cfan.supported_features & turn_off_feature
        assert dfan.supported_features & turn_off_feature

"""Tests for Vivosun fan platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.fan import (
    VivosunCirculationFanEntity,
    VivosunDuctFanEntity,
    async_setup_entry,
)
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.shadow import (
    build_cfan_level_payload,
    build_cfan_night_mode_payload,
    build_cfan_oscillate_payload,
    build_dfan_auto_mode_payload,
    build_dfan_auto_threshold_payload,
    build_dfan_level_payload,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
from homeassistant.components.fan import FanEntityFeature


class _StubCoordinator:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.device = DeviceInfo(
            device_id="dev-1",
            client_id="vivosun-VSCTLE42A-acc-dev-1",
            topic_prefix="prefix",
            name="GrowHub",
            online=True,
            scene_id=66078,
        )
        self.is_mqtt_connected = True
        self.async_publish_shadow_update = AsyncMock()


def _make_cfan(coordinator: _StubCoordinator) -> VivosunCirculationFanEntity:
    return VivosunCirculationFanEntity(cast("object", coordinator))


def _make_dfan(coordinator: _StubCoordinator) -> VivosunDuctFanEntity:
    return VivosunDuctFanEntity(cast("object", coordinator))


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


async def test_circulation_fan_state_and_commands() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadow": {
            "cFan": {"level": 70, "oscillating": True, "night_mode": False},
            "connection": {"connected": True},
        }
    }
    entity = _make_cfan(coordinator)

    assert entity.unique_id == "vivosun_growhub_dev-1_cfan"
    assert entity.percentage == 50
    assert entity.oscillating is True
    assert entity.preset_mode == "normal"
    assert entity.available is True

    await entity.async_set_percentage(70)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_level_payload(80))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_oscillate(False)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_oscillate_payload(False))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_preset_mode("night")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_night_mode_payload(True))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_preset_mode("normal")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_night_mode_payload(False))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_preset_mode("natural_wind")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_level_payload(200))

    with pytest.raises(ValueError):
        await entity.async_set_preset_mode("invalid")

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_level_payload(70))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_level_payload(0))


async def test_duct_fan_state_and_commands() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadow": {
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
    entity = _make_dfan(coordinator)

    assert entity.unique_id == "vivosun_growhub_dev-1_dfan"
    assert entity.percentage == 50
    assert entity.preset_mode == "auto"
    assert entity.extra_state_attributes["lvMin"] == 20
    assert entity.extra_state_attributes["tMin"] is None

    await entity.async_set_percentage(55)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_level_payload(70))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_preset_mode("auto")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_auto_mode_payload(True))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_preset_mode("manual")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_auto_mode_payload(False))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_set_auto_threshold("tMax", 3000)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_auto_threshold_payload("tMax", 3000))

    with pytest.raises(ValueError):
        await entity.async_set_auto_threshold("invalid", 1)

    with pytest.raises(ValueError):
        await entity.async_set_preset_mode("invalid")

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_level_payload(60))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_level_payload(0))


async def test_circulation_fan_natural_wind_maps_to_preset_and_on_state() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadow": {
            "cFan": {"level": 200, "oscillating": False, "night_mode": False},
            "connection": {"connected": True},
        }
    }
    entity = _make_cfan(coordinator)

    assert entity.percentage is None
    assert entity.preset_mode == "natural_wind"
    assert entity.is_on is True


async def test_circulation_fan_turn_on_with_natural_wind_preset_does_not_override_level() -> None:
    coordinator = _StubCoordinator()
    entity = _make_cfan(coordinator)

    await entity.async_turn_on(preset_mode="natural_wind")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_level_payload(200))


async def test_fan_turn_on_without_existing_level_uses_minimum_safe_default() -> None:
    coordinator = _StubCoordinator()
    cfan = _make_cfan(coordinator)
    dfan = _make_dfan(coordinator)

    await cfan.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_cfan_level_payload(44))

    coordinator.async_publish_shadow_update.reset_mock()
    await dfan.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_level_payload(30))


async def test_fan_toggle_bridges_plain_on_off_actions() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadow": {
            "dFan": {"level": 0, "auto_enabled": False},
            "connection": {"connected": True},
        }
    }
    entity = _make_dfan(coordinator)

    await entity.async_toggle()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_level_payload(30))

    coordinator.async_publish_shadow_update.reset_mock()
    coordinator.data["shadow"] = {
        "dFan": {"level": 60, "auto_enabled": False},
        "connection": {"connected": True},
    }

    await entity.async_toggle()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_dfan_level_payload(0))


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

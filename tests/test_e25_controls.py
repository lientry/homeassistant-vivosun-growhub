"""E25 selected-port discovery against genuine reports and HA's entity registry."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.fan import (
    VivosunCirculationFanEntity,
    VivosunDuctFanEntity,
)
from custom_components.vivosun_growhub.fan import (
    async_setup_entry as setup_fan,
)
from custom_components.vivosun_growhub.light import VivosunLightEntity
from custom_components.vivosun_growhub.light import async_setup_entry as setup_light
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant

DEVICE = DeviceInfo("e25-one", "vivosun-VSCTL002-acc-e25-one", "prefix/e25", "E25", True, 0, "controller")
E42A = DeviceInfo("e42-one", "vivosun-VSCTLE42A-acc-e42-one", "prefix/e42", "E42A", True, 0, "controller")
TOPIC = f"$aws/things/{DEVICE.client_id}/shadow/"
OPTIONS = {
    2: frozenset({"light", "cFan"}),
    4: frozenset({"cFan"}),
    5: frozenset({"dFan"}),
}


def _coordinator(hass: HomeAssistant, *, devices: list[DeviceInfo] | None = None) -> VivosunCoordinator:
    coordinator = VivosunCoordinator(hass, object(), email="unused", password="unused")
    coordinator._devices = devices if devices is not None else [DEVICE]
    coordinator._build_topic_maps()
    coordinator._mqtt_client = MagicMock(is_connected=True)
    coordinator.async_publish_shadow_update = AsyncMock()
    coordinator.async_publish_light_level = AsyncMock()
    coordinator.async_publish_selected_fan_level = AsyncMock()
    return coordinator


async def _report(
    coordinator: VivosunCoordinator, selection: object = None, *,
    version: int = 1, timestamp: int = 100, desired: bool = False,
    include_selection: bool = True, topic: str = "get/accepted",
) -> None:
    root = {"ctrlTyp": selection} if include_selection else {"cFan": {"lv": 40}}
    payload: dict[str, object] = {
        "version": version,
        "state": {"desired" if desired else "reported": root},
        "metadata": {"reported": {"ctrlTyp": {"timestamp": timestamp}}},
    }
    await coordinator._handle_mqtt_publish(TOPIC + topic, json.dumps(payload).encode(), 0)


@asynccontextmanager
async def _platforms(
    hass: HomeAssistant, coordinator: VivosunCoordinator, *, entry: MockConfigEntry | None = None,
) -> AsyncIterator[tuple[MockConfigEntry, EntityPlatform, EntityPlatform]]:
    """Set up real HA entity platforms without the live account or MQTT."""
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, title="E25", data={})
        entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id, coordinator=coordinator,
    )
    fan_platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    light_platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="light", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    fan_platform.config_entry = entry
    light_platform.config_entry = entry

    def add_fans(entities: list[object]) -> None:
        hass.async_create_task(fan_platform.async_add_entities(cast("list", entities)))

    def add_lights(entities: list[object]) -> None:
        hass.async_create_task(light_platform.async_add_entities(cast("list", entities)))

    try:
        with patch("custom_components.vivosun_growhub.fan.async_get_current_platform", return_value=fan_platform):
            await setup_fan(hass, entry, add_fans)
        with patch("custom_components.vivosun_growhub.light.async_get_current_platform", return_value=light_platform):
            await setup_light(hass, entry, add_lights)
        await hass.async_block_till_done()
        yield entry, fan_platform, light_platform
    finally:
        await entry._async_process_on_unload(hass)
        await fan_platform.async_reset()
        await light_platform.async_reset()


@pytest.mark.parametrize(("selection", "expected"), OPTIONS.items())
async def test_reported_e25_selection_enables_exact_terminals(
    hass: HomeAssistant, selection: int, expected: frozenset[str],
) -> None:
    coordinator = _coordinator(hass)
    await _report(coordinator, selection)
    assert {channel for channel in ("light", "cFan", "dFan")
            if coordinator.supports_control(DEVICE.device_id, channel)} == expected


async def test_sparse_desired_stale_and_invalid_e25_reports(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    assert not any(coordinator.supports_control(DEVICE.device_id, ch) for ch in ("light", "cFan", "dFan"))
    await _report(coordinator, 4, desired=True)
    assert not coordinator.supports_control(DEVICE.device_id, "cFan")
    await _report(coordinator, 4, version=2, timestamp=200)
    await _report(coordinator, 5, version=1, timestamp=100)
    await _report(coordinator, include_selection=False, version=3)
    assert coordinator.supports_control(DEVICE.device_id, "cFan")
    await _report(coordinator, 5, version=4, timestamp=400)
    assert coordinator.supports_control(DEVICE.device_id, "dFan")
    await _report(coordinator, "4", version=5, timestamp=500)
    assert coordinator.supports_control(DEVICE.device_id, "dFan")
    await _report(coordinator, True, version=6, timestamp=600)
    await _report(coordinator, 3, version=7, timestamp=700)
    assert coordinator.supports_control(DEVICE.device_id, "dFan")
    assert not coordinator.supports_control(DEVICE.device_id, "light")


async def test_e25_discovery_is_incremental_and_retains_native_registry(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (entry, fan_platform, light_platform):
        assert not fan_platform.entities and not light_platform.entities

        await _report(coordinator, 4, version=1)
        await hass.async_block_till_done()
        assert len(fan_platform.entities) == 1
        cfan = next(iter(fan_platform.entities.values()))
        assert isinstance(cfan, VivosunCirculationFanEntity)
        assert not light_platform.entities
        registry = er.async_get(hass)
        cfan_id = registry.async_get_entity_id("fan", DOMAIN, "vivosun_growhub_e25-one_cfan")
        assert cfan_id is not None
        registry.async_update_entity(cfan_id, name="My Custom Fan", hidden_by=er.RegistryEntryHider.USER)
        disabled = registry.async_get_or_create(
            "fan", DOMAIN, "vivosun_growhub_e25-one_dfan", config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )

        await _report(coordinator, 4, version=2, timestamp=200)
        await hass.async_block_till_done()
        assert fan_platform.entities[cfan_id] is cfan

        await _report(coordinator, 2, version=3, timestamp=300)
        await hass.async_block_till_done()
        assert len(light_platform.entities) == 1
        light = next(iter(light_platform.entities.values()))
        assert isinstance(light, VivosunLightEntity)
        light_id = light.entity_id
        assert fan_platform.entities[cfan_id] is cfan

        await _report(coordinator, 5, version=4, timestamp=400)
        await hass.async_block_till_done()
        assert not fan_platform.entities  # User-disabled duct fan stays excluded.
        assert registry.async_get(disabled.entity_id).disabled_by == er.RegistryEntryDisabler.USER
        assert cfan_id not in fan_platform.entities
        assert light_id not in light_platform.entities
        assert hass.states.get(cfan_id).state == "unavailable"
        assert hass.states.get(cfan_id).attributes["restored"] is True
        assert hass.states.get(light_id).attributes["restored"] is True
        assert cfan.available is False
        assert light.available is False
        with pytest.raises(ServiceValidationError):
            await cfan.async_turn_on(percentage=50)
        with pytest.raises(ServiceValidationError):
            await light.async_turn_off()
        coordinator.async_publish_shadow_update.assert_not_awaited()
        coordinator.async_publish_light_level.assert_not_awaited()
        coordinator.async_publish_selected_fan_level.assert_not_awaited()
        assert registry.async_get(cfan_id).name == "My Custom Fan"

        await _report(coordinator, 4, version=5, timestamp=500)
        await hass.async_block_till_done()
        assert cfan_id in fan_platform.entities
        assert fan_platform.entities[cfan_id] is not cfan
        assert registry.async_get(cfan_id).name == "My Custom Fan"
        assert registry.async_get(cfan_id).hidden_by == er.RegistryEntryHider.USER
        assert "restored" not in hass.states.get(cfan_id).attributes


async def test_deleted_deselected_e25_registry_row_is_not_recreated_early(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (_, fan_platform, _):
        registry = er.async_get(hass)
        unique_id = "vivosun_growhub_e25-one_cfan"
        await _report(coordinator, 4)
        await hass.async_block_till_done()
        old_id = registry.async_get_entity_id("fan", DOMAIN, unique_id)
        assert old_id in fan_platform.entities

        await _report(coordinator, 5, version=2, timestamp=200)
        await hass.async_block_till_done()
        assert old_id not in fan_platform.entities
        registry.async_remove(old_id)
        await _report(coordinator, include_selection=False, version=3, timestamp=300)
        await _report(coordinator, "4", version=4, timestamp=400)
        await _report(coordinator, 5, version=5, timestamp=500)
        await hass.async_block_till_done()
        assert registry.async_get_entity_id("fan", DOMAIN, unique_id) is None
        assert old_id not in fan_platform.entities

        await _report(coordinator, 4, version=6, timestamp=600)
        await hass.async_block_till_done()
        new_id = registry.async_get_entity_id("fan", DOMAIN, unique_id)
        assert new_id in fan_platform.entities
        assert isinstance(fan_platform.entities[new_id], VivosunCirculationFanEntity)


async def test_e25_disabled_row_preserved_and_reenabled_at_next_selection(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (entry, fan_platform, _):
        registry = er.async_get(hass)
        disabled = registry.async_get_or_create(
            "fan", DOMAIN, "vivosun_growhub_e25-one_dfan", config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        await _report(coordinator, 5)
        await hass.async_block_till_done()
        assert not fan_platform.entities
        assert registry.async_get(disabled.entity_id).disabled_by == er.RegistryEntryDisabler.USER

        await _report(coordinator, 4, version=2, timestamp=200)
        await hass.async_block_till_done()
        assert len(fan_platform.entities) == 1
        registry.async_update_entity(disabled.entity_id, disabled_by=None)
        await _report(coordinator, 5, version=3, timestamp=300)
        await hass.async_block_till_done()
        assert list(fan_platform.entities) == [disabled.entity_id]
        assert isinstance(fan_platform.entities[disabled.entity_id], VivosunDuctFanEntity)


async def test_e25_rapid_selection_reversals_serialize_native_add_and_remove(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (_, fan_platform, light_platform):
        started = asyncio.Event()
        resume = asyncio.Event()
        original_add = fan_platform.async_add_entities

        async def slow_add(entities: list[object]) -> None:
            started.set()
            await resume.wait()
            await original_add(cast("list", entities))

        with patch.object(fan_platform, "async_add_entities", side_effect=slow_add):
            await _report(coordinator, 4)
            await asyncio.wait_for(started.wait(), timeout=2)
            await _report(coordinator, 5, version=2, timestamp=200)
            resume.set()
            await hass.async_block_till_done()

        assert len(fan_platform.entities) == 1
        assert isinstance(next(iter(fan_platform.entities.values())), VivosunDuctFanEntity)
        assert not light_platform.entities
        cfan_id = er.async_get(hass).async_get_entity_id("fan", DOMAIN, "vivosun_growhub_e25-one_cfan")
        assert cfan_id not in fan_platform.entities
        assert hass.states.get(cfan_id).attributes["restored"] is True

        await _report(coordinator, 2, version=3, timestamp=300)
        await _report(coordinator, 4, version=4, timestamp=400)
        await _report(coordinator, 5, version=5, timestamp=500)
        await _report(coordinator, 2, version=6, timestamp=600)
        await hass.async_block_till_done()
        assert len(fan_platform.entities) == len(light_platform.entities) == 1
        assert isinstance(next(iter(fan_platform.entities.values())), VivosunCirculationFanEntity)
        assert isinstance(next(iter(light_platform.entities.values())), VivosunLightEntity)
        coordinator.async_publish_shadow_update.assert_not_awaited()
        coordinator.async_publish_light_level.assert_not_awaited()
        coordinator.async_publish_selected_fan_level.assert_not_awaited()


async def test_e25_native_add_failure_recovers_without_changed_report(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (_, fan_platform, _):
        original_add = fan_platform.async_add_entities
        attempts = 0

        async def flaky_add(entities: list[object]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("native add failed")
            await original_add(cast("list", entities))

        with (
            patch.object(fan_platform, "async_add_entities", side_effect=flaky_add),
            patch("custom_components.vivosun_growhub.discovery.async_call_later") as schedule,
        ):
            await _report(coordinator, 4)
            await hass.async_block_till_done()
            assert attempts == 1 and not fan_platform.entities
            assert schedule.call_count == 1
            assert schedule.call_args.args[1] == 1

            await _report(coordinator, 4, version=2, timestamp=200)
            await hass.async_block_till_done()
            assert attempts == 1

            schedule.call_args.args[2](datetime.now(UTC))
            await hass.async_block_till_done()
            assert attempts == 2
            assert len(fan_platform.entities) == 1
            assert isinstance(next(iter(fan_platform.entities.values())), VivosunCirculationFanEntity)
            assert schedule.call_count == 1
            coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_e25_native_failures_exhaust_two_retries_then_inventory_recovers(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (_, fan_platform, _):
        original_add = fan_platform.async_add_entities
        attempts = 0

        async def flaky_add(entities: list[object]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                raise RuntimeError("native add failed")
            await original_add(cast("list", entities))

        with (
            patch.object(fan_platform, "async_add_entities", side_effect=flaky_add),
            patch("custom_components.vivosun_growhub.discovery.async_call_later") as schedule,
        ):
            await _report(coordinator, 4)
            await hass.async_block_till_done()
            for attempt in (2, 3):
                assert schedule.call_count == attempt - 1
                assert schedule.call_args.args[1] == 1
                schedule.call_args.args[2](datetime.now(UTC))
                await hass.async_block_till_done()
                assert attempts == attempt
            assert not fan_platform.entities
            assert schedule.call_count == 2

            await _report(coordinator, 4, version=2, timestamp=200)
            await hass.async_block_till_done()
            assert attempts == 3

            for callback, cameras_only in tuple(coordinator._device_listeners):
                if not cameras_only:
                    callback(coordinator.devices)
            await hass.async_block_till_done()
            assert attempts == 4
            assert len(fan_platform.entities) == 1
            coordinator.async_publish_shadow_update.assert_not_awaited()


async def test_e25_native_remove_failure_retries_latest_selection_and_keeps_registry(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (_, fan_platform, light_platform):
        await _report(coordinator, 4)
        await hass.async_block_till_done()
        old = next(iter(fan_platform.entities.values()))
        old_id = old.entity_id
        registry = er.async_get(hass)
        registry.async_update_entity(old_id, name="My Fan", hidden_by=er.RegistryEntryHider.USER)
        original_remove = old.async_remove
        attempts = 0

        async def flaky_remove() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("native remove failed")
            await original_remove()

        with (
            patch.object(old, "async_remove", side_effect=flaky_remove),
            patch("custom_components.vivosun_growhub.discovery.async_call_later") as schedule,
        ):
            await _report(coordinator, 5, version=2, timestamp=200)
            await hass.async_block_till_done()
            assert attempts == 1
            assert fan_platform.entities[old_id] is old
            with pytest.raises(ServiceValidationError):
                await old.async_turn_on(percentage=50)

            await _report(coordinator, 5, version=3, timestamp=300)
            await hass.async_block_till_done()
            assert attempts == 1
            schedule.call_args.args[2](datetime.now(UTC))
            await hass.async_block_till_done()
            assert attempts == 2
            assert len(fan_platform.entities) == 1
            assert isinstance(next(iter(fan_platform.entities.values())), VivosunDuctFanEntity)
            assert not light_platform.entities
            assert old_id not in fan_platform.entities
            assert registry.async_get(old_id).name == "My Fan"
            assert registry.async_get(old_id).hidden_by == er.RegistryEntryHider.USER
            assert hass.states.get(old_id).attributes["restored"] is True
            coordinator.async_publish_selected_fan_level.assert_not_awaited()


async def test_e25_post_add_failure_and_reversal_do_not_duplicate_native_objects(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (_, fan_platform, light_platform):
        original_add = fan_platform.async_add_entities
        attempts = 0

        async def add_then_fail(entities: list[object]) -> None:
            nonlocal attempts
            attempts += 1
            await original_add(cast("list", entities))
            if attempts == 1:
                raise RuntimeError("post-add failure")

        with (
            patch.object(fan_platform, "async_add_entities", side_effect=add_then_fail),
            patch("custom_components.vivosun_growhub.discovery.async_call_later") as schedule,
        ):
            await _report(coordinator, 4)
            await hass.async_block_till_done()
            assert len(fan_platform.entities) == 1
            assert attempts == 1 and schedule.call_count == 1
            cancelled = schedule.return_value

            await _report(coordinator, 5, version=2, timestamp=200)
            await hass.async_block_till_done()
            assert cancelled.called
            assert attempts == 2
            assert len(fan_platform.entities) == 1
            assert isinstance(next(iter(fan_platform.entities.values())), VivosunDuctFanEntity)
            assert not light_platform.entities
            assert schedule.call_count == 1

            # A callback already queued at cancellation must not revive old selection.
            schedule.call_args.args[2](datetime.now(UTC))
            await hass.async_block_till_done()
            assert len(fan_platform.entities) == 1
            assert attempts == 2


async def test_e25_unload_cancels_failed_reconciliation_retry(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    attempts = 0
    with patch("custom_components.vivosun_growhub.discovery.async_call_later") as schedule:
        async with _platforms(hass, coordinator) as (_, fan_platform, _):

            async def failing_add(_entities: list[object]) -> None:
                nonlocal attempts
                attempts += 1
                raise RuntimeError("native add failed")

            with patch.object(fan_platform, "async_add_entities", side_effect=failing_add):
                await _report(coordinator, 4)
                await hass.async_block_till_done()
                assert attempts == 1 and schedule.call_count == 1

        assert schedule.return_value.called
        schedule.call_args.args[2](datetime.now(UTC))
        await hass.async_block_till_done()
        assert attempts == 1
        assert not coordinator._device_listeners


async def test_e25_sparse_invalid_stale_and_disconnected_reports_preserve_platform(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    async with _platforms(hass, coordinator) as (_, fan_platform, light_platform):
        await _report(coordinator, 2, version=2, timestamp=200)
        await hass.async_block_till_done()
        fan_before = next(iter(fan_platform.entities.values()))
        light_before = next(iter(light_platform.entities.values()))
        await _report(coordinator, 5, version=1, timestamp=100)
        await _report(coordinator, 5, desired=True, version=3, timestamp=300)
        await _report(coordinator, include_selection=False, version=4, timestamp=400)
        await _report(coordinator, "5", version=5, timestamp=500)
        await _report(coordinator, 3, version=6, timestamp=600)
        coordinator._mqtt_client.is_connected = False
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        assert next(iter(fan_platform.entities.values())) is fan_before
        assert next(iter(light_platform.entities.values())) is light_before
        assert len(fan_platform.entities) == len(light_platform.entities) == 1
        assert not fan_before.available and not light_before.available
        assert hass.states.get(fan_before.entity_id).attributes.get("restored") is not True
        assert hass.states.get(light_before.entity_id).attributes.get("restored") is not True


async def test_e25_selection_does_not_withdraw_e42a_platform_entities(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, devices=[DEVICE, E42A])
    async with _platforms(hass, coordinator) as (_, fan_platform, light_platform):
        e42_fans = {entity.unique_id: entity for entity in fan_platform.entities.values()}
        e42_lights = {entity.unique_id: entity for entity in light_platform.entities.values()}
        assert len(e42_fans) == 2 and len(e42_lights) == 1
        assert all("e42-one" in uid for uid in (*e42_fans, *e42_lights))

        await _report(coordinator, 2)
        await hass.async_block_till_done()
        await _report(coordinator, 5, version=2, timestamp=200)
        await hass.async_block_till_done()
        for entity in (*e42_fans.values(), *e42_lights.values()):
            platform = light_platform if isinstance(entity, VivosunLightEntity) else fan_platform
            assert platform.entities[entity.entity_id] is entity


async def test_e25_reload_recreates_only_selected_native_controls(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="E25", data={})
    entry.add_to_hass(hass)
    async with _platforms(hass, coordinator, entry=entry) as (_, fan_platform, light_platform):
        await _report(coordinator, 2)
        await hass.async_block_till_done()
        assert len(fan_platform.entities) == len(light_platform.entities) == 1
        cfan_id = next(iter(fan_platform.entities))
    assert not coordinator._device_listeners

    await _report(coordinator, 5, version=2, timestamp=200)
    async with _platforms(hass, coordinator, entry=entry) as (_, fan_platform, light_platform):
        assert len(fan_platform.entities) == 1
        assert isinstance(next(iter(fan_platform.entities.values())), VivosunDuctFanEntity)
        assert cfan_id not in fan_platform.entities
        assert not light_platform.entities

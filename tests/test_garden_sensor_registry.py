"""Garden schedule presentation through native Home Assistant entity registry."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub import retire_controller_plan_entities
from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.garden_topology import GardenTopology
from custom_components.vivosun_growhub.gardens import (
    Garden,
    GardenInventory,
    GardenMember,
    GardenPlanRead,
    GardenPlans,
    parse_garden_plans,
)
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.sensor import async_setup_entry

if TYPE_CHECKING:
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


def _recipe(scene_id: int, name: str, start_time: int) -> GardenPlans:
    """Parsed synthetic response with every template family, not attachment evidence."""
    content = {key: {"slot": [{"time": 360, "level": 50}, {"time": 1080, "level": 0}]}
               for key in ("light", "cfan", "dfan", "hmdf", "dhmdf", "drip", "heat", "aircd")}
    plan_id = scene_id + 100
    return parse_garden_plans({
        "scene": {"sceneId": scene_id},
        "iotUserPlanList": [{
            "sceneId": scene_id, "userPlanId": plan_id, "userRecipeId": 2000 + scene_id,
            "name": name, "needSync": True, "iotDeviceList": [],
            "userPlanStageList": [{
                "userPlanStageId": 3000 + scene_id, "userPlanId": plan_id,
                "stageFlag": "stage1", "stageName": "Vegetative", "icon": "",
                "sort": 1, "startTime": start_time, "timeOffset": 0,
                "contentUpdate": 1, "planMode": 1, "planStageContent": json.dumps(content),
            }],
        }],
    }, scene_id=scene_id)


def _inventory(*, moved: bool = False, renamed: bool = False) -> GardenInventory:
    return GardenInventory((
        Garden(10, 600, "First", "Europe/Berlin", (
            GardenMember("controller-a", None), *(() if moved else (GardenMember("controller-b", None),)),
        )),
        Garden(20, 600, "Second renamed" if renamed else "Second", "America/New_York", (
            *((GardenMember("controller-b", None),) if moved else ()),
        )),
        Garden(30, 600, "Empty", "Europe/Berlin", ()),
    ))


def _device(device_id: str, scene_id: int) -> DeviceInfo:
    return DeviceInfo(device_id, f"vivosun-VSCTLE42A-test-{device_id}", f"topic/{device_id}",
                      device_id, True, scene_id, "controller")


async def test_real_ha_registry_garden_ownership_dynamic_schedules_and_reload(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Account", data={})
    entry.add_to_hass(hass)
    coordinator = VivosunCoordinator(hass, cast("object", None), email="synthetic@example.invalid", password="test")
    devices = [_device("controller-a", 10), _device("controller-b", 10)]
    coordinator._devices = devices
    coordinator._all_devices = devices
    coordinator._garden_inventory = _inventory()
    coordinator._garden_reads = {
        10: GardenPlanRead(_recipe(10, "Autoflower", 1), "current"),
        20: GardenPlanRead(_recipe(20, "Photoperiod", 0), "current"),
        30: GardenPlanRead(GardenPlans(30, ()), "current"),
    }
    coordinator._shadow_states = {"controller-a": {"plan": {"active_stage": "not-garden-stage"}}}
    coordinator.data = coordinator._build_state_snapshot()
    topology = GardenTopology(hass, entry.entry_id)
    coordinator.set_garden_topology(topology)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id, coordinator=coordinator,
    )
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    platform.config_entry = entry

    def add_entities(entities: list[SensorEntity]) -> None:
        hass.async_create_task(platform.async_add_entities(entities))

    try:
        await async_setup_entry(hass, entry, add_entities)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        devices_registry = dr.async_get(hass)

        def garden_entity(scene_id: int, role: str) -> str | None:
            return registry.async_get_entity_id(
                "sensor", DOMAIN, f"vivosun_growhub_{entry.entry_id}_garden_{scene_id}_{role}",
            )

        assert all(garden_entity(scene_id, role) is not None
                   for scene_id in (10, 20, 30) for role in ("recipe", "stage"))
        assert all(garden_entity(scene_id, role) is not None for scene_id in (10, 20)
                   for role in ("plan_light", "plan_cfan", "plan_dfan", "plan_hmdf",
                                "plan_dhmdf", "plan_drip", "plan_heat", "plan_aircd"))
        assert garden_entity(30, "plan_light") is None

        g10 = devices_registry.async_get_device(identifiers={(DOMAIN, topology.identifier(10))})
        assert g10 is not None and g10.entry_type == dr.DeviceEntryType.SERVICE
        recipe_id = garden_entity(10, "recipe")
        assert recipe_id is not None
        recipe = registry.async_get(recipe_id)
        assert recipe is not None and recipe.device_id == g10.id
        assert hass.states.get(recipe_id).state == "Autoflower"
        assert hass.states.get(garden_entity(10, "stage")).state == "Vegetative"
        assert hass.states.get(garden_entity(20, "stage")).state == "Inactive"
        assert hass.states.get(garden_entity(30, "recipe")).state == "No recipe"

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, "vivosun_growhub_controller-b_plan_stage",
        ) is None
        devices_registry.async_update_device(g10.id, name_by_user="Tent A", area_id="living_room")

        # One member moves in the app; its physical entity ID stays unchanged.
        moved = [_device("controller-a", 10), _device("controller-b", 20)]
        coordinator._all_devices = moved
        coordinator._devices = moved
        coordinator._garden_inventory = _inventory(moved=True, renamed=True)
        topology.apply(coordinator.garden_inventory, moved)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        assert garden_entity(20, "plan_cfan") is not None
        assert hass.states.get(garden_entity(10, "recipe")).state == "Autoflower"
        assert hass.states.get(garden_entity(20, "recipe")).state == "Photoperiod"
        moved_parent = devices_registry.async_get_device(identifiers={(DOMAIN, topology.identifier(20))})
        physical = devices_registry.async_get_device(identifiers={(DOMAIN, "controller-b")})
        assert moved_parent is not None and physical is not None and physical.via_device_id == moved_parent.id
        assert devices_registry.async_get(g10.id).name_by_user == "Tent A"
    finally:
        await entry._async_process_on_unload(hass)
        await platform.async_reset()
    assert coordinator._garden_listeners == []
    # Native registry identities persist across the platform's unload.
    assert er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"vivosun_growhub_{entry.entry_id}_garden_10_recipe",
    ) is not None


async def test_retirement_uses_single_entry_ownership_without_deprecated_access(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="First", data={})
    other = MockConfigEntry(domain=DOMAIN, title="Second", data={})
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    owned = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "owned")})
    unrelated = devices.async_get_or_create(config_entry_id=other.entry_id, identifiers={(DOMAIN, "other")})
    retired = registry.async_get_or_create(
        "sensor", DOMAIN, "vivosun_growhub_owned_plan_stage", config_entry=entry, device_id=owned.id,
    )
    outside = registry.async_get_or_create(
        "sensor", DOMAIN, "vivosun_growhub_other_plan_stage", config_entry=other, device_id=unrelated.id,
    )

    def deprecated_access(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Deprecated device registry API accessed")

    monkeypatch.setattr(dr.DeviceRegistry, "async_get_device", deprecated_access)
    monkeypatch.setattr(dr.DeviceEntry, "config_entries", property(deprecated_access))
    retire_controller_plan_entities(hass, entry)
    assert registry.async_get(retired.entity_id).disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert registry.async_get(outside.entity_id) == outside


async def test_legacy_registry_retirement_is_scoped_idempotent_and_preserves_metadata(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Account", data={})
    other = MockConfigEntry(domain=DOMAIN, title="Other", data={})
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    first = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "ctrl-a")})
    second = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "ctrl-b")})
    unrelated = devices.async_get_or_create(config_entry_id=other.entry_id, identifiers={(DOMAIN, "ctrl-x")})
    roles = ("plan_stage", "plan_light", "plan_cfan", "plan_dfan", "plan_hmdf",
             "plan_dhmdf", "plan_drip", "plan_heat", "plan_aircd")
    historical = []
    for device, identifier in ((first, "ctrl-a"), (second, "ctrl-b")):
        for role in roles:
            row = registry.async_get_or_create("sensor", DOMAIN, f"vivosun_growhub_{identifier}_{role}",
                                               config_entry=entry, device_id=device.id)
            historical.append(row.entity_id)
    customized = historical[0]
    registry.async_update_entity(customized, name="My stage", area_id="living_room",
                                 hidden_by=er.RegistryEntryHider.USER,
                                 disabled_by=er.RegistryEntryDisabler.USER)
    outside = registry.async_get_or_create("sensor", DOMAIN, "vivosun_growhub_ctrl-x_plan_stage",
                                           config_entry=other, device_id=unrelated.id)
    physical = registry.async_get_or_create("sensor", DOMAIN, "vivosun_growhub_ctrl-a_inTemp",
                                            config_entry=entry, device_id=first.id)
    misleading = registry.async_get_or_create("sensor", DOMAIN, "vivosun_growhub_ctrl-a_plan_light_extra",
                                               config_entry=entry, device_id=first.id)
    no_device = registry.async_get_or_create("sensor", DOMAIN, "vivosun_growhub_unknown_plan_stage",
                                               config_entry=entry)

    retire_controller_plan_entities(hass, entry)
    retire_controller_plan_entities(hass, entry)
    for entity_id in historical:
        row = registry.async_get(entity_id)
        assert row is not None
        assert row.disabled_by in (er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER)
    row = registry.async_get(customized)
    assert row is not None
    assert (row.name, row.area_id, row.hidden_by, row.disabled_by) == (
        "My stage", "living_room", er.RegistryEntryHider.USER, er.RegistryEntryDisabler.USER)
    for original in (outside, physical, misleading, no_device):
        assert registry.async_get(original.entity_id) == original


async def test_upgrade_reload_keeps_historical_rows_and_physical_identity(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Upgrade", data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    physical = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "controller-a")})
    old = registry.async_get_or_create(
        "sensor", DOMAIN, "vivosun_growhub_controller-a_plan_stage", config_entry=entry, device_id=physical.id,
    )
    registry.async_update_entity(old.entity_id, name="Previous stage", hidden_by=er.RegistryEntryHider.USER)
    measurement = registry.async_get_or_create(
        "sensor", DOMAIN, "vivosun_growhub_controller-a_inTemp", config_entry=entry, device_id=physical.id,
    )
    registry.async_update_entity(measurement.entity_id, name="Tent temperature")
    coordinator = VivosunCoordinator(hass, cast("object", None), email="synthetic@example.invalid", password="test")
    coordinator._devices = [_device("controller-a", 10)]
    coordinator._all_devices = coordinator.devices
    coordinator._garden_inventory = _inventory()
    plans = _recipe(10, "Autoflower", 0)
    coordinator._garden_reads = {
        10: GardenPlanRead(plans, "current"),
        20: GardenPlanRead(_recipe(20, "Photoperiod", 0), "current"),
        30: GardenPlanRead(GardenPlans(30, ()), "current"),
    }
    coordinator.data = coordinator._build_state_snapshot()
    coordinator.set_garden_topology(GardenTopology(hass, entry.entry_id))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry_id=entry.entry_id, coordinator=coordinator)
    previous_ids: dict[str, str] = {}

    for _ in range(2):
        retire_controller_plan_entities(hass, entry)
        platform = EntityPlatform(
            hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
            platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
        )
        platform.config_entry = entry

        def add_entities(entities: list[SensorEntity], current_platform: EntityPlatform = platform) -> None:
            hass.async_create_task(current_platform.async_add_entities(entities))

        try:
            await async_setup_entry(hass, entry, add_entities)
            await hass.async_block_till_done()
            historical = registry.async_get(old.entity_id)
            assert historical is not None
            assert (historical.unique_id, historical.device_id, historical.name, historical.hidden_by) == (
                old.unique_id, physical.id, "Previous stage", er.RegistryEntryHider.USER,
            )
            assert historical.disabled_by == er.RegistryEntryDisabler.INTEGRATION
            old_state = hass.states.get(old.entity_id)
            assert old_state is None or old_state.state == "unavailable"
            current_measurement = registry.async_get(measurement.entity_id)
            assert current_measurement is not None
            assert (current_measurement.unique_id, current_measurement.device_id, current_measurement.name,
                    current_measurement.disabled_by) == (
                measurement.unique_id, physical.id, "Tent temperature", None,
            )
            ids = {
                row.unique_id: row.entity_id
                for row in er.async_entries_for_config_entry(registry, entry.entry_id)
                if "_garden_" in row.unique_id
            }
            assert len(ids) == 22  # Two recipes with eight schedules each, plus an empty garden.
            if previous_ids:
                assert ids == previous_ids
            previous_ids = ids
            light_id = ids[f"vivosun_growhub_{entry.entry_id}_garden_10_plan_light"]
            assert hass.states.get(light_id).state == "Inactive"
            coordinator._garden_reads[10] = GardenPlanRead(plans, "stale")
            coordinator._notify_shadow_changed()
            await hass.async_block_till_done()
            assert hass.states.get(light_id).state == "unavailable"
            coordinator._garden_reads[10] = GardenPlanRead(plans, "current")
            coordinator._notify_shadow_changed()
            await hass.async_block_till_done()
            assert hass.states.get(light_id).state == "Inactive"
        finally:
            await entry._async_process_on_unload(hass)
            await platform.async_reset()
    assert coordinator._garden_listeners == []

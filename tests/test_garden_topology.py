"""Native Home Assistant garden and physical-device topology contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.garden_topology import GardenTopology, garden_membership
from custom_components.vivosun_growhub.gardens import Garden, GardenInventory, GardenMember
from custom_components.vivosun_growhub.models import DeviceInfo

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


def _garden(scene_id: int, name: str, *member_ids: str) -> Garden:
    return Garden(
        scene_id, 600, name, "Europe/Berlin", tuple(GardenMember(device_id, None) for device_id in member_ids)
    )


def _device(device_id: str, scene_id: int) -> DeviceInfo:
    return DeviceInfo(device_id, f"thing-{device_id}", f"topic/{device_id}", device_id, True, scene_id,
                      device_type="controller")


@pytest.mark.parametrize("kind", ["vseed", "curing_box"])
def test_garden_membership_accepts_devices_without_scene_metadata(kind: str) -> None:
    device = DeviceInfo("standalone", "thing", "topic", "Standalone", True, 0, kind)
    assert garden_membership(GardenInventory((_garden(10, "Garden", "standalone"),)), [device]) == {"standalone": 10}


async def test_native_three_gardens_moves_and_customizations(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Account", data={})
    entry.add_to_hass(hass)
    topology = GardenTopology(hass, entry.entry_id)
    registry = dr.async_get(hass)
    entities = er.async_get(hass)
    initial = GardenInventory((_garden(10, "My Garden", "e42a"), _garden(20, "Test", "e25"),
                               _garden(30, "Empty Garden")))

    topology.apply(initial, [_device("e42a", 10), _device("e25", 20)])

    parents = [
        registry.async_get_device_by_identifier((DOMAIN, topology.identifier(i)), entry.entry_id)
        for i in (10, 20, 30)
    ]
    assert all(parent is not None and parent.entry_type == dr.DeviceEntryType.SERVICE for parent in parents)
    p10, p20, p30 = parents
    assert p30 is not None and p30.via_device_id is None
    e25 = registry.async_get_device_by_identifier((DOMAIN, "e25"), entry.entry_id)
    assert e25 is not None and p20 is not None and e25.via_device_id == p20.id
    entity = entities.async_get_or_create("sensor", DOMAIN, "vivosun_growhub_e25_inTemp",
                                          config_entry=entry, device_id=e25.id)
    entities.async_update_entity(entity.entity_id, name="Keep sensor", disabled_by=er.RegistryEntryDisabler.USER)
    original_entity_id = entity.entity_id
    original_unique_id = entity.unique_id
    original_device_id = entity.device_id
    registry.async_update_device(e25.id, name_by_user="My Controller", area_id="living_room", labels={"my-label"})
    registry.async_update_device(p20.id, name_by_user="My Tent", area_id="living_room")

    topology.apply(GardenInventory((_garden(10, "My Garden", "e42a", "e25"),
                                    _garden(20, "Renamed Test"), _garden(30, "Empty Garden"))),
                   [_device("e42a", 10), _device("e25", 10)])

    moved = registry.async_get(e25.id)
    assert moved is not None and p10 is not None
    assert moved.id == e25.id and moved.via_device_id == p10.id
    assert (moved.name_by_user, moved.area_id, moved.labels) == ("My Controller", "living_room", {"my-label"})
    updated_parent = registry.async_get(p20.id)
    assert updated_parent is not None
    assert (updated_parent.name, updated_parent.name_by_user, updated_parent.area_id) == (
        "Renamed Test", "My Tent", "living_room"
    )
    retained_entity = entities.async_get(original_entity_id)
    assert retained_entity is not None
    assert (retained_entity.entity_id, retained_entity.unique_id, retained_entity.device_id) == (
        original_entity_id, original_unique_id, original_device_id,
    )
    assert retained_entity.disabled_by == er.RegistryEntryDisabler.USER
    assert retained_entity.name == "Keep sensor"

    topology.apply(GardenInventory((_garden(10, "My Garden", "e42a"),
                                    _garden(20, "Renamed Test"), _garden(30, "Empty Garden"))),
                   [_device("e42a", 10), _device("e25", 0)])
    assert registry.async_get(e25.id).via_device_id is None
    topology.apply(GardenInventory(()), [])
    assert registry.async_get(p10.id) is None
    assert registry.async_get(p20.id) is None
    assert registry.async_get(p30.id) is None
    assert registry.async_get(e25.id) is not None
    assert registry.async_get(e25.id).via_device_id is None


async def test_removed_persisted_gardens_remove_owned_rows_and_unlink_absent_physical(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Account", data={})
    unrelated_entry = MockConfigEntry(domain=DOMAIN, title="Unrelated", data={})
    entry.add_to_hass(hass)
    unrelated_entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    entities = er.async_get(hass)
    topology = GardenTopology(hass, entry.entry_id)
    other_topology = GardenTopology(hass, unrelated_entry.entry_id)

    # This is a persisted-only garden. The new coordinator has never seen it.
    deleted = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, topology.identifier(20))},
        entry_type=dr.DeviceEntryType.SERVICE, name="Deleted garden", model="Garden",
    )
    deleted_second = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, topology.identifier(30))},
        entry_type=dr.DeviceEntryType.SERVICE, name="Another deleted garden", model="Garden",
    )
    retained = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "temporarily-absent")},
        via_device_id=deleted.id,
    )
    physical_row = entities.async_get_or_create(
        "sensor", DOMAIN, "vivosun_growhub_temporarily-absent_temperature",
        config_entry=entry, device_id=retained.id,
    )
    entities.async_update_entity(physical_row.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    old_row = entities.async_get_or_create(
        "sensor", DOMAIN, f"vivosun_growhub_{entry.entry_id}_garden_20_stage",
        config_entry=entry, device_id=deleted.id,
    )
    old_second_row = entities.async_get_or_create(
        "sensor", DOMAIN, f"vivosun_growhub_{entry.entry_id}_garden_30_stage",
        config_entry=entry, device_id=deleted_second.id,
    )
    other_row = entities.async_get_or_create(
        "sensor", DOMAIN, f"vivosun_growhub_{unrelated_entry.entry_id}_garden_20_stage",
        config_entry=unrelated_entry,
    )
    other_topology.apply(GardenInventory((_garden(20, "Same ID, other account"),)), [])
    other_parent = registry.async_get_device_by_identifier(
        (DOMAIN, other_topology.identifier(20)), unrelated_entry.entry_id,
    )

    valid = GardenInventory((_garden(10, "Surviving garden"),))
    topology.apply(valid, [])
    topology.apply(valid, [])
    assert registry.async_get(deleted.id) is None
    assert registry.async_get(deleted_second.id) is None
    assert entities.async_get(old_row.entity_id) is None
    assert entities.async_get(old_second_row.entity_id) is None
    assert registry.async_get(retained.id).via_device_id is None
    assert entities.async_get(physical_row.entity_id).disabled_by == er.RegistryEntryDisabler.USER
    assert entities.async_get(other_row.entity_id) == other_row
    assert registry.async_get(other_parent.id) == other_parent

    # A returning garden is registered without adopting the other account's device.
    topology.apply(GardenInventory((_garden(10, "Surviving garden"), _garden(20, "Returned"))), [])
    restored = registry.async_get_device_by_identifier((DOMAIN, topology.identifier(20)), entry.entry_id)
    assert restored is not None and restored.id != other_parent.id


async def test_garden_parent_ids_are_entry_scoped(hass: HomeAssistant) -> None:
    first = MockConfigEntry(domain=DOMAIN, title="One", data={})
    second = MockConfigEntry(domain=DOMAIN, title="Two", data={})
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    inventory = GardenInventory((_garden(10, "Same Name"),))
    GardenTopology(hass, first.entry_id).apply(inventory, [])
    GardenTopology(hass, second.entry_id).apply(inventory, [])
    registry = dr.async_get(hass)
    a = registry.async_get_device(identifiers={(DOMAIN, GardenTopology(hass, first.entry_id).identifier(10))})
    b = registry.async_get_device(identifiers={(DOMAIN, GardenTopology(hass, second.entry_id).identifier(10))})
    assert a is not None and b is not None and a.id != b.id


async def test_topology_uses_entry_scoped_lookup_with_shared_identifier(
    hass: HomeAssistant, monkeypatch: MonkeyPatch,
) -> None:
    first = MockConfigEntry(domain=DOMAIN, title="First", data={})
    second = MockConfigEntry(domain=DOMAIN, title="Second", data={})
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    registry = dr.async_get(hass)
    owned = registry.async_get_or_create(config_entry_id=first.entry_id, identifiers={(DOMAIN, "shared")})
    unrelated = registry.async_get_or_create(config_entry_id=second.entry_id, identifiers={(DOMAIN, "shared")})
    assert owned.id != unrelated.id
    registry.async_update_device(unrelated.id, name_by_user="Other account", area_id="living_room")
    before = registry.async_get(unrelated.id)

    def deprecated_access(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Deprecated device registry API accessed")

    monkeypatch.setattr(dr.DeviceRegistry, "async_get_device", deprecated_access)
    monkeypatch.setattr(dr.DeviceEntry, "config_entries", property(deprecated_access))
    topology = GardenTopology(hass, first.entry_id)
    topology.apply(GardenInventory((_garden(10, "First garden", "shared"),)), [_device("shared", 10)])
    parent = registry.async_get_device_by_identifier((DOMAIN, topology.identifier(10)), first.entry_id)
    assert parent is not None
    assert registry.async_get(owned.id).via_device_id == parent.id
    assert registry.async_get(unrelated.id) == before
    topology.apply(GardenInventory((_garden(10, "First garden"),)), [_device("shared", 0)])
    assert registry.async_get(owned.id).via_device_id is None
    assert registry.async_get(unrelated.id) == before

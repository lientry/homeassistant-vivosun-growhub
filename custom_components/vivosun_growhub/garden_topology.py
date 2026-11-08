"""Native Home Assistant registry links for account-scoped garden service devices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .exceptions import VivosunResponseError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .gardens import GardenInventory
    from .models import DeviceInfo


def garden_membership(inventory: GardenInventory, devices: list[DeviceInfo]) -> dict[str, int]:
    """Validate independently read garden/device joins before mutating any registry row."""
    members = {member.device_id: garden.scene_id
               for garden in inventory.gardens for member in garden.members}
    member_clients = {member.device_id: member.client_id
                      for garden in inventory.gardens for member in garden.members}
    known: set[str] = set()
    routes: set[str] = set()
    for device in devices:
        if device.device_id in known or (device.client_id and device.client_id in routes):
            raise VivosunResponseError("Conflicting physical inventory identity")
        known.add(device.device_id)
        if device.client_id:
            routes.add(device.client_id)
        scene_id = members.get(device.device_id, 0)
        if scene_id != device.scene_id:
            raise VivosunResponseError("Garden and device inventories disagree on membership")
        expected_client = member_clients.get(device.device_id)
        if expected_client and device.client_id and expected_client != device.client_id:
            raise VivosunResponseError("Garden and device inventories disagree on routing")
    return members


class GardenTopology:
    """Apply validated cloud garden membership without altering user registry fields."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._registry = dr.async_get(hass)
        self._entities = er.async_get(hass)
        self._entry_id = entry_id

    def identifier(self, scene_id: int) -> str:
        """Use stable account/config-entry identity rather than a garden name."""
        return f"garden:{self._entry_id}:{scene_id}"

    def apply(self, inventory: GardenInventory, devices: list[DeviceInfo]) -> None:
        """Register all parents first, then update only confirmed physical links."""
        membership = garden_membership(inventory, devices)
        parents: dict[int, str] = {}
        for garden in inventory.gardens:
            parent = self._registry.async_get_or_create(
                config_entry_id=self._entry_id,
                identifiers={(DOMAIN, self.identifier(garden.scene_id))},
                entry_type=dr.DeviceEntryType.SERVICE,
                name=garden.name,
                manufacturer="VIVOSUN",
                model="Garden",
            )
            parents[garden.scene_id] = parent.id

        for device in devices:
            existing = self._registry.async_get_device_by_identifier((DOMAIN, device.device_id), self._entry_id)
            via_device_id = parents.get(membership[device.device_id]) if device.device_id in membership else None
            if existing is None:
                self._registry.async_get_or_create(
                    config_entry_id=self._entry_id,
                    identifiers={(DOMAIN, device.device_id)},
                    name=device.name,
                    manufacturer="VIVOSUN",
                    via_device_id=via_device_id,
                )
            elif existing.via_device_id != via_device_id:
                self._registry.async_update_device(existing.id, via_device_id=via_device_id)

        # Unlink retained physical children before removing confirmed-deleted gardens.
        current_parents = set(parents.values())
        device_entries = dr.async_entries_for_config_entry(self._registry, self._entry_id)
        retired = [
            row for row in device_entries
            if row.entry_type == dr.DeviceEntryType.SERVICE
            and any(domain == DOMAIN and identifier.startswith(f"garden:{self._entry_id}:")
                    for domain, identifier in row.identifiers)
            and row.id not in current_parents
        ]
        retired_ids = {row.id for row in retired}
        for row in device_entries:
            if row.via_device_id in retired_ids:
                self._registry.async_update_device(row.id, via_device_id=None)
        if retired_ids:
            for entity in er.async_entries_for_config_entry(self._entities, self._entry_id):
                if entity.device_id in retired_ids and entity.platform == DOMAIN:
                    self._entities.async_remove(entity.entity_id)
        for parent in retired:
            self._registry.async_remove_device(parent.id)

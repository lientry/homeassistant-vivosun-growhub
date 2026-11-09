"""Idempotent per-platform entity additions from account discovery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import VivosunCoordinator
    from .gardens import Garden, GardenInventory
    from .models import DeviceInfo

_LOGGER = logging.getLogger(__name__)


def watch_devices(
    entry: ConfigEntry,
    coordinator: VivosunCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[DeviceInfo], Sequence[Entity]],
    *,
    cameras: bool = False,
) -> None:
    """Add only new identities, keeping HA's existing entities and listeners intact."""
    seen: set[str] = set()

    def added(devices: list[DeviceInfo]) -> None:
        for device in devices:
            if device.device_id in seen:
                continue
            try:
                candidates = list(factory(device))
                if not candidates:
                    continue
                async_add_entities(candidates)
            except Exception:
                _LOGGER.exception("Device entity discovery failed; will retry on next inventory")
                continue
            seen.add(device.device_id)

    entry.async_on_unload(coordinator.async_add_discovery_listener(added, cameras=cameras))


def watch_gardens(
    entry: ConfigEntry,
    coordinator: VivosunCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[Garden], Sequence[Entity]],
) -> None:
    """Add garden-owned entities once, including schedules enabled by later evidence."""
    seen: set[str] = set()

    def added(inventory: GardenInventory) -> None:
        active = {f"vivosun_growhub_{entry.entry_id}_garden_{g.scene_id}_" for g in inventory.gardens}
        seen.intersection_update({uid for uid in seen if any(uid.startswith(prefix) for prefix in active)})
        for garden in inventory.gardens:
            try:
                candidates: dict[str, Entity] = {}
                for entity in factory(garden):
                    unique_id = entity.unique_id
                    if unique_id is not None and unique_id not in seen:
                        candidates.setdefault(unique_id, entity)
                if candidates:
                    async_add_entities(list(candidates.values()))
                    seen.update(candidates)
            except Exception:
                _LOGGER.exception("Garden sensor discovery failed; will retry")

    entry.async_on_unload(coordinator.async_add_garden_listener(added))

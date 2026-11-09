"""Per-platform entity discovery and selected E25 control lifecycle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from .models import client_model_token

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddEntitiesCallback, EntityPlatform

    from .coordinator import VivosunCoordinator
    from .gardens import Garden, GardenInventory
    from .models import DeviceInfo

_LOGGER = logging.getLogger(__name__)
_E25_RETRY_DELAY = 1  # Seconds between at most two automatic retries per failed device.
_E25_RETRY_LIMIT = 2


def watch_devices(
    entry: ConfigEntry,
    coordinator: VivosunCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[DeviceInfo], Sequence[Entity]],
    *,
    cameras: bool = False,
    incremental: bool = False,
    control_platform: EntityPlatform | None = None,
) -> None:
    """Add discoveries, retiring only confirmed deselected E25 platform controls."""
    seen: set[str] = set()
    identities: set[str] = set()
    active_e25: dict[str, dict[str, Entity]] = {}
    pending_e25: dict[str, DeviceInfo] = {}
    retry_e25: dict[str, DeviceInfo] = {}
    retry_counts: dict[str, int] = {}
    retry_cancel: Callable[[], None] | None = None
    worker: asyncio.Task[None] | None = None
    unloaded = False

    async def reconcile_e25() -> None:
        """Serialize native adds/removals so a selection reversal cannot reuse an old entity."""
        assert control_platform is not None
        while pending_e25 and not unloaded:
            device_id, device = pending_e25.popitem()
            try:
                candidates = {entity.unique_id: entity for entity in factory(device)
                              if entity.unique_id is not None}
                active = active_e25.setdefault(device_id, {})
                for unique_id, entity in tuple(active.items()):
                    if entity.entity_id is None or control_platform.entities.get(entity.entity_id) is not entity:
                        del active[unique_id]
                    elif unique_id not in candidates:
                        await entity.async_remove()
                        del active[unique_id]
                for unique_id, entity in candidates.items():
                    if unloaded or device_id in pending_e25:
                        break
                    if unique_id in active:
                        continue
                    try:
                        await control_platform.async_add_entities([entity])
                    finally:
                        # The native add may have registered the entity before raising.
                        if entity.entity_id is not None and control_platform.entities.get(entity.entity_id) is entity:
                            active[unique_id] = entity
            except Exception:
                if unloaded or device_id in pending_e25:
                    _LOGGER.exception("E25 control platform reconciliation failed; newer selection pending")
                    continue
                count = retry_counts.get(device_id, 0)
                if count >= _E25_RETRY_LIMIT:
                    retry_counts.pop(device_id, None)
                    _LOGGER.exception("E25 control platform reconciliation failed; awaiting next inventory")
                    continue
                retry_counts[device_id] = count + 1
                retry_e25[device_id] = device
                _LOGGER.exception("E25 control platform reconciliation failed; scheduling retry %d/%d in %ds",
                                  count + 1, _E25_RETRY_LIMIT, _E25_RETRY_DELAY)
                schedule_retry()
            else:
                retry_counts.pop(device_id, None)

    def start_worker() -> None:
        nonlocal worker
        if pending_e25 and control_platform is not None and not unloaded and (worker is None or worker.done()):
            worker = control_platform.hass.async_create_task(reconcile_e25())

    @callback
    def retry(_now: datetime) -> None:
        """Retry failed native operations once per timer without replaying MQTT reports."""
        nonlocal retry_cancel
        retry_cancel = None
        if unloaded:
            return
        for device_id, device in retry_e25.items():
            pending_e25.setdefault(device_id, device)
        retry_e25.clear()
        start_worker()

    def schedule_retry() -> None:
        nonlocal retry_cancel
        if control_platform is not None and retry_cancel is None and not unloaded:
            retry_cancel = async_call_later(control_platform.hass, _E25_RETRY_DELAY, retry)

    def stop() -> None:
        nonlocal retry_cancel, unloaded
        unloaded = True
        pending_e25.clear()
        retry_e25.clear()
        retry_counts.clear()
        if retry_cancel is not None:
            retry_cancel()
            retry_cancel = None
        if worker is not None and not worker.done():
            worker.cancel()

    def added(devices: list[DeviceInfo]) -> None:
        nonlocal retry_cancel
        if unloaded:
            return
        for device in devices:
            if control_platform is not None and client_model_token(device.client_id) == "VSCTL002":
                retry_e25.pop(device.device_id, None)
                retry_counts.pop(device.device_id, None)
                pending_e25[device.device_id] = device
                continue
            if not incremental and device.device_id in seen:
                continue
            try:
                candidates = list(factory(device))
                if incremental:
                    candidates = [entity for entity in candidates
                                  if entity.unique_id is not None and entity.unique_id not in identities]
                if not candidates:
                    continue
                async_add_entities(candidates)
            except Exception:
                _LOGGER.exception("Device entity discovery failed; will retry on next inventory")
                continue
            seen.add(device.device_id)
            identities.update(entity.unique_id for entity in candidates if entity.unique_id is not None)
        if not retry_e25 and retry_cancel is not None:
            retry_cancel()
            retry_cancel = None
        start_worker()

    entry.async_on_unload(coordinator.async_add_discovery_listener(added, cameras=cameras))
    if control_platform is not None:
        entry.async_on_unload(stop)


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

"""Incremental entity handoff through Home Assistant's real entity platform."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.discovery import watch_devices, watch_gardens
from custom_components.vivosun_growhub.gardens import Garden, GardenInventory
from custom_components.vivosun_growhub.models import DeviceInfo

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant


def _device(device_id: str) -> DeviceInfo:
    return DeviceInfo(device_id, device_id, device_id, device_id, True, 0, "controller")


class _Coordinator:
    """Typed discovery contract with explicit unload visibility."""

    def __init__(self) -> None:
        self.devices: list[DeviceInfo] = []
        self.camera_devices: list[DeviceInfo] = []
        self._listeners: list[Callable[[list[DeviceInfo]], None]] = []

    def async_add_discovery_listener(
        self, callback: Callable[[list[DeviceInfo]], None], *, cameras: bool = False,
    ) -> Callable[[], None]:
        callback(self.camera_devices if cameras else self.devices)
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def notify(self, *devices: DeviceInfo) -> None:
        for listener in tuple(self._listeners):
            listener(list(devices))


class _DiscoveredSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Reading"
    _attr_native_value = 12

    def __init__(self, device_id: str) -> None:
        self._attr_name = f"{device_id} Reading"
        self._attr_unique_id = f"garden-test-{device_id}"


class _GardenCoordinator:
    def __init__(self) -> None:
        self.listeners: list[Callable[[GardenInventory], None]] = []

    def async_add_garden_listener(
        self, callback: Callable[[GardenInventory], None],
    ) -> Callable[[], None]:
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def notify(self, *gardens: Garden) -> None:
        for callback in tuple(self.listeners):
            callback(GardenInventory(gardens))


async def test_factory_and_callback_failures_recover_without_poisoning_native_registry(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Account", data={})
    entry.add_to_hass(hass)
    coordinator = _Coordinator()
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(minutes=5),
        entity_namespace=None,
    )
    platform.config_entry = entry
    tasks = []
    calls: list[str] = []
    factory_failed = True
    callback_failed = True

    def factory(device: DeviceInfo) -> list[SensorEntity]:
        nonlocal factory_failed
        calls.append(device.device_id)
        if device.device_id == "b" and factory_failed:
            factory_failed = False
            raise ValueError("temporary entity construction error")
        return [_DiscoveredSensor(device.device_id)]

    def add_entities(entities: list[SensorEntity]) -> None:
        nonlocal callback_failed
        if entities[0].unique_id == "garden-test-c" and callback_failed:
            callback_failed = False
            raise RuntimeError("transient platform handoff failure")
        tasks.append(hass.async_create_task(platform.async_add_entities(entities)))

    watch_devices(entry, coordinator, add_entities, factory)
    try:
        coordinator.notify(_device("a"), _device("b"), _device("c"))
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        assert registry.async_get_entity_id("sensor", DOMAIN, "garden-test-a") is not None
        assert registry.async_get_entity_id("sensor", DOMAIN, "garden-test-b") is None
        assert registry.async_get_entity_id("sensor", DOMAIN, "garden-test-c") is None

        coordinator.notify(_device("a"), _device("b"), _device("c"))
        await hass.async_block_till_done()
        assert {er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"garden-test-{id_}")
                for id_ in ("a", "b", "c")} == {
            "sensor.a_reading", "sensor.b_reading", "sensor.c_reading"
        }
        coordinator.notify(_device("a"), _device("b"), _device("c"))
        await hass.async_block_till_done()
        assert len(tasks) == 3
        assert calls == ["a", "b", "c", "b", "c"]
    finally:
        await entry._async_process_on_unload(hass)
        await platform.async_reset()
    assert not coordinator._listeners
    coordinator.notify(_device("d"))
    assert registry.async_get_entity_id("sensor", DOMAIN, "garden-test-d") is None


async def test_garden_additions_batch_and_retry_without_losing_dynamic_sources(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Account", data={})
    entry.add_to_hass(hass)
    coordinator = _GardenCoordinator()
    garden = Garden(10, 600, "Garden", "UTC", ())
    include_micro = False
    handoffs: list[list[str]] = []

    def factory(garden: Garden) -> list[SensorEntity]:
        roles = ["stage", "recipe", "recipe"]
        if include_micro:
            roles.append("micro_inside")
        entities = []
        for role in roles:
            sensor = _DiscoveredSensor(role)
            sensor._attr_unique_id = f"vivosun_growhub_{entry.entry_id}_garden_{garden.scene_id}_{role}"
            entities.append(sensor)
        return entities

    def add_entities(entities: list[SensorEntity]) -> None:
        handoffs.append([entity.unique_id for entity in entities])
        if len(handoffs) == 1:
            raise RuntimeError("transient platform handoff failure")

    watch_gardens(entry, coordinator, add_entities, factory)
    prefix = f"vivosun_growhub_{entry.entry_id}_garden_10_"
    try:
        coordinator.notify(garden)
        coordinator.notify(garden)
        coordinator.notify(garden)
        assert handoffs == [
            [prefix + "stage", prefix + "recipe"],
            [prefix + "stage", prefix + "recipe"],
        ]
        include_micro = True
        coordinator.notify(garden)
        assert handoffs[-1] == [prefix + "micro_inside"]
        coordinator.notify()
        coordinator.notify(garden)
        assert handoffs[-1] == [prefix + "stage", prefix + "recipe", prefix + "micro_inside"]
    finally:
        await entry._async_process_on_unload(hass)
    assert not coordinator.listeners

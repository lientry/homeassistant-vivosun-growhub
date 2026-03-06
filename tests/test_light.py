"""Tests for Vivosun light platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from homeassistant.components.light import ATTR_BRIGHTNESS
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.light import VivosunLightEntity, async_setup_entry
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.shadow import build_light_level_payload, build_light_spectrum_payload

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


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


def _make_light(coordinator: _StubCoordinator) -> VivosunLightEntity:
    return VivosunLightEntity(cast("object", coordinator))


async def test_light_setup_creates_one_entity(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunLightEntity] = []

    def _add(entities: list[VivosunLightEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 1
    assert added[0].unique_id == "vivosun_growhub_dev-1_light"


async def test_light_state_mapping_and_availability() -> None:
    coordinator = _StubCoordinator()
    coordinator.data = {
        "shadow": {
            "light": {"level": 50, "mode": 0, "spectrum": 20},
            "connection": {"connected": True},
        }
    }
    entity = _make_light(coordinator)

    assert entity.is_on is True
    assert entity.brightness == 128
    assert entity.extra_state_attributes == {"mode": 0, "spectrum": 20}
    assert entity.available is True
    assert entity.device_info["model"] == "VSCTLE42A"

    coordinator.is_mqtt_connected = False
    assert entity.available is False


async def test_light_turn_on_and_off_publishes_shadow_payloads() -> None:
    coordinator = _StubCoordinator()
    entity = _make_light(coordinator)

    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 255})
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_light_level_payload(100))

    coordinator.async_publish_shadow_update.reset_mock()
    coordinator.data = {"shadow": {"light": {"level": 0}}}
    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_light_level_payload(25))

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 0, "spectrum": 40})
    assert coordinator.async_publish_shadow_update.await_count == 2
    calls = coordinator.async_publish_shadow_update.await_args_list
    assert calls[0].args[0] == build_light_level_payload(0)
    assert calls[1].args[0] == build_light_spectrum_payload(40)

    coordinator.async_publish_shadow_update.reset_mock()
    await entity.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_light_level_payload(0))


async def test_light_turn_on_clamps_nonzero_levels_to_device_minimum() -> None:
    coordinator = _StubCoordinator()
    entity = _make_light(coordinator)

    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 10})
    coordinator.async_publish_shadow_update.assert_awaited_once_with(build_light_level_payload(25))


async def test_light_brightness_boundaries() -> None:
    coordinator = _StubCoordinator()
    entity = _make_light(coordinator)

    coordinator.data = {"shadow": {"light": {"level": 0}}}
    assert entity.brightness == 0

    coordinator.data = {"shadow": {"light": {"level": 1}}}
    assert entity.brightness == 3

    coordinator.data = {"shadow": {"light": {"level": 50}}}
    assert entity.brightness == 128

    coordinator.data = {"shadow": {"light": {"level": 100}}}
    assert entity.brightness == 255

"""Tests for Vivosun switch platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.switch import VivosunControlSwitch, async_setup_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_DEV_ID = "vcure-1"


class _StubCoordinator:
    def __init__(self) -> None:
        self.data: dict[str, object] = {
            "shadows": {
                _DEV_ID: {
                    "reported_supported": {
                        "ctlGlass": 1,
                        "ctlLight": 0,
                        "ctlLock": "1",
                    }
                }
            }
        }
        self._device = DeviceInfo(
            device_id=_DEV_ID,
            client_id="vivosun-VSCBC80-acc-dev-1",
            topic_prefix="prefix",
            name="VCure C80",
            online=True,
            scene_id=0,
            device_type="curing_box",
            supports_point_log=False,
        )
        self.is_mqtt_connected = True
        self.async_publish_shadow_update = AsyncMock()

    @property
    def device(self) -> DeviceInfo:
        return self._device

    @property
    def devices(self) -> list[DeviceInfo]:
        return [self._device]

    def get_device(self, device_id: str) -> DeviceInfo | None:
        return self._device if device_id == _DEV_ID else None


async def test_switch_setup_creates_curing_box_controls(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )
    added: list[VivosunControlSwitch] = []

    def _add(entities: list[VivosunControlSwitch]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert {entity.unique_id for entity in added} == {
        f"vivosun_growhub_{_DEV_ID}_ctlGlass",
        f"vivosun_growhub_{_DEV_ID}_ctlLight",
        f"vivosun_growhub_{_DEV_ID}_ctlLock",
    }
    assert {entity.name for entity in added} == {
        "Privacy Glass",
        "Interior Light",
        "Door Lock",
    }


async def test_switch_state_and_commands(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )
    added: list[VivosunControlSwitch] = []

    def _add(entities: list[VivosunControlSwitch]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    by_unique_id = {entity.unique_id: entity for entity in added}
    glass = by_unique_id[f"vivosun_growhub_{_DEV_ID}_ctlGlass"]
    light = by_unique_id[f"vivosun_growhub_{_DEV_ID}_ctlLight"]

    assert glass.is_on is True
    assert light.is_on is False

    await light.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"ctlLight": 1}}},
        device_id=_DEV_ID,
        qos=1,
    )
    coordinator.async_publish_shadow_update.reset_mock()

    await glass.async_turn_off()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"ctlGlass": 0}}},
        device_id=_DEV_ID,
        qos=1,
    )

"""Tests for Vivosun select platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.select import (
    OPTION_CURE_ONLY,
    OPTION_STOPPED,
    VivosunCureModeSelect,
    async_setup_entry,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch

_DEV_ID = "vcure-1"


class _StubCoordinator:
    def __init__(self) -> None:
        self.data: dict[str, object] = {
            "shadows": {
                _DEV_ID: {
                    "reported_supported": {
                        "cure": {"inPlan": 1},
                        "plan": {"stage1": {"contId": "234195+1756947324", "startT": 12345}},
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


async def test_select_setup_and_current_option(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )
    added: list[VivosunCureModeSelect] = []

    def _add(entities: list[VivosunCureModeSelect]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert len(added) == 1
    assert added[0].name == "Mode"
    assert added[0].current_option == OPTION_CURE_ONLY
    assert added[0].extra_state_attributes == {
        "cont_id": "234195+1756947324",
        "start_time": 12345,
    }


async def test_select_stopped_when_not_in_plan(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator.data["shadows"][_DEV_ID]["reported_supported"]["cure"] = {"inPlan": 0}  # type: ignore[index]
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )
    added: list[VivosunCureModeSelect] = []

    def _add(entities: list[VivosunCureModeSelect]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    assert added[0].current_option == OPTION_STOPPED


async def test_select_commands_publish_stop_then_preset(hass: HomeAssistant, monkeypatch: MonkeyPatch) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=cast("object", coordinator),
    )
    monkeypatch.setattr("custom_components.vivosun_growhub.select.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("custom_components.vivosun_growhub.select.time.time", lambda: 98765)
    added: list[VivosunCureModeSelect] = []

    def _add(entities: list[VivosunCureModeSelect]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    await added[0].async_select_option(OPTION_CURE_ONLY)

    assert coordinator.async_publish_shadow_update.await_args_list[0].kwargs == {
        "device_id": _DEV_ID,
        "qos": 1,
    }
    assert coordinator.async_publish_shadow_update.await_args_list[0].args == (
        {"state": {"desired": {"plan": {"stage1": {"startT": 0}}}}},
    )
    assert coordinator.async_publish_shadow_update.await_args_list[1].args == (
        {
            "state": {
                "desired": {
                    "plan": {
                        "stage1": {
                            "startT": 98765,
                            "contId": "234195+1756947324",
                        }
                    }
                }
            }
        },
    )

"""Tests for Vivosun select platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from discovery_stubs import DiscoveryCoordinatorStub
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.select import (
    OPTION_CURE_ONLY,
    OPTION_STOPPED,
    VivosunCureModeSelect,
    VivosunSpectrumSelect,
    async_setup_entry,
)


async def test_spectrum_requires_opt_in_and_reported_manual_state(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    controller = DeviceInfo(
        "controller-1", "vivosun-VSCTLE42A-account-1", "topic-a", "AeroLight", True, 1, "controller"
    )
    spider = DeviceInfo(
        "controller-2", "vivosun-VSCTLE42A-account-2", "topic-b", "Spider Farmer", True, 2, "controller"
    )
    coordinator._device = controller
    coordinator._devices = [controller, spider]
    coordinator.data["shadows"]["controller-1"] = {"confirmed_spectrum": {"spec": 50, "mode": 0, "inPlan": 0}}
    coordinator.data["shadows"]["controller-2"] = {"confirmed_spectrum": {"spec": 50, "mode": 0, "inPlan": 0}}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"spectrum_controllers": ["controller-1"]})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    spectra = [entity for entity in added if isinstance(entity, VivosunSpectrumSelect)]
    assert len(spectra) == 1
    spectrum = spectra[0]
    assert spectrum.unique_id == "vivosun_growhub_controller-1_spectrum"
    assert spectrum.current_option == "Vegetative"
    await spectrum.async_select_option("Bloom")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"light": {"manu": {"spec": 100}}}}},
        device_id="controller-1", qos=1,
    )
    coordinator.data["shadows"]["controller-1"]["confirmed_spectrum"]["inPlan"] = 1
    try:
        await spectrum.async_select_option("Seedling")
    except ServiceValidationError:
        pass
    else:
        raise AssertionError("active plan must reject spectrum writes")
    assert coordinator.async_publish_shadow_update.await_count == 1


async def test_spectrum_default_off_for_vgrow_and_external_controllers(hass: HomeAssistant) -> None:
    coordinator = _StubCoordinator()
    coordinator._devices = [
        DeviceInfo("vgrow", "vivosun-VSCTLY18-account", "topic-1", "VGrow", True, 1, "controller"),
        DeviceInfo("spider", "vivosun-VSCTLE42A-account", "topic-2", "Spider Farmer", True, 2, "controller"),
    ]
    for device in coordinator._devices:
        coordinator.data["shadows"][device.device_id] = {
            "confirmed_spectrum": {"spec": 50, "mode": 0, "inPlan": 0}
        }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    assert not any(isinstance(entity, VivosunSpectrumSelect) for entity in added)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch

_DEV_ID = "vcure-1"


class _StubCoordinator(DiscoveryCoordinatorStub):
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
        return getattr(self, "_devices", [self._device])

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

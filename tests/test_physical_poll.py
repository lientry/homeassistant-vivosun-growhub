"""Physical telemetry ordering regressions independent of garden projections."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.exceptions import VivosunResponseError
from custom_components.vivosun_growhub.models import AuthTokens, DeviceInfo

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


CONTROLLER = DeviceInfo(
    "controller-1", "synthetic-VSCTLE42A-1", "controller/topic", "Controller", True, 10, "controller",
)
SEED = DeviceInfo("seed-1", "synthetic-VSSBS40-1", "seed/topic", "Seed", True, 0, "vseed")


def coordinator_for(hass: HomeAssistant, device: DeviceInfo) -> VivosunCoordinator:
    coordinator = VivosunCoordinator(hass, cast("object", None), email="test", password="test")
    coordinator._tokens = AuthTokens("a", "b", "c", "d")
    coordinator._all_devices = [device]
    coordinator._devices = [device]
    return coordinator


@pytest.mark.parametrize("device,key", ((CONTROLLER, "inTemp"), (SEED, "rTemp")), ids=("controller", "vseed"))
async def test_late_physical_poll_does_not_overwrite_newer_values(
    hass: HomeAssistant, device: DeviceInfo, key: str,
) -> None:
    coordinator = coordinator_for(hass, device)
    started, resume = asyncio.Event(), asyncio.Event()
    count = 0

    async def poll(*_args: object, **_kwargs: object) -> dict[str, int]:
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            await resume.wait()
            return {key: 1800}
        return {key: 1948}

    coordinator._api.get_point_log = AsyncMock(side_effect=poll)
    first = asyncio.create_task(coordinator._refresh_point_log())
    await started.wait()
    await coordinator._refresh_point_log()
    resume.set()
    await first
    assert coordinator._sensor_states[device.device_id] == (
        {key: 1948} if device.device_type != "vseed" else {
            "rTemp": 1948, "pTemp": None, "pHumi": None, "pVpd": None, "rssi": None,
        }
    )
    await coordinator.async_shutdown()


@pytest.mark.parametrize("device,key", ((CONTROLLER, "inTemp"), (SEED, "rTemp")), ids=("controller", "vseed"))
@pytest.mark.parametrize("change", ("removed", "moved", "rerouted", "client", "generation"))
async def test_retired_physical_route_cannot_publish_old_poll(
    hass: HomeAssistant, device: DeviceInfo, key: str, change: str,
) -> None:
    coordinator = coordinator_for(hass, device)
    coordinator._sensor_states[device.device_id] = {key: 2200}
    started, resume = asyncio.Event(), asyncio.Event()

    async def poll(*_args: object, **_kwargs: object) -> dict[str, int]:
        started.set()
        await resume.wait()
        return {key: 1800}

    coordinator._api.get_point_log = AsyncMock(side_effect=poll)
    first = asyncio.create_task(coordinator._refresh_point_log())
    await started.wait()
    if change == "removed":
        coordinator._devices = []
        coordinator._all_devices = []
    elif change == "generation":
        coordinator._inventory_generation += 1
    else:
        replacement = {
            "moved": replace(device, scene_id=device.scene_id + 1),
            "rerouted": replace(device, topic_prefix="other/topic"),
            "client": replace(device, client_id="other-client"),
        }[change]
        coordinator._devices = [replacement]
        coordinator._all_devices = [replacement]
    resume.set()
    await first
    assert coordinator._sensor_states[device.device_id] == {key: 2200}
    await coordinator.async_shutdown()


@pytest.mark.parametrize("failure", ("invalid", "unreachable", "cancelled"))
async def test_older_failed_physical_poll_does_not_erase_newer_success(
    hass: HomeAssistant, failure: str,
) -> None:
    coordinator = coordinator_for(hass, CONTROLLER)
    started, resume = asyncio.Event(), asyncio.Event()
    count = 0

    async def poll(*_args: object, **_kwargs: object) -> dict[str, int]:
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            await resume.wait()
            if failure == "invalid":
                raise VivosunResponseError("Invalid older point log")
            if failure == "cancelled":
                raise asyncio.CancelledError
            raise TimeoutError("Old point log unavailable")
        return {"inTemp": 1948}

    coordinator._api.get_point_log = AsyncMock(side_effect=poll)
    first = asyncio.create_task(coordinator._refresh_point_log())
    await started.wait()
    await coordinator._refresh_point_log()
    resume.set()
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        await first
    assert coordinator._sensor_states[CONTROLLER.device_id] == {"inTemp": 1948}
    await coordinator.async_shutdown()

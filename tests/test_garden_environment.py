"""Garden projections: captured literals and explicitly synthetic failure cases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from custom_components.vivosun_growhub.api import PointLog, VivosunApiClient
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.exceptions import VivosunResponseError
from custom_components.vivosun_growhub.garden_environment import Sample, parse_sample, parse_selection
from custom_components.vivosun_growhub.gardens import Garden, GardenInventory, GardenMember
from custom_components.vivosun_growhub.models import AuthTokens, DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.sensor import VivosunGardenEnvironmentSensor, async_setup_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

CLIENT = "synthetic-VSCTLE42A-1"
GARDEN = Garden(10, 1, "Garden", "UTC", (GardenMember("device-1", CLIENT),))
DEVICE = DeviceInfo("device-1", CLIENT, "topic", "Controller", True, 10, "controller")
SEED = DeviceInfo("seed-1", "synthetic-VSSBS40-1", "seed/topic", "Seed", True, 0, "vseed")


def card(tag: str, channel: str, code: str, *, keys: tuple[str, ...] = ("inTemp", "inHumi", "inVpd")) -> dict:
    """Aliased shape from the selected panel, not the candidate defaults."""
    return {
        "dataTag": tag, "group": "additional_env" if tag == "other_humiture" else "main_env",
        "dataGroupType": "CLIMATE", "extraId": "device-1", "clientId": CLIENT,
        "extraMasterId": "", "dataGroupGid": channel, "code": code, "dataGroupName": channel,
        "deviceDataList": [{"dataType": kind, "dataKey": key} for kind, key in zip(
            ("TEMP", "HUMI", "VPD"), keys, strict=True
        )],
    }


def panel(*cards: dict) -> dict:
    return {"deviceCardGroupList": [{"deviceCardList": list(cards)}]}


def row(timestamp: int) -> dict:
    """Captured garden values with a synthetic timestamp."""
    return {"time": timestamp, "inTemp": 1948, "inHumi": 5363, "inVpd": 104,
            "outTemp": 1943, "outHumi": 5417, "outVpd": 103}


def test_captured_same_source_main_micro_and_values() -> None:
    inside = card("in_humiture", "channel-1", "code-1")
    micro = card("other_humiture", "channel-1", "code-1")
    outside = card("out_humiture", "channel-2", "code-2", keys=("outTemp", "outHumi", "outVpd"))
    selection = parse_selection(panel(inside, outside, micro), GARDEN, [DEVICE], 1000)
    assert selection.inside is not None and selection.outside is not None
    assert selection.micro == (selection.inside,)
    assert selection.inside.identity != selection.outside.identity
    assert "device-1" not in selection.inside.identity
    sample = parse_sample({"iotDataLogList": [row(990)]})
    assert sample is not None
    assert [sample.value(key, 1000) for key in (
        "inTemp", "inHumi", "inVpd", "outTemp", "outHumi", "outVpd"
    )] == [19.48, 53.63, 1.04, 19.43, 54.17, 1.03]


def test_empty_and_synthetic_ambiguous_unsupported_cards() -> None:
    empty = card("in_humiture", "", "")
    empty.update(extraId="", clientId="", deviceDataList=[])
    selection = parse_selection(panel(empty), GARDEN, [DEVICE], 1000)
    assert selection.inside is None and not selection.invalid
    duplicate = card("other_humiture", "channel-1", "code-1")
    result = parse_selection(panel(duplicate, dict(duplicate)), GARDEN, [DEVICE], 1000)
    assert result.micro == () and result.invalid == {"micro"}
    second = card("other_humiture", "channel-2", "code-2", keys=("outTemp", "outHumi", "outVpd"))
    distinct = parse_selection(panel(duplicate, second), GARDEN, [DEVICE], 1000)
    assert len(distinct.micro) == 2 and distinct.micro[0].identity != distinct.micro[1].identity
    conflict = card("other_humiture", "channel-1", "different-code")
    assert parse_selection(panel(duplicate, conflict), GARDEN, [DEVICE], 1000).invalid == {"micro"}
    wrong = card("in_humiture", "channel-1", "code-1")
    wrong["clientId"] = "synthetic-VSPLUGA22-1"
    assert parse_selection(panel(wrong), GARDEN, [DEVICE], 1000).invalid == {"inside"}
    with pytest.raises(VivosunResponseError):
        parse_selection({"deviceCardGroupList": None}, GARDEN, [DEVICE], 1000)


def test_synthetic_metric_validity_freshness_and_ordering() -> None:
    latest = row(1000)
    latest.update(inTemp=-124, inHumi=0, inVpd=-6666, outTemp=True, outHumi="10", outVpd=None)
    sample = parse_sample({"iotDataLogList": [latest, row(900)]})
    assert sample is not None
    assert [sample.value(key, 1000) for key in (
        "inTemp", "inHumi", "inVpd", "outTemp", "outHumi", "outVpd"
    )] == [-1.24, 0, None, None, None, None]
    assert sample.value("inTemp", 1300) == -1.24
    assert sample.value("inTemp", 1301) is None
    assert sample.value("inTemp", 940) == -1.24
    assert sample.value("inTemp", 939) is None
    assert sample.value("inTemp", 1000, after=1000) is None
    with pytest.raises(VivosunResponseError):
        parse_sample({"iotDataLogList": [row(1000), row(1000)]})
    with pytest.raises(VivosunResponseError):
        parse_sample({"iotDataLogList": [{"time": True}]})


@pytest.mark.parametrize("model", ["VSCTL001", "VSCTLFUTURE"])
def test_climate_contract_not_model_token(model: str) -> None:
    client = f"synthetic-{model}-1"
    device = replace(DEVICE, client_id=client)
    garden = replace(GARDEN, members=(GardenMember("device-1", client),))
    selected = card("other_humiture", "channel-1", "code-1")
    selected["clientId"] = client
    selected["deviceDataList"] = [
        {"dataType": kind, "dataKey": key}
        for kind, key in (("HUMI", "pHumi"), ("VPD", "pVpd"), ("TEMP", "pTemp"))
    ]
    result = parse_selection(panel(selected), garden, [device], 1000)
    assert not result.invalid
    assert len(result.micro) == 1
    assert result.micro[0].keys == ("pTemp", "pHumi", "pVpd")


@pytest.mark.parametrize("keys,values,expected", [
    (("inTemp", "inHumi", "inVpd"), (1948, 5363, 104), (19.48, 53.63, 1.04)),
    (("outTemp", "outHumi", "outVpd"), (1943, 5417, 103), (19.43, 54.17, 1.03)),
    (("pTemp", "pHumi", "pVpd"), (2050, 6025, 127), (20.5, 60.25, 1.27)),
    (("bTemp", "bHumi", "bVpd"), (2240, 0, -6666), (22.4, 0, None)),
])
def test_supported_physical_metric_families(
    keys: tuple[str, str, str], values: tuple[int, int, int], expected: tuple[float | None, ...],
) -> None:
    sample = parse_sample({"iotDataLogList": [{"time": 1000, **dict(zip(keys, values, strict=True))}]})
    assert sample is not None
    assert tuple(sample.value(key, 1000) for key in keys) == expected


def test_selection_rejects_ambiguous_descriptors_and_unconfirmed_membership() -> None:
    selected = card("other_humiture", "channel-1", "code-1", keys=("pTemp", "pHumi", "pVpd"))
    bad = dict(selected, deviceDataList=[
        {"dataType": "TEMP", "dataKey": "pTemp"},
        {"dataType": "HUMI", "dataKey": "pHumi"},
        {"dataType": "VPD", "dataKey": "unknownVpd"},
    ])
    assert parse_selection(panel(bad), GARDEN, [DEVICE], 1000).invalid == {"micro"}
    repeated = dict(selected, deviceDataList=[
        {"dataType": "TEMP", "dataKey": "pTemp"},
        {"dataType": "TEMP", "dataKey": "pTemp"},
        {"dataType": "VPD", "dataKey": "pVpd"},
    ])
    assert parse_selection(panel(repeated), GARDEN, [DEVICE], 1000).invalid == {"micro"}
    wrong_garden = replace(GARDEN, members=(GardenMember("device-1", "different-route"),))
    assert parse_selection(panel(selected), wrong_garden, [DEVICE], 1000).invalid == {"micro"}
    assert parse_selection(panel(selected), GARDEN, [DEVICE, DEVICE], 1000).invalid == {"micro"}


async def test_micro_uses_selected_physical_metric_without_scene_role_fallback(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    now = int(datetime.now(tz=UTC).timestamp())
    selected = card("other_humiture", "channel-probe", "code-probe", keys=("pTemp", "pHumi", "pVpd"))
    coordinator._environment_selections[10] = parse_selection(panel(selected), GARDEN, [DEVICE], now)
    role = next(iter(coordinator.garden_environment_sources(10)))
    coordinator._environment_samples[10] = Sample(now, {"inTemp": 1948, "inHumi": 5363, "inVpd": 104})
    coordinator._physical_samples["device-1"] = (CLIENT, Sample(now, {
        "inTemp": 1948, "pTemp": 2150, "pHumi": 6125, "pVpd": 135,
    }))
    assert [coordinator.garden_environment_value(10, role, metric) for metric in range(3)] == [21.5, 61.25, 1.35]
    coordinator._physical_samples["device-1"] = (CLIENT, Sample(now, {"pTemp": 2150}))
    assert [coordinator.garden_environment_value(10, role, metric) for metric in range(3)] == [21.5, None, None]
    coordinator._all_devices = [replace(DEVICE, supports_point_log=False)]
    coordinator._devices = list(coordinator._all_devices)
    assert coordinator.garden_environment_value(10, role, 0) is None
    assert coordinator.garden_environment_status(10, role, 0)["sample_status"] == "unsupported"


def make_coordinator(hass: HomeAssistant) -> VivosunCoordinator:
    coordinator = VivosunCoordinator(hass, cast("object", None), email="test", password="test")
    coordinator._tokens = AuthTokens("a", "b", "c", "d")
    coordinator._garden_inventory = GardenInventory((GARDEN,))
    coordinator._all_devices = [DEVICE]
    coordinator._devices = [DEVICE]
    return coordinator


async def test_synthetic_replacement_fresh_micro_and_stable_roles(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    now = int(datetime.now(tz=UTC).timestamp())
    sample = cast("Sample", parse_sample({"iotDataLogList": [row(now - 1)]}))
    coordinator._api.get_garden_panel = AsyncMock(return_value=panel(
        card("in_humiture", "channel-1", "code-1"), card("other_humiture", "channel-1", "code-1")
    ))
    coordinator._api.get_garden_sample = AsyncMock(return_value=sample)
    coordinator._physical_samples["device-1"] = (CLIENT, sample)
    await coordinator._read_environment(10)
    sources = coordinator.garden_environment_sources(10)
    micro = next(role for role in sources if role.startswith("micro_"))
    assert coordinator.garden_environment_value(10, "inside", 0) == 19.48
    assert coordinator.garden_environment_value(10, micro, 2) == 1.04
    coordinator._api.get_garden_panel = AsyncMock(return_value=panel(
        card("in_humiture", "channel-2", "code-2"), card("other_humiture", "channel-1", "code-1")
    ))
    await coordinator._read_environment(10)
    assert coordinator.garden_environment_value(10, "inside", 0) is None
    assert coordinator.garden_environment_status(10, "inside", 0)["sample_status"] == "awaiting_sample"
    assert coordinator.garden_environment_value(10, micro, 0) == 19.48
    coordinator._environment_samples[10] = Sample(now + 1, sample.values)
    assert coordinator.garden_environment_value(10, "inside", 0) == 19.48
    assert len(coordinator._api.get_garden_panel.call_args_list) == 1
    await coordinator.async_shutdown()


async def test_synthetic_unmapped_stale_and_unchanged_recovery(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    now = datetime.now(tz=UTC).timestamp()
    selected = card("in_humiture", "channel-1", "code-1")
    sample = Sample(int(now), {"inTemp": 1948})
    coordinator._api.get_garden_panel = AsyncMock(return_value=panel(selected))
    coordinator._api.get_garden_sample = AsyncMock(return_value=sample)
    await coordinator._read_environment(10)
    assert coordinator.garden_environment_value(10, "inside", 0) == 19.48
    coordinator._environment_selections[10] = replace(coordinator._environment_selections[10], observed=now - 241)
    assert coordinator.garden_environment_value(10, "inside", 0) is None
    assert coordinator.garden_environment_status(10, "inside", 0)["selection_status"] == "stale"
    await coordinator._read_environment(10)
    assert coordinator.garden_environment_value(10, "inside", 0) == 19.48
    coordinator._api.get_garden_panel.return_value = panel()
    await coordinator._read_environment(10)
    assert coordinator.garden_environment_sources(10) == {}
    assert coordinator.garden_environment_value(10, "inside", 0) is None
    assert coordinator.garden_environment_status(10, "inside", 0)["selection_status"] == "unmapped"
    coordinator._api.get_garden_panel.return_value = panel(selected)
    await coordinator._read_environment(10)
    assert coordinator.garden_environment_value(10, "inside", 0) == 19.48
    coordinator._environment_samples[10] = Sample(int(now) - 301, {"inTemp": 1948})
    assert coordinator.garden_environment_value(10, "inside", 0) is None
    assert coordinator.garden_environment_status(10, "inside", 0)["sample_status"] == "stale"
    coordinator._all_devices = []
    assert coordinator.garden_environment_sources(10) == {}
    await coordinator.async_shutdown()


async def test_synthetic_read_errors_are_distinct_and_recover(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    sample = Sample(int(datetime.now(tz=UTC).timestamp()), {"inTemp": 1948, "inHumi": None})
    coordinator._api.get_garden_panel = AsyncMock(return_value=panel(card("in_humiture", "channel-1", "code-1")))
    coordinator._api.get_garden_sample = AsyncMock(return_value=sample)
    await coordinator._read_environment(10)
    assert coordinator.garden_environment_status(10, "inside", 1)["sample_status"] == "invalid"
    coordinator._api.get_garden_panel.side_effect = TimeoutError()
    coordinator._api.get_garden_sample.side_effect = TimeoutError()
    await coordinator._read_environment(10)
    assert coordinator.garden_environment_status(10, "inside", 0) == {
        "selection_status": "unreachable", "sample_status": "unreachable",
    }
    assert coordinator.garden_environment_value(10, "inside", 0) == 19.48
    coordinator._api.get_garden_panel.side_effect = None
    coordinator._api.get_garden_sample.side_effect = None
    await coordinator._read_environment(10)
    assert coordinator.garden_environment_status(10, "inside", 0) == {
        "selection_status": "current", "sample_status": "current",
    }
    await coordinator.async_shutdown()


async def test_shared_physical_poll_keeps_existing_snapshot(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    now = int(datetime.now(tz=UTC).timestamp())
    sample = Sample(now, {"inTemp": 1948, "inHumi": 5363, "inVpd": 104})
    coordinator._sensor_states["device-1"] = {"inHumi": 5363}
    coordinator._api.get_point_log = AsyncMock(return_value=PointLog({"inTemp": 1948}, sample))
    await coordinator._refresh_point_log()
    assert coordinator._api.get_point_log.await_count == 1
    assert coordinator._sensor_states["device-1"]["inTemp"] == 1948
    assert coordinator._sensor_states["device-1"]["inHumi"] == 5363
    assert coordinator._physical_samples["device-1"] == (CLIENT, sample)
    coordinator._api.get_point_log.return_value = PointLog({"inTemp": 1900}, Sample(now - 1, {"inTemp": 1900}))
    await coordinator._refresh_point_log()
    assert coordinator._physical_samples["device-1"] == (CLIENT, sample)
    assert coordinator._sensor_states["device-1"]["inTemp"] == 1900
    await coordinator.async_shutdown()


async def test_synthetic_out_of_order_physical_polls_keep_newer_generation(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    now = int(datetime.now(tz=UTC).timestamp())
    started = asyncio.Event()
    resume = asyncio.Event()
    count = 0

    async def poll(*_args: object, **_kwargs: object) -> PointLog:
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            await resume.wait()
            return PointLog({"inTemp": 1800}, Sample(now - 1, {"inTemp": 1800}))
        return PointLog({"inTemp": 1948}, Sample(now, {"inTemp": 1948}))

    coordinator._api.get_point_log = AsyncMock(side_effect=poll)
    first = asyncio.create_task(coordinator._refresh_point_log())
    await started.wait()
    await coordinator._refresh_point_log()
    resume.set()
    await first
    assert coordinator._physical_samples["device-1"][1] == Sample(now, {"inTemp": 1948})
    assert coordinator._physical_sample_status["device-1"] == "current"
    assert coordinator._sensor_states["device-1"] == {"inTemp": 1948}
    await coordinator.async_shutdown()


async def test_synthetic_out_of_order_vseed_poll_keeps_newer_values(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator._devices = [SEED]
    coordinator._all_devices = [SEED]
    started = asyncio.Event()
    resume = asyncio.Event()
    count = 0

    async def poll(*_args: object, **_kwargs: object) -> dict[str, int | None]:
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            await resume.wait()
            return {"rTemp": None, "pTemp": None, "pHumi": None, "pVpd": None, "rssi": None}
        return {"rTemp": 2500, "pTemp": 2400, "pHumi": 7000, "pVpd": 120, "rssi": -53}

    coordinator._api.get_point_log = AsyncMock(side_effect=poll)
    first = asyncio.create_task(coordinator._refresh_point_log())
    await started.wait()
    await coordinator._refresh_point_log()
    resume.set()
    await first
    assert coordinator._sensor_states["seed-1"] == {
        "rTemp": 2500, "pTemp": 2400, "pHumi": 7000, "pVpd": 120, "rssi": -53,
    }
    assert coordinator._physical_samples == {}
    await coordinator.async_shutdown()


@pytest.mark.parametrize("device", (DEVICE, SEED), ids=("controller", "vseed"))
@pytest.mark.parametrize("change", ("removed", "moved", "rerouted", "client", "generation"))
async def test_synthetic_physical_poll_rejects_retired_device_routes(
    hass: HomeAssistant, device: DeviceInfo, change: str,
) -> None:
    coordinator = make_coordinator(hass)
    coordinator._devices = [device]
    coordinator._all_devices = [device]
    key = "rTemp" if device.device_type == "vseed" else "inTemp"
    coordinator._sensor_states[device.device_id] = {key: 2200}
    started = asyncio.Event()
    resume = asyncio.Event()

    async def poll(*_args: object, **_kwargs: object) -> PointLog | dict[str, int | None]:
        started.set()
        await resume.wait()
        if device.device_type == "vseed":
            return {"rTemp": 1800}
        return PointLog({"inTemp": 1800}, Sample(1000, {"inTemp": 1800}))

    coordinator._api.get_point_log = AsyncMock(side_effect=poll)
    first = asyncio.create_task(coordinator._refresh_point_log())
    await started.wait()
    if change == "removed":
        coordinator._devices = []
        coordinator._all_devices = []
    elif change == "moved":
        moved = replace(device, scene_id=device.scene_id + 1)
        coordinator._devices = [moved]
        coordinator._all_devices = [moved]
    elif change == "rerouted":
        rerouted = replace(device, topic_prefix="new/topic")
        coordinator._devices = [rerouted]
        coordinator._all_devices = [rerouted]
    elif change == "client":
        rerouted = replace(device, client_id="new-client")
        coordinator._devices = [rerouted]
        coordinator._all_devices = [rerouted]
    else:
        coordinator._inventory_generation += 1
    resume.set()
    await first
    assert coordinator._sensor_states[device.device_id] == {key: 2200}
    assert coordinator._physical_samples == {}
    assert coordinator._physical_sample_status == {}
    await coordinator.async_shutdown()


@pytest.mark.parametrize("failure", ("invalid", "unreachable", "cancelled"))
async def test_synthetic_failed_older_physical_poll_cannot_erase_newer_success(
    hass: HomeAssistant, failure: str,
) -> None:
    coordinator = make_coordinator(hass)
    now = int(datetime.now(tz=UTC).timestamp())
    started = asyncio.Event()
    resume = asyncio.Event()
    count = 0

    async def poll(*_args: object, **_kwargs: object) -> PointLog:
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            await resume.wait()
            if failure == "invalid":
                raise VivosunResponseError("Invalid older point log")
            if failure == "cancelled":
                raise asyncio.CancelledError
            raise TimeoutError("Older point log unavailable")
        return PointLog({"inTemp": 1948}, Sample(now, {"inTemp": 1948}))

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
    assert coordinator._sensor_states["device-1"] == {"inTemp": 1948}
    assert coordinator._physical_samples["device-1"] == (CLIENT, Sample(now, {"inTemp": 1948}))
    assert coordinator._physical_sample_status["device-1"] == "current"
    await coordinator.async_shutdown()


async def test_garden_jobs_are_coalesced_and_limited(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator._garden_inventory = GardenInventory(tuple(
        Garden(scene, 1, "Garden", "UTC", ()) for scene in range(10, 14)
    ))
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def read_panel(*_args: object) -> dict:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            entered.set()
        await release.wait()
        active -= 1
        return panel()

    coordinator._api.get_garden_panel = AsyncMock(side_effect=read_panel)
    coordinator._api.get_garden_sample = AsyncMock(return_value=None)
    for scene_id in range(10, 14):
        coordinator._queue_environment_read(scene_id)
        coordinator._queue_environment_read(scene_id)
    await asyncio.wait_for(entered.wait(), 2)
    assert peak == 3 and coordinator._api.get_garden_panel.await_count == 3
    release.set()
    await asyncio.gather(*coordinator._environment_tasks.values())
    assert coordinator._api.get_garden_panel.await_count == 4
    for scene_id in range(10, 14):
        coordinator._queue_environment_read(scene_id)
    assert coordinator._api.get_garden_panel.await_count == 4
    await coordinator.async_shutdown()


async def test_read_methods_use_only_verified_read_bodies() -> None:
    client = VivosunApiClient(cast("object", None))
    client._request_json = AsyncMock(side_effect=[panel(), {"iotDataLogList": [row(1000)]}])
    tokens = AuthTokens("a", "b", "c", "d")
    assert await client.get_garden_panel(tokens, 10) == panel()
    sample = await client.get_garden_sample(tokens, 10, 1100)
    assert sample is not None and sample.value("inTemp", 1100) == 19.48
    first, second = client._request_json.await_args_list
    assert first.args == ("POST", "/iot/feature/getDataPanel")
    assert first.kwargs["json_body"] == {
        "kind": "feature_iot_home_data_panel", "code": "app_v2", "sceneId": 10,
    }
    assert second.args == ("POST", "/iot/data/getScenePointLog")
    assert second.kwargs["json_body"] == {
        "sceneId": 10, "startTime": 800, "endTime": 1100, "reportType": 0,
        "timeLevel": "ONE_MINUTE", "orderBy": "asc",
        "param": ["inTemp", "inHumi", "inVpd", "outTemp", "outHumi", "outVpd"],
    }


async def test_point_log_adds_timestamp_without_changing_physical_snapshot() -> None:
    client = VivosunApiClient(cast("object", None))
    client._request_json = AsyncMock(return_value={"iotDataLogList": [row(1000)]})
    result = await client.get_point_log(AuthTokens("a", "b", "c", "d"), DEVICE, start_time=900, end_time=1100)
    assert isinstance(result, PointLog)
    assert result["inTemp"] == 1948
    assert result.sample is not None and result.sample.time == 1000
    assert result.sample.value("inVpd", 1100) == 1.04


async def test_synthetic_removed_garden_rejects_inflight_result(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    started = asyncio.Event()
    resume = asyncio.Event()

    async def blocked(*_args: object) -> dict:
        started.set()
        await resume.wait()
        return panel(card("in_humiture", "channel-1", "code-1"))

    coordinator._api.get_garden_panel = AsyncMock(side_effect=blocked)
    coordinator._api.get_garden_sample = AsyncMock(return_value=None)
    task = asyncio.create_task(coordinator._read_environment(10))
    await started.wait()
    coordinator._garden_inventory = GardenInventory(())
    coordinator._inventory_generation += 1
    resume.set()
    await task
    assert coordinator.garden_environment_sources(10) == {}


def test_physical_point_log_retains_existing_dict_contract() -> None:
    sample = Sample(1000, {"inTemp": 1948})
    result = PointLog({"inTemp": 1948}, sample)
    assert result == {"inTemp": 1948}
    assert result.sample is sample


async def test_native_entity_keeps_ids_and_units(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    coordinator = make_coordinator(hass)
    entry = MockConfigEntry(domain="vivosun_growhub", entry_id="entry", data={})
    now = datetime.now(tz=UTC).timestamp()
    coordinator._environment_selections[10] = parse_selection(
        panel(card("in_humiture", "channel-1", "code-1")), GARDEN, [DEVICE], now
    )
    coordinator._environment_samples[10] = Sample(int(now), {"inTemp": 0})
    entity = VivosunGardenEnvironmentSensor(coordinator, entry, 10, "inside", 0)
    assert entity.unique_id == "vivosun_growhub_entry_garden_10_inside_temperature"
    assert entity.native_value == 0 and entity.available
    assert entity.native_unit_of_measurement == "°C"
    assert entity.state_class == "measurement"


@pytest.mark.parametrize("model,keys,channel", [
    ("VSCTL001", ("inTemp", "inHumi", "inVpd"), "inside"),
    ("VSCTLE42AP", ("pTemp", "pHumi", "pVpd"), "probe"),
    ("VSCTLFUTURE", ("bTemp", "bHumi", "bVpd"), "built_in"),
])
async def test_environment_diagnostics_follow_source_contract(
    hass: HomeAssistant, model: str, keys: tuple[str, str, str], channel: str,
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    client = f"synthetic-{model}-1"
    device = replace(DEVICE, client_id=client)
    garden = replace(GARDEN, members=(GardenMember("device-1", client),))
    coordinator = make_coordinator(hass)
    coordinator._garden_inventory = GardenInventory((garden,))
    coordinator._all_devices = [device]
    coordinator._devices = [device]
    selected = card("other_humiture", "channel-private", "code-private", keys=keys)
    selected["clientId"] = client
    now = int(datetime.now(tz=UTC).timestamp())
    coordinator._environment_selections[10] = parse_selection(panel(selected), garden, [device], now)
    role = next(iter(coordinator.garden_environment_sources(10)))
    entity = VivosunGardenEnvironmentSensor(
        coordinator, MockConfigEntry(domain="vivosun_growhub", entry_id="entry", data={}), 10, role, 0,
    )
    attributes = entity.extra_state_attributes
    assert attributes["selected_source_model"] == model
    assert attributes["selected_source_channel"] == channel
    assert "channel-private" not in str(attributes)
    assert "device-1" not in str(attributes)


async def test_registry_dynamic_mapping_and_retained_unmapped_entities(hass: HomeAssistant) -> None:
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_platform import EntityPlatform
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.vivosun_growhub.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = make_coordinator(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, coordinator)
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="sensor", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(minutes=5), entity_namespace=None,
    )
    platform.config_entry = entry

    def add_entities(entities: list) -> None:
        hass.async_create_task(platform.async_add_entities(entities))

    try:
        await async_setup_entry(hass, entry, add_entities)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        prefix = f"vivosun_growhub_{entry.entry_id}_garden_10_"
        assert registry.async_get_entity_id("sensor", DOMAIN, prefix + "inside_temperature") is None
        now = datetime.now(tz=UTC).timestamp()
        coordinator._environment_selections[10] = parse_selection(panel(
            card("in_humiture", "channel-1", "code-1"), card("other_humiture", "channel-1", "code-1"),
        ), GARDEN, [DEVICE], now)
        coordinator._environment_samples[10] = Sample(int(now), {"inTemp": 1948})
        coordinator._physical_samples["device-1"] = (CLIENT, Sample(int(now), {"inTemp": 1948}))
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        inside = registry.async_get_entity_id("sensor", DOMAIN, prefix + "inside_temperature")
        source = coordinator._environment_selections[10].micro[0]
        micro = registry.async_get_entity_id("sensor", DOMAIN, prefix + f"micro_{source.identity}_temperature")
        assert inside is not None and micro is not None and inside != micro
        assert hass.states.get(inside).state == "19.48"
        assert hass.states.get(micro).state == "19.48"
        registry.async_update_entity(micro, name="Old micro")
        coordinator._environment_selections[10] = parse_selection(panel(
            card("in_humiture", "channel-1", "code-1"), card("other_humiture", "channel-2", "code-2"),
        ), GARDEN, [DEVICE], now)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        replacement = coordinator._environment_selections[10].micro[0]
        new_micro = registry.async_get_entity_id("sensor", DOMAIN, prefix + f"micro_{replacement.identity}_temperature")
        assert new_micro is not None and new_micro != micro
        assert hass.states.get(new_micro).state == "19.48"
        assert registry.async_get(micro).name == "Old micro"
        assert hass.states.get(micro).state == "unavailable"
        coordinator._environment_selections[10] = replace(
            coordinator._environment_selections[10], observed=datetime.now(tz=UTC).timestamp() - 239.8,
        )
        coordinator._schedule_environment_expiry()
        await asyncio.sleep(0.35)
        await hass.async_block_till_done()
        assert hass.states.get(inside).state == "unavailable"
        assert hass.states.get(micro).state == "unavailable"
        coordinator._environment_selections[10] = parse_selection(panel(), GARDEN, [DEVICE], now)
        coordinator._notify_shadow_changed()
        await hass.async_block_till_done()
        assert registry.async_get(micro) is not None
        assert hass.states.get(micro).state == "unavailable"
        assert registry.async_get(new_micro) is not None
        assert hass.states.get(new_micro).state == "unavailable"
        assert registry.async_get(inside) is not None
        assert hass.states.get(inside).state == "unavailable"
    finally:
        if coordinator._environment_expiry is not None:
            coordinator._environment_expiry.cancel()
        await entry._async_process_on_unload(hass)
        await platform.async_reset()

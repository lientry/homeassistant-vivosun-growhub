"""Synthetic VSeed protocol and entity contract tests."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vivosun_growhub.api import VivosunApiClient
from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.models import AuthTokens, DeviceInfo, infer_device_type
from custom_components.vivosun_growhub.number import VivosunSeedSettingNumber
from custom_components.vivosun_growhub.select import VivosunSeedStageSelect
from custom_components.vivosun_growhub.sensor import (
    _VSEED_SENSORS,
    VivosunChannelSensorEntity,
    VivosunSeedLiveLightSensor,
)
from custom_components.vivosun_growhub.shadow import parse_shadow_document
from custom_components.vivosun_growhub.switch import VivosunSeedIrrigationSwitch, VivosunSeedStageSwitch
from custom_components.vivosun_growhub.vseed import command, parse_point_log, reported_seed, stage_name


def test_exact_vseed_scene_less_discovery() -> None:
    api = VivosunApiClient(None)  # type: ignore[arg-type]
    entry = {"name": "Propagation", "deviceId": "device-1", "clientId": "id-VSSBS40-1", "topicPrefix": "thing/1"}
    assert infer_device_type("Seed", entry["clientId"]) == "vseed"
    result = api._parse_device_entry(entry, category_key="STORE", index=0)
    assert result is not None and result.device_type == "vseed" and result.supports_point_log
    assert api._parse_device_entry({**entry, "clientId": "id-VSSBS41-1"}, category_key="STORE", index=1) is None
    assert api._parse_device_entry({**entry, "topicPrefix": "thing/+"}, category_key="STORE", index=2) is None


@pytest.mark.asyncio
async def test_scene_less_point_log_preserves_other_model_payloads() -> None:
    api = VivosunApiClient(None)  # type: ignore[arg-type]
    api._request_json = AsyncMock(return_value={"iotDataLogList": []})  # type: ignore[method-assign]
    tokens = AuthTokens("a", "b", "c", "d")
    device = DeviceInfo("device-1", "id-VSSBS40-1", "thing/1", "Seed", True, 0, "vseed")
    assert all(v is None for v in (await api.get_point_log(tokens, device, start_time=1, end_time=2)).values())
    assert "sceneId" not in api._request_json.await_args.kwargs["json_body"]
    other = DeviceInfo("device-2", "id-VSCTLY18-1", "thing/2", "Hub", True, 123, "controller")
    await api.get_point_log(tokens, other, start_time=1, end_time=2)
    assert api._request_json.await_args.kwargs["json_body"]["sceneId"] == 123


def test_vseed_point_log_clears_invalid_and_missing_samples() -> None:
    rows = [{"time": 1000, "rTemp": 2500, "pHumi": 6600},
            {"time": 1010, "rTemp": -6666, "pTemp": 2400, "pHumi": 10100, "pVpd": 135}]
    assert parse_point_log(rows, now=1020) == {
        "rTemp": None, "pTemp": 2400, "pHumi": None, "pVpd": 135, "rssi": None,
    }
    assert all(v is None for v in parse_point_log([], now=1020).values())


def test_desired_only_seed_cannot_be_reported_state() -> None:
    parsed = parse_shadow_document({"state": {"desired": {
        "seed": {"state": 3, "quickAct": {"waterTime": 600}}, "light": {"manu": {"lv": 42}},
    }}})
    assert "seed" not in parsed["reported_supported"]
    assert parsed["light"]["level"] == 42


@pytest.mark.asyncio
async def test_s40_entities_read_reported_and_publish_exact_fields() -> None:
    coordinator = MagicMock()
    coordinator.data = {"shadows": {"device-1": {"reported_supported": {"seed": {
        "state": 1, "lgtLv": 0, "curParams": {"lgtLv": 63},
        "stage1": {"lgtLv": 80, "lgtOnDur": 43260},
        "quickAct": {"waterTime": 0},
    }}}}}
    coordinator.async_publish_shadow_update = AsyncMock()
    stage = VivosunSeedStageSelect(coordinator, "device-1")
    light = VivosunSeedLiveLightSensor(coordinator, "device-1")
    setting = VivosunSeedSettingNumber(coordinator, "device-1", 1, "lgtOnDur")
    irrigation = VivosunSeedIrrigationSwitch(coordinator, "device-1")
    assert (stage.current_option, light.native_value, setting.native_value, irrigation.is_on) == (
        "Germination", 0, 43260, False,
    )
    assert (setting.native_min_value, setting.native_max_value, setting.native_step) == (0, 86400, 1)
    await stage.async_select_option("Seedling")
    await setting.async_set_native_value(85380)
    await irrigation.async_turn_on()
    assert [call.args[0] for call in coordinator.async_publish_shadow_update.await_args_list] == [
        command(("state",), 3), command(("stage1", "lgtOnDur"), 85380),
        command(("quickAct", "waterTime"), 600),
    ]
    assert stage.current_option == "Germination"
    assert irrigation.is_on is False
    with pytest.raises(ValueError):
        command(("stage1", "lgtOnTime"), 600)


def _real_coordinator(hass: HomeAssistant) -> VivosunCoordinator:
    coordinator = VivosunCoordinator(hass, object(), email="synthetic@example.test", password="unused")  # type: ignore[arg-type]
    coordinator._devices = [DeviceInfo("device-1", "id-VSSBS40-1", "thing/1", "Seed", True, 0, "vseed")]
    coordinator._build_topic_maps()
    coordinator.data = coordinator._build_state_snapshot()
    return coordinator


async def _receive_seed(coordinator: VivosunCoordinator, kind: str, document: dict[str, object]) -> None:
    await coordinator._handle_mqtt_publish(
        f"$aws/things/id-VSSBS40-1/shadow/{kind}", json.dumps(document).encode(), 1
    )


@pytest.mark.asyncio
async def test_live_mqtt_ignores_desired_ack_and_merges_only_reported_partials(hass: HomeAssistant) -> None:
    coordinator = _real_coordinator(hass)
    stage = VivosunSeedStageSelect(coordinator, "device-1")
    irrigation = VivosunSeedIrrigationSwitch(coordinator, "device-1")
    light = VivosunSeedLiveLightSensor(coordinator, "device-1")
    await _receive_seed(coordinator, "get/accepted", {"state": {"reported": {"seed": {
        "state": 1, "quickAct": {"waterTime": 0}, "stage1": {"lgtEn": 0},
        "lgtLv": 0, "curParams": {"lgtLv": 63},
    }}}})
    assert (stage.current_option, irrigation.is_on, light.native_value) == ("Germination", False, 0)

    mqtt = MagicMock(is_connected=True)
    mqtt.publish = AsyncMock()
    coordinator._mqtt_client = mqtt
    await stage.async_select_option("Seedling")
    await irrigation.async_turn_on()
    assert [json.loads(call.args[1]) for call in mqtt.publish.await_args_list] == [
        command(("state",), 3),
        command(("quickAct", "waterTime"), 600),
    ]
    assert all(call.kwargs["qos"] == 1 for call in mqtt.publish.await_args_list)
    assert (stage.current_option, irrigation.is_on, light.native_value) == ("Germination", False, 0)

    await _receive_seed(coordinator, "update/accepted", {"state": {"desired": {"seed": {
        "state": 3, "quickAct": {"waterTime": 600}, "stage1": {"lgtEn": 1},
    }}}})
    assert (stage.current_option, irrigation.is_on, light.native_value) == ("Germination", False, 0)
    assert reported_seed(coordinator, "device-1")["stage1"] == {"lgtEn": 0}

    await _receive_seed(coordinator, "update/documents", {
        "state": {"desired": {"seed": {"quickAct": {"waterTime": 600}}}},
        "current": {"state": {"reported": {"seed": {"quickAct": {"waterTime": 0}, "lackWater": 2}}}},
    })
    assert (stage.current_option, irrigation.is_on) == ("Germination", False)
    assert irrigation.extra_state_attributes["water_warning_code"] == 2
    await _receive_seed(coordinator, "update/accepted", {"state": {"reported": {"seed": {
        "state": 3, "quickAct": {"waterTime": 600},
    }}}})
    assert (stage.current_option, irrigation.is_on) == ("Seedling", True)
    await _receive_seed(coordinator, "update/accepted", {"state": {"desired": {"seed": {
        "quickAct": {"waterTime": 0},
    }}}})
    assert irrigation.is_on is True
    await _receive_seed(coordinator, "update/documents", {"current": {"state": {"reported": {
        "seed": {"quickAct": {"waterTime": 0}},
    }}}})
    assert (stage.current_option, irrigation.is_on) == ("Seedling", False)


@pytest.mark.asyncio
async def test_desired_only_seed_initial_state_remains_unknown(hass: HomeAssistant) -> None:
    coordinator = _real_coordinator(hass)
    await _receive_seed(coordinator, "update/accepted", {"state": {"desired": {"seed": {
        "state": 1, "quickAct": {"waterTime": 600},
    }}}})
    assert VivosunSeedStageSelect(coordinator, "device-1").current_option is None
    assert VivosunSeedIrrigationSwitch(coordinator, "device-1").is_on is None


@pytest.mark.asyncio
async def test_successful_s40_rest_poll_clears_stale_values_without_affecting_other_devices(
    hass: HomeAssistant,
) -> None:
    coordinator = _real_coordinator(hass)
    coordinator._tokens = AuthTokens("a", "b", "c", "d")
    api = VivosunApiClient(None)  # type: ignore[arg-type]
    timestamp = int(datetime.now(tz=UTC).timestamp())
    api._request_json = AsyncMock(side_effect=[  # type: ignore[method-assign]
        {"iotDataLogList": [{"time": timestamp, "rTemp": 2500, "pTemp": 2300, "pHumi": 7000, "pVpd": 120}]},
        {"iotDataLogList": [{"time": timestamp, "rTemp": -6666, "pTemp": 2400, "pVpd": -6666}]},
        {"iotDataLogList": []},
    ])
    coordinator._api = api
    entry = MagicMock(options={})
    coordinator._sensor_states["other-device"] = {"inTemp": 2100}

    def values() -> dict[str, float | None]:
        coordinator.data = coordinator._build_state_snapshot()
        return {
            desc.key: VivosunChannelSensorEntity(coordinator, entry, desc, "device-1").native_value
            for desc in _VSEED_SENSORS
        }

    await coordinator._refresh_point_log()
    assert values() == {
        "seed_root_temperature": 25.0, "seed_dome_temperature": 23.0,
        "seed_dome_humidity": 70.0, "seed_vpd": 1.2,
    }
    await coordinator._refresh_point_log()
    assert values() == {
        "seed_root_temperature": None, "seed_dome_temperature": 24.0,
        "seed_dome_humidity": None, "seed_vpd": None,
    }
    await coordinator._refresh_point_log()
    assert all(value is None for value in values().values())
    assert coordinator._sensor_states["other-device"] == {"inTemp": 2100}


@pytest.mark.parametrize(("path", "value"), [
    (("state",), 4), (("state",), True), (("quickAct", "waterTime"), 30),
    (("stage1", "lgtLv"), 24), (("stage1", "lgtLv"), 101),
    (("stage5", "pumpMode"), 1), (("stage2", "pumpMode"), 3),
    (("stage1", "lgtOnDur"), 86401), (("stage1", "lgtOnTime"), 21600),
])
def test_seed_command_rejects_unsupported_paths_and_values(path: tuple[str, ...], value: int) -> None:
    with pytest.raises(ValueError):
        command(path, value)


def test_stages_and_stored_enables_are_not_live_actuators() -> None:
    assert [stage_name(value) for value in range(4)] == [
        "Standby", "Germination", "Rooting", "Seedling",
    ]
    coordinator = MagicMock()
    coordinator.data = {"shadows": {"device-1": {"reported_supported": {"seed": {
        "curParams": {"lgtEn": 1}, "stage1": {"lgtEn": 0},
    }}}}}
    stage_enable = VivosunSeedStageSwitch(coordinator, "device-1", 1, "lgtEn")
    assert stage_enable.is_on is False

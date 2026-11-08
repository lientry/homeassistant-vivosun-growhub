"""Synthetic VSeed protocol expectations independent of captured identifiers."""

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
from custom_components.vivosun_growhub.shadow import parse_reported_fragment, parse_shadow_document
from custom_components.vivosun_growhub.switch import VivosunSeedIrrigationSwitch, VivosunSeedStageSwitch
from custom_components.vivosun_growhub.vseed import command, parse_point_log, reported_seed, stage_name


def test_vseed_model_token_overrides_friendly_name() -> None:
    assert infer_device_type("Seed station", "owner-VSSBS40-abc") == "vseed"
    assert infer_device_type("Seed station", "owner-UNKNOWN-abc") == "unknown"


def test_vseed_scene_less_discovery_only_for_exact_model() -> None:
    api = VivosunApiClient(None)  # type: ignore[arg-type]
    entry = {"name": "Propagation", "deviceId": "device-1", "clientId": "id-VSSBS40-1", "topicPrefix": "thing/1"}
    device = api._parse_device_entry(entry, category_key="STORE", index=0)
    assert device is not None
    assert device.device_type == "vseed"
    assert device.supports_point_log is True
    assert api._parse_device_entry({**entry, "clientId": "id-VSSBS41-1"}, category_key="STORE", index=1) is None
    assert api._parse_device_entry({**entry, "topicPrefix": "thing/+"}, category_key="STORE", index=2) is None
    assert api._parse_device_entry({**entry, "clientId": "id-VSSBS40-thing+"}, category_key="STORE", index=3) is None


@pytest.mark.asyncio
async def test_vseed_point_log_omits_scene_id_without_changing_other_models() -> None:
    api = VivosunApiClient(None)  # type: ignore[arg-type]
    api._request_json = AsyncMock(return_value={"iotDataLogList": []})  # type: ignore[method-assign]
    tokens = AuthTokens("a", "b", "c", "d")
    device = DeviceInfo("device-1", "id-VSSBS40-1", "thing/1", "Seed", True, 0, "vseed")
    assert await api.get_point_log(tokens, device, start_time=1, end_time=2) == {
        "rTemp": None, "pTemp": None, "pHumi": None, "pVpd": None, "rssi": None,
    }
    first_payload = api._request_json.await_args.kwargs["json_body"]
    assert first_payload == {
        "deviceId": "device-1", "startTime": 1, "endTime": 2,
        "reportType": 0, "orderBy": "asc", "timeLevel": "ONE_MINUTE",
    }
    other = DeviceInfo("device-2", "id-VSCTLY18-1", "thing/2", "Hub", True, 123, "controller")
    await api.get_point_log(tokens, other, start_time=1, end_time=2)
    assert api._request_json.await_args.kwargs["json_body"]["sceneId"] == 123


def test_seed_reported_state_is_not_stage_recipe_or_desired() -> None:
    report = parse_reported_fragment({"seed": {"state": 1, "curParams": {"lgtLv": 63}, "stage1": {"lgtLv": 100}}})
    assert report["reported_supported"]["seed"] == {"state": 1, "curParams": {"lgtLv": 63}, "stage1": {"lgtLv": 100}}
    partial = parse_reported_fragment({"seed": {"quickAct": {"waterTime": 0}}})
    assert partial["reported_supported"]["seed"] == {"quickAct": {"waterTime": 0}}


def test_shadow_parser_excludes_only_seed_from_desired_fallback() -> None:
    parsed = parse_shadow_document({"state": {"desired": {
        "seed": {"state": 3, "quickAct": {"waterTime": 600}},
        "light": {"manu": {"lv": 42}},
    }}})
    assert "seed" not in parsed["reported_supported"]
    assert parsed["light"]["level"] == 42


def test_vseed_scene_less_point_log_filters_and_uses_newest_sample() -> None:
    rows = [
        {"time": 1000, "rTemp": 2500, "pTemp": 2300, "pHumi": 6600, "pVpd": 120, "rssi": -55},
        {"time": 1010, "rTemp": 2550, "pTemp": 2400, "pHumi": -6666, "pVpd": 135, "rssi": -60},
        {"time": 9999, "rTemp": 9900},
    ]
    assert parse_point_log(rows, now=1020) == {
        "rTemp": 2550, "pTemp": 2400, "pHumi": None, "pVpd": 135, "rssi": -60,
    }


def test_vseed_point_log_never_invents_missing_values() -> None:
    missing = {"rTemp": None, "pTemp": None, "pHumi": None, "pVpd": None, "rssi": None}
    assert parse_point_log([{"time": 100, "rTemp": -6666, "pHumi": 10100}], now=100) == missing
    assert parse_point_log([{"rTemp": 2100}], now=100) == missing
    assert parse_point_log([], now=100) == missing


def test_vseed_telemetry_entities_use_specific_root_and_dome_channels() -> None:
    coordinator = _coordinator({"lgtLv": 0, "curParams": {"lgtLv": 63}, "stage1": {"lgtLv": 100}})
    coordinator.data["sensors"] = {"device-1": {"rTemp": 2550, "pTemp": 2400, "pHumi": 6600, "pVpd": 135}}
    entry = MagicMock()
    entry.options = {}
    values = {
        desc.key: VivosunChannelSensorEntity(coordinator, entry, desc, "device-1").native_value
        for desc in _VSEED_SENSORS
    }
    assert values["seed_root_temperature"] == 25.5
    assert values["seed_dome_temperature"] == 24.0
    assert values["seed_dome_humidity"] == 66.0
    assert values["seed_vpd"] == 1.35
    # Capture event 31: physical light off while configured light intensity is 63.
    assert VivosunSeedLiveLightSensor(coordinator, "device-1").native_value == 0


def _real_coordinator(hass: HomeAssistant) -> VivosunCoordinator:
    coordinator = VivosunCoordinator(hass, object(), email="synthetic@example.test", password="unused")  # type: ignore[arg-type]
    coordinator._devices = [DeviceInfo("device-1", "id-VSSBS40-1", "thing/1", "Seed", True, 0, "vseed")]
    coordinator._build_topic_maps()
    coordinator.data = coordinator._build_state_snapshot()
    return coordinator


async def _receive_seed(coordinator: VivosunCoordinator, kind: str, document: dict[str, object]) -> None:
    topic = f"$aws/things/id-VSSBS40-1/shadow/{kind}"
    await coordinator._handle_mqtt_publish(topic, json.dumps(document).encode(), 1)


@pytest.mark.asyncio
async def test_real_mqtt_ingress_ignores_desired_ack_and_preserves_partial_report(
    hass: HomeAssistant,
) -> None:
    coordinator = _real_coordinator(hass)
    stage = VivosunSeedStageSelect(coordinator, "device-1")
    irrigation = VivosunSeedIrrigationSwitch(coordinator, "device-1")
    light = VivosunSeedLiveLightSensor(coordinator, "device-1")
    await _receive_seed(coordinator, "get/accepted", {"state": {"reported": {"seed": {
        "state": 1, "quickAct": {"waterTime": 0}, "stage1": {"lgtEn": 0},
        "lgtLv": 0, "curParams": {"lgtLv": 63},
    }}}})
    assert (stage.current_option, irrigation.is_on) == ("Germination", False)
    assert light.native_value == 0

    mqtt = MagicMock(is_connected=True)
    mqtt.publish = AsyncMock()
    coordinator._mqtt_client = mqtt
    await stage.async_select_option("Seedling")
    await irrigation.async_turn_on()
    assert [json.loads(call.args[1]) for call in mqtt.publish.await_args_list] == [
        {"state": {"desired": {"seed": {"state": 3}}}},
        {"state": {"desired": {"seed": {"quickAct": {"waterTime": 600}}}}},
    ]
    assert all(call.kwargs["qos"] == 1 for call in mqtt.publish.await_args_list)
    assert (stage.current_option, irrigation.is_on) == ("Germination", False)

    # A request acknowledgement must never masquerade as a reported state.
    await _receive_seed(coordinator, "update/accepted", {"state": {"desired": {"seed": {
        "state": 3, "quickAct": {"waterTime": 600}, "stage1": {"lgtEn": 1}, "lgtLv": 63,
    }}}})
    assert (stage.current_option, irrigation.is_on) == ("Germination", False)
    assert light.native_value == 0
    assert reported_seed(coordinator, "device-1")["stage1"] == {"lgtEn": 0}

    # Captured empty-tank refusal updates only quick irrigation and fault status.
    await _receive_seed(coordinator, "update/documents", {
        "state": {"desired": {"seed": {"quickAct": {"waterTime": 600}}}},
        "current": {"state": {"reported": {"seed": {"quickAct": {"waterTime": 0}, "lackWater": 2}}}},
    })
    assert (stage.current_option, irrigation.is_on) == ("Germination", False)
    assert light.native_value == 0
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
async def test_real_mqtt_ingress_desired_only_initial_seed_is_unknown(hass: HomeAssistant) -> None:
    coordinator = _real_coordinator(hass)
    await _receive_seed(coordinator, "update/accepted", {"state": {"desired": {"seed": {
        "state": 1, "quickAct": {"waterTime": 600},
    }}}})
    assert VivosunSeedStageSelect(coordinator, "device-1").current_option is None
    assert VivosunSeedIrrigationSwitch(coordinator, "device-1").is_on is None


@pytest.mark.asyncio
async def test_vseed_ingress_change_does_not_modify_controller_desired_fallback(hass: HomeAssistant) -> None:
    coordinator = _real_coordinator(hass)
    coordinator._devices = [DeviceInfo("device-2", "id-VSCTLY18-1", "thing/2", "Hub", True, 100, "controller")]
    coordinator._build_topic_maps()
    await coordinator._handle_mqtt_publish(
        "$aws/things/id-VSCTLY18-1/shadow/update/accepted",
        json.dumps({"state": {"desired": {"light": {"manu": {"lv": 42}}}}}).encode(),
        1,
    )
    assert coordinator.data["shadows"]["device-2"]["light"]["level"] == 42


@pytest.mark.asyncio
async def test_real_api_coordinator_poll_invalidates_only_vseed_readings(hass: HomeAssistant) -> None:
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

    def values() -> dict[str, float | None]:
        coordinator.data = coordinator._build_state_snapshot()
        return {
            desc.key: VivosunChannelSensorEntity(coordinator, entry, desc, "device-1").native_value
            for desc in _VSEED_SENSORS
        }

    coordinator._sensor_states["other-device"] = {"inTemp": 2100}
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


@pytest.mark.parametrize(("path", "value", "expected"), [
    (("state",), 2, {"state": 2}),
    (("quickAct", "waterTime"), 600, {"quickAct": {"waterTime": 600}}),
    (("quickAct", "waterTime"), 0, {"quickAct": {"waterTime": 0}}),
    (("stage1", "lgtLv"), 63, {"stage1": {"lgtLv": 63}}),
    (("stage2", "pumpMode"), 2, {"stage2": {"pumpMode": 2}}),
    (("stage1", "lgtOnDur"), 43260, {"stage1": {"lgtOnDur": 43260}}),
    (("stage1", "lgtOnDur"), 85380, {"stage1": {"lgtOnDur": 85380}}),
])
def test_command_matches_single_field_seed_shadow(
    path: tuple[str, ...], value: int, expected: dict[str, object]
) -> None:
    assert command(path, value) == {"state": {"desired": {"seed": expected}}}


@pytest.mark.parametrize(("path", "value"), [
    (("state",), 4), (("state",), True), (("quickAct", "waterTime"), 30),
    (("stage1", "lgtLv"), 24), (("stage1", "lgtLv"), 101),
    (("stage5", "pumpMode"), 1), (("stage2", "pumpMode"), 3),
    (("stage1", "lgtOnDur"), 86401), (("stage1", "lgtOnTime"), 21600),
])
def test_unsupported_seed_commands_are_rejected(path: tuple[str, ...], value: int) -> None:
    with pytest.raises(ValueError):
        command(path, value)


def test_stage_labels_are_product_vocabulary() -> None:
    assert [stage_name(value) for value in range(4)] == ["Standby", "Germination", "Rooting", "Seedling"]


def _coordinator(seed: dict[str, object]) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = {"shadows": {"device-1": {"reported_supported": {"seed": seed}}}}
    coordinator.async_publish_shadow_update = AsyncMock()
    return coordinator


@pytest.mark.asyncio
async def test_stage_select_uses_reported_state_and_exact_command() -> None:
    coordinator = _coordinator({"state": 1, "stage2": {"lgtLv": 55}})
    entity = VivosunSeedStageSelect(coordinator, "device-1")
    assert entity.current_option == "Germination"
    coordinator.is_mqtt_connected = False
    assert entity.available is False
    coordinator.is_mqtt_connected = True
    await entity.async_select_option("Rooting")
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"seed": {"state": 2}}}}, device_id="device-1", qos=1
    )
    assert entity.current_option == "Germination"


@pytest.mark.asyncio
async def test_quick_irrigation_is_reported_not_optimistic() -> None:
    coordinator = _coordinator({"quickAct": {"waterTime": 0}, "lackWater": 2})
    entity = VivosunSeedIrrigationSwitch(coordinator, "device-1")
    assert entity.is_on is False
    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"seed": {"quickAct": {"waterTime": 600}}}}}, device_id="device-1", qos=1
    )
    assert entity.is_on is False
    await entity.async_turn_off()
    assert coordinator.async_publish_shadow_update.await_args_list[-1].kwargs == {"device_id": "device-1", "qos": 1}
    last_payload = coordinator.async_publish_shadow_update.await_args_list[-1].args[0]
    assert last_payload["state"]["desired"]["seed"] == {"quickAct": {"waterTime": 0}}


@pytest.mark.asyncio
async def test_stage_setting_reads_stored_recipe_and_writes_only_that_stage() -> None:
    coordinator = _coordinator({"state": 1, "curParams": {"lgtLv": 63}, "stage2": {"lgtLv": 80}})
    entity = VivosunSeedSettingNumber(coordinator, "device-1", 2, "lgtLv")
    assert entity.native_value == 80
    await entity.async_set_native_value(75)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"seed": {"stage2": {"lgtLv": 75}}}}}, device_id="device-1", qos=1
    )
    assert entity.native_value == 80


@pytest.mark.asyncio
async def test_stage_light_duration_accepts_captured_seconds_not_light_start_time() -> None:
    coordinator = _coordinator({"stage1": {"lgtOnDur": 43260, "lgtOnTime": 0}})
    entity = VivosunSeedSettingNumber(coordinator, "device-1", 1, "lgtOnDur")
    assert entity.name == "Germination Light Duration"
    assert entity.native_unit_of_measurement == "s"
    assert (entity.native_min_value, entity.native_max_value, entity.native_step) == (0, 86400, 1)
    assert entity.native_value == 43260
    await entity.async_set_native_value(85380)
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"seed": {"stage1": {"lgtOnDur": 85380}}}}}, device_id="device-1", qos=1
    )
    assert entity.native_value == 43260
    with pytest.raises(ValueError):
        VivosunSeedSettingNumber(coordinator, "device-1", 1, "lgtOnTime")


@pytest.mark.asyncio
async def test_stage_light_enable_is_stored_setting_not_live_light_state() -> None:
    coordinator = _coordinator({"curParams": {"lgtEn": 1}, "stage1": {"lgtEn": 0}})
    entity = VivosunSeedStageSwitch(coordinator, "device-1", 1, "lgtEn")
    assert entity.is_on is False
    await entity.async_turn_on()
    coordinator.async_publish_shadow_update.assert_awaited_once_with(
        {"state": {"desired": {"seed": {"stage1": {"lgtEn": 1}}}}}, device_id="device-1", qos=1
    )
    assert entity.is_on is False

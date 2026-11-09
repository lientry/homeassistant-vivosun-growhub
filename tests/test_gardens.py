"""Synthetic garden inventory and configured recipe read contracts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import aiohttp
import pytest

from custom_components.vivosun_growhub.api import VivosunApiClient
from custom_components.vivosun_growhub.exceptions import (
    VivosunAuthError,
    VivosunConnectionError,
    VivosunResponseError,
)
from custom_components.vivosun_growhub.gardens import parse_garden_inventory, parse_garden_plans
from custom_components.vivosun_growhub.models import AuthTokens

if TYPE_CHECKING:
    from collections.abc import Callable


def _garden(scene_id: int, name: str, *devices: str) -> dict[str, object]:
    return {
        "sceneId": scene_id,
        "sceneGroupId": 600,
        "sceneName": name,
        "timezoneId": "Europe/Berlin",
        "iotDeviceList": [
            {"deviceId": device_id, "clientId": f"synthetic-{device_id}"} for device_id in devices
        ],
    }


def _stage(plan_id: int, stage_id: int, start_time: object = 0, *, content: object = "{}") -> dict[str, object]:
    return {
        "userPlanStageId": stage_id,
        "userPlanId": plan_id,
        "stageFlag": "stage1",
        "stageName": "Seedling",
        "icon": "seedling",
        "sort": 10,
        "startTime": start_time,
        "timeOffset": 3600,
        "contentUpdate": 12345,
        "planMode": 1,
        "planStageContent": content,
    }


def _plan(
    scene_id: int, plan_id: int, name: str, *stages: dict[str, object], devices: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "sceneId": scene_id,
        "userPlanId": plan_id,
        "userRecipeId": 300 + plan_id,
        "name": name,
        "needSync": False,
        "iotDeviceList": [{"deviceId": d, "clientId": f"synthetic-{d}"} for d in devices],
        "userPlanStageList": list(stages),
    }


def _plans_payload(scene_id: int, *plans: dict[str, object]) -> dict[str, object]:
    return {"scene": _garden(scene_id, "Test Garden"), "iotUserPlanList": list(plans)}


def _envelope(data: object, *, success: bool = True) -> dict[str, object]:
    return {"success": success, "message": "success" if success else "bad request", "data": data}


class _Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, *, content_type: str | None = None) -> object:
        return self.payload


class _Session:
    def __init__(self, *responses: _Response, error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def _tokens() -> AuthTokens:
    return AuthTokens("access", "login", "refresh", "user")


def test_garden_inventory_keeps_three_stable_identities_and_empty_membership() -> None:
    data = {"iotSceneList": [
        _garden(10, "My Garden", "controller-a"),
        _garden(20, "Tests", "controller-b"),
        _garden(30, "Empty Garden"),
    ]}

    result = parse_garden_inventory(data)

    assert [(g.scene_id, g.scene_group_id, g.name, g.timezone_id) for g in result.gardens] == [
        (10, 600, "My Garden", "Europe/Berlin"),
        (20, 600, "Tests", "Europe/Berlin"),
        (30, 600, "Empty Garden", "Europe/Berlin"),
    ]
    assert [[m.device_id for m in g.members] for g in result.gardens] == [
        ["controller-a"], ["controller-b"], [],
    ]
    assert result.gardens[0].members[0].client_id == "synthetic-controller-a"
    assert parse_garden_inventory({"iotSceneList": []}).gardens == ()


@pytest.mark.parametrize("mutation", [
    lambda data: data.pop("iotSceneList"),
    lambda data: data.update(iotSceneList=None),
    lambda data: data.update(iotSceneList=[_garden(10, "Again"), _garden(10, "Duplicate")]),
    lambda data: data["iotSceneList"][0].update(sceneId="10"),
    lambda data: data["iotSceneList"][0].update(sceneId=0),
    lambda data: data["iotSceneList"][0].update(sceneName=None),
    lambda data: data["iotSceneList"][0].update(timezoneId=3),
    lambda data: data["iotSceneList"][0]["iotDeviceList"].append({"deviceId": "controller-a"}),
    lambda data: data["iotSceneList"][0]["iotDeviceList"][0].pop("deviceId"),
    lambda data: data["iotSceneList"][0].update(iotDeviceList=None),
])
def test_garden_inventory_rejects_partial_or_duplicate_identity(
    mutation: Callable[[dict[str, object]], object],
) -> None:
    data = {"iotSceneList": [_garden(10, "My Garden", "controller-a"), _garden(20, "Tests")]}
    mutation(data)
    with pytest.raises(VivosunResponseError):
        parse_garden_inventory(data)


def test_garden_inventory_rejects_device_in_two_gardens() -> None:
    data = {"iotSceneList": [_garden(10, "One", "a"), _garden(20, "Two", "a")]}
    with pytest.raises(VivosunResponseError, match="device"):
        parse_garden_inventory(data)


def test_recipe_lists_keep_plural_plans_and_independent_schedule_states() -> None:
    light = json.dumps({"light": {"slot": [{"time": 360, "level": 50}]}})
    stages = [
        _stage(101, 1001, 0, content="{}"),
        _stage(101, 1002, 20, content=light),
        _stage(101, 1003, 300, content=light),
        _stage(101, 1004, None, content="{}"),
    ]
    stages[3]["stageFlag"] = "stage4"
    stages[3]["stageName"] = "Drying"
    data = _plans_payload(
        10,
        _plan(10, 101, "Autoflower", *stages, devices=("controller-a",)),
        _plan(10, 102, "Photoperiod", _stage(102, 2001, 40), devices=("controller-b",)),
    )

    result = parse_garden_plans(data, scene_id=10)

    assert (result.scene_id, len(result.plans)) == (10, 2)
    assert [(p.plan_id, p.name, p.need_sync, p.recipe_id) for p in result.plans] == [
        (101, "Autoflower", False, 401), (102, "Photoperiod", False, 402),
    ]
    assert [[m.device_id for m in p.members] for p in result.plans] == [["controller-a"], ["controller-b"]]
    first = result.plans[0]
    assert [(s.stage_id, s.stage_flag, s.time_offset, s.content_update, s.plan_mode) for s in first.stages] == [
        (1001, "stage1", 3600, 12345, 1),
        (1002, "stage1", 3600, 12345, 1),
        (1003, "stage1", 3600, 12345, 1),
        (1004, "stage4", 3600, 12345, 1),
    ]
    assert [s.state_at(100) for s in first.stages] == ["disabled", "running", "scheduled", "disabled"]
    assert first.stages[0].content_status == "valid" and first.stages[0].content == {}
    assert first.stages[1].content == {"light": {"slot": [{"time": 360, "level": 50}]}}
    # Both independently configured running stages survive; there is no invented consensus.
    assert [s.stage_id for p in result.plans for s in p.stages if s.state_at(100) == "running"] == [
        1002, 2001,
    ]


def test_recipe_empty_vs_missing_plan_list_and_missing_stage_time() -> None:
    assert parse_garden_plans(_plans_payload(30), scene_id=30).plans == ()
    stage = _stage(101, 1001)
    del stage["startTime"]
    result = parse_garden_plans(_plans_payload(10, _plan(10, 101, "Unknown", stage)), scene_id=10)
    assert result.plans[0].stages[0].state_at(100) == "unknown"

    with pytest.raises(VivosunResponseError):
        parse_garden_plans({"scene": _garden(30, "Empty Garden")}, scene_id=30)


@pytest.mark.parametrize("content,expected", [
    (None, "missing"),
    ("not json", "invalid"),
    ("[]", "invalid"),
    ("", "invalid"),
    ("{}", "valid"),
])
def test_recipe_content_distinguishes_missing_invalid_and_valid_empty(content: object, expected: str) -> None:
    stage = _stage(101, 1001, content=content)
    result = parse_garden_plans(_plans_payload(10, _plan(10, 101, "A", stage)), scene_id=10)
    parsed = result.plans[0].stages[0]
    assert parsed.content_status == expected
    assert parsed.content == ({} if expected == "valid" else None)


@pytest.mark.parametrize("mutation", [
    lambda data: data["scene"].update(sceneId=20),
    lambda data: data["iotUserPlanList"][0].update(sceneId=20),
    lambda data: data["iotUserPlanList"][0]["userPlanStageList"][0].update(userPlanId=999),
    lambda data: data["iotUserPlanList"].append(_plan(10, 101, "Duplicate")),
    lambda data: data["iotUserPlanList"][0]["userPlanStageList"].append(_stage(101, 1001)),
    lambda data: data["iotUserPlanList"][0].pop("userPlanStageList"),
    lambda data: data["iotUserPlanList"][0].pop("name"),
    lambda data: data["iotUserPlanList"][0].update(needSync="false"),
    lambda data: data["iotUserPlanList"][0]["userPlanStageList"][0].update(startTime="10"),
    lambda data: data.update(iotUserPlanList=None),
])
def test_recipe_reads_reject_partial_duplicate_or_conflicting_data(
    mutation: Callable[[dict[str, object]], object],
) -> None:
    data = _plans_payload(10, _plan(10, 101, "A", _stage(101, 1001)))
    mutation(data)
    with pytest.raises(VivosunResponseError):
        parse_garden_plans(data, scene_id=10)


async def test_client_uses_authenticated_encrypted_post_for_only_two_read_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext_bodies: list[dict[str, object]] = []

    def fake_encrypt(body: bytes, *, timestamp_ms: int) -> tuple[str, str, bytes]:
        assert timestamp_ms > 0
        plaintext_bodies.append(json.loads(body))
        return "time", "code", b"ciphertext"

    monkeypatch.setattr("custom_components.vivosun_growhub.api.encrypt_request_body", fake_encrypt)
    session = _Session(
        _Response(_envelope({"iotSceneList": [_garden(10, "My Garden", "controller-a")]})),
        _Response(_envelope(_plans_payload(10, _plan(10, 101, "A", _stage(101, 1001))))),
    )
    client = VivosunApiClient(cast("aiohttp.ClientSession", session))
    inventory = await client.get_gardens(_tokens())
    plans = await client.get_garden_plans(_tokens(), inventory.gardens[0].scene_id)

    assert len(inventory.gardens) == len(plans.plans) == 1
    assert plaintext_bodies == [{"sceneGroupName": "garden"}, {"sceneId": 10}]
    assert [(method, url.rsplit("/", 3)[-3:]) for method, url, _ in session.calls] == [
        ("POST", ["iot", "scene", "getList"]), ("POST", ["iot", "plan", "getList"]),
    ]
    for _method, _url, kwargs in session.calls:
        assert kwargs["data"] == b"ciphertext"
        assert kwargs["headers"]["access-token"] == "access"
        assert kwargs["headers"]["login-token"] == "login"
        assert kwargs["headers"]["Request-Code"] == "code"


@pytest.mark.parametrize("response,error", [
    (_Response(_envelope({})), VivosunResponseError),
    (_Response(_envelope({}, success=False)), VivosunResponseError),
    (_Response(_envelope({}), status=401), VivosunAuthError),
])
async def test_client_failed_inventory_is_not_an_empty_success(response: _Response, error: type[Exception]) -> None:
    client = VivosunApiClient(cast("aiohttp.ClientSession", _Session(response)))
    with pytest.raises(error):
        await client.get_gardens(_tokens())


async def test_client_transport_failure_is_not_an_empty_plan_list() -> None:
    session = _Session(error=aiohttp.ClientError("offline"))
    client = VivosunApiClient(cast("aiohttp.ClientSession", session))
    with pytest.raises(VivosunConnectionError):
        await client.get_garden_plans(_tokens(), 10)


async def test_invalid_garden_id_never_sends_request() -> None:
    session = _Session()
    client = VivosunApiClient(cast("aiohttp.ClientSession", session))
    with pytest.raises(ValueError):
        await client.get_garden_plans(_tokens(), 0)
    assert session.calls == []

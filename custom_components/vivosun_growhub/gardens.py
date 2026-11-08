"""Strict read-only normalization of garden inventory and configured recipes.

Invalid complete snapshots raise VivosunResponseError. Callers must retain their
last successful snapshot rather than interpreting a failed read as an empty one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

from .exceptions import VivosunResponseError


@dataclass(frozen=True, slots=True)
class GardenMember:
    """Device reference from a garden or recipe, not a discovered device route."""

    device_id: str
    client_id: str | None


@dataclass(frozen=True, slots=True)
class Garden:
    """Cloud garden identity and its explicitly listed device references."""

    scene_id: int
    scene_group_id: int
    name: str
    timezone_id: str
    members: tuple[GardenMember, ...]


@dataclass(frozen=True, slots=True)
class GardenInventory:
    """Complete successful garden listing, including gardens with no members."""

    gardens: tuple[Garden, ...]


ContentStatus = Literal["valid", "missing", "invalid"]
StageState = Literal["disabled", "running", "scheduled", "unknown"]


@dataclass(frozen=True, slots=True)
class GardenStage:
    """Configured stage timing and content, never device-reported execution."""

    stage_id: int
    plan_id: int
    stage_flag: str
    name: str
    icon: str
    sort: int
    start_time: int | None
    start_time_present: bool
    time_offset: int
    content_update: int
    plan_mode: int
    content: dict[str, object] | None
    content_status: ContentStatus

    def state_at(self, epoch_seconds: int) -> StageState:
        """Classify this one stage using app-source startTime conventions."""
        if not self.start_time_present:
            return "unknown"
        if self.start_time is None or self.start_time == 0:
            return "disabled"
        try:
            datetime.fromtimestamp(self.start_time, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return "unknown"
        return "running" if self.start_time <= epoch_seconds else "scheduled"


@dataclass(frozen=True, slots=True)
class GardenPlan:
    """One configured plan; gardens can have zero or multiple plans."""

    plan_id: int
    recipe_id: int | None
    scene_id: int
    name: str
    need_sync: bool
    members: tuple[GardenMember, ...]
    stages: tuple[GardenStage, ...]


@dataclass(frozen=True, slots=True)
class GardenPlans:
    """Successful per-garden plan read, distinct from a failed read."""

    scene_id: int
    plans: tuple[GardenPlan, ...]


@dataclass(frozen=True, slots=True)
class GardenPlanRead:
    """Read health independent of MQTT; refreshing retains a previously valid read."""

    plans: GardenPlans | None
    status: Literal["unknown", "current", "refreshing", "stale"]


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise VivosunResponseError(f"Expected object at {context}")
    return value


def _list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise VivosunResponseError(f"Expected list at {context}")
    return value


def _int(value: object, context: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value <= 0):
        raise VivosunResponseError(f"Invalid integer at {context}")
    return value


def _str(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VivosunResponseError(f"Invalid string at {context}")
    return value


def _optional_int(value: object, context: str) -> int | None:
    if value is None:
        return None
    return _int(value, context)


def _members(value: object, context: str) -> tuple[GardenMember, ...]:
    result: list[GardenMember] = []
    seen: set[str] = set()
    for index, raw in enumerate(_list(value, context)):
        member = _mapping(raw, f"{context}[{index}]")
        device_id = _str(member.get("deviceId"), "deviceId")
        if device_id in seen:
            raise VivosunResponseError("Duplicate device reference")
        seen.add(device_id)
        client_id = member.get("clientId")
        if client_id is not None:
            client_id = _str(client_id, "clientId")
        result.append(GardenMember(device_id=device_id, client_id=client_id))
    return tuple(result)


def parse_garden_inventory(data: Mapping[str, object]) -> GardenInventory:
    """Require a complete scene list before allowing membership reconciliation."""
    gardens: list[Garden] = []
    seen_ids: set[int] = set()
    seen_devices: set[str] = set()
    for index, raw in enumerate(_list(data.get("iotSceneList"), "iotSceneList")):
        scene = _mapping(raw, f"iotSceneList[{index}]")
        scene_id = _int(scene.get("sceneId"), "sceneId", positive=True)
        if scene_id in seen_ids:
            raise VivosunResponseError("Duplicate garden identity")
        seen_ids.add(scene_id)
        members = _members(scene.get("iotDeviceList"), "iotDeviceList")
        for member in members:
            if member.device_id in seen_devices:
                raise VivosunResponseError("Conflicting device garden membership")
            seen_devices.add(member.device_id)
        gardens.append(Garden(
            scene_id=scene_id,
            scene_group_id=_int(scene.get("sceneGroupId"), "sceneGroupId", positive=True),
            name=_str(scene.get("sceneName"), "sceneName"),
            timezone_id=_str(scene.get("timezoneId"), "timezoneId"),
            members=members,
        ))
    return GardenInventory(gardens=tuple(gardens))


def _content(raw: object) -> tuple[dict[str, object] | None, ContentStatus]:
    if raw is None:
        return None, "missing"
    if not isinstance(raw, str):
        return None, "invalid"
    try:
        value = json.loads(raw)
    except ValueError:
        return None, "invalid"
    if not isinstance(value, dict):
        return None, "invalid"
    return value, "valid"


def parse_garden_plans(data: Mapping[str, object], *, scene_id: int) -> GardenPlans:
    """Decode all plans for a known garden without selecting a current plan."""
    if scene_id <= 0:
        raise ValueError("scene_id must be positive")
    scene = _mapping(data.get("scene"), "scene")
    if _int(scene.get("sceneId"), "scene.sceneId") != scene_id:
        raise VivosunResponseError("Plan list garden identity mismatch")
    result: list[GardenPlan] = []
    seen_plans: set[int] = set()
    seen_stages: set[int] = set()
    for index, raw in enumerate(_list(data.get("iotUserPlanList"), "iotUserPlanList")):
        plan = _mapping(raw, f"iotUserPlanList[{index}]")
        plan_id = _int(plan.get("userPlanId"), "userPlanId", positive=True)
        if plan_id in seen_plans:
            raise VivosunResponseError("Duplicate plan identity")
        seen_plans.add(plan_id)
        if _int(plan.get("sceneId"), "plan.sceneId") != scene_id:
            raise VivosunResponseError("Plan garden identity mismatch")
        need_sync = plan.get("needSync")
        if not isinstance(need_sync, bool):
            raise VivosunResponseError("Invalid needSync")
        stages: list[GardenStage] = []
        for raw_stage in _list(plan.get("userPlanStageList"), "userPlanStageList"):
            stage = _mapping(raw_stage, "userPlanStageList item")
            stage_id = _int(stage.get("userPlanStageId"), "userPlanStageId", positive=True)
            if stage_id in seen_stages:
                raise VivosunResponseError("Duplicate stage identity")
            seen_stages.add(stage_id)
            if _int(stage.get("userPlanId"), "stage.userPlanId") != plan_id:
                raise VivosunResponseError("Stage plan identity mismatch")
            start_time = _optional_int(stage.get("startTime"), "startTime")
            if start_time is not None and start_time < 0:
                raise VivosunResponseError("Invalid startTime")
            content, content_status = _content(stage.get("planStageContent"))
            icon = stage.get("icon")
            stages.append(GardenStage(
                stage_id=stage_id,
                plan_id=plan_id,
                stage_flag=_str(stage.get("stageFlag"), "stageFlag"),
                name=_str(stage.get("stageName"), "stageName"),
                icon=icon if isinstance(icon, str) else "",
                sort=_int(stage.get("sort"), "sort"),
                start_time=start_time,
                start_time_present="startTime" in stage,
                time_offset=_int(stage.get("timeOffset"), "timeOffset"),
                content_update=_int(stage.get("contentUpdate"), "contentUpdate"),
                plan_mode=_int(stage.get("planMode"), "planMode"),
                content=content,
                content_status=content_status,
            ))
        result.append(GardenPlan(
            plan_id=plan_id,
            recipe_id=_optional_int(plan.get("userRecipeId"), "userRecipeId"),
            scene_id=scene_id,
            name=_str(plan.get("name"), "plan.name"),
            need_sync=need_sync,
            members=_members(plan.get("iotDeviceList"), "plan.iotDeviceList"),
            stages=tuple(stages),
        ))
    return GardenPlans(scene_id=scene_id, plans=tuple(result))

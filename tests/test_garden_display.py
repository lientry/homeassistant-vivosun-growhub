"""Garden-owned recipe projections, independent of controller shadow execution."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from custom_components.vivosun_growhub.garden_display import select_garden_plan
from custom_components.vivosun_growhub.gardens import GardenPlans, parse_garden_plans

if TYPE_CHECKING:
    from collections.abc import Sequence


def _plans(
    *plan_stages: Sequence[tuple[str, int | None]],
    content: object = None,
) -> GardenPlans:
    """Produce a synthetic wire-shaped recipe snapshot with distinct stage identities."""
    plans: list[dict[str, object]] = []
    for plan_index, stages in enumerate(plan_stages, start=1):
        plan_id = 100 + plan_index
        plans.append({
            "sceneId": 10,
            "userPlanId": plan_id,
            "userRecipeId": plan_id + 100,
            "name": f"Recipe {plan_index}",
            "needSync": True,
            "iotDeviceList": [],
            "userPlanStageList": [
                {
                    "userPlanStageId": plan_id * 100 + i,
                    "userPlanId": plan_id,
                    "stageFlag": f"stage{i}",
                    "stageName": name,
                    "icon": "",
                    "sort": i,
                    "startTime": timestamp,
                    "timeOffset": 0,
                    "contentUpdate": 0,
                    "planMode": 1,
                    "planStageContent": content if content is not None else json.dumps({
                        "light": {"slot": [{"time": 360, "level": 60}, {"time": 1080, "level": 0}]}
                    }),
                }
                for i, (name, timestamp) in enumerate(stages, start=1)
            ],
        })
    return parse_garden_plans({
        "scene": {"sceneId": 10},
        "iotUserPlanList": plans,
    }, scene_id=10)


@pytest.mark.parametrize(("stages", "stage_state", "stage_name"), [
    ([("Seedling", 0), ("Vegetative", 0)], "inactive", None),
    ([("Seedling", 0), ("Vegetative", 200)], "scheduled", "Vegetative"),
    ([("Seedling", 50), ("Vegetative", 200)], "running", "Seedling"),
    ([("Seedling", 50), ("Vegetative", 75)], "ambiguous", None),
    ([("Seedling", None), ("Vegetative", 0)], "inactive", None),
])
def test_stage_selection_distinguishes_timing(
    stages: list[tuple[str, int | None]], stage_state: str, stage_name: str | None,
) -> None:
    projection = select_garden_plan(_plans(stages), now_epoch=100)
    assert projection.recipe == "Recipe 1"
    assert projection.state == stage_state
    assert (projection.stage.name if projection.stage else None) == stage_name


def test_ambiguous_plans_and_tied_schedules_do_not_select_first() -> None:
    assert select_garden_plan(_plans([("Seedling", 50)], [("Drying", 0)]), 100).state == "ambiguous"
    assert select_garden_plan(
        _plans([("Seedling", 200), ("Vegetative", 200)]), 100,
    ).state == "ambiguous"


def test_empty_is_known_inactive_but_missing_start_is_unknown() -> None:
    empty = select_garden_plan(_plans(), now_epoch=100)
    assert (empty.recipe, empty.state, empty.stage) == ("No recipe", "inactive", None)

    payload = _plans([("Seedling", 0)])
    stage = payload.plans[0].stages[0]
    uncertain = replace(payload, plans=(replace(payload.plans[0], stages=(
        replace(stage, start_time_present=False),
    )),))
    assert select_garden_plan(uncertain, 100).state == "unknown"
    assert select_garden_plan(_plans([("One", 50), ("Two", None)]), 100).state == "running"


def test_malformed_content_preserves_stage_but_never_invents_a_schedule() -> None:
    projection = select_garden_plan(_plans([("Flowering", 20)], content="{"), 100)
    assert projection.state == "running"
    assert projection.stage is not None and projection.stage.content_status == "invalid"

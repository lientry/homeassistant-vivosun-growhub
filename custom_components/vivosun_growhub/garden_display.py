"""Select garden-configured recipe intent without consulting device execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .gardens import GardenPlans, GardenStage

GardenStageStatus = Literal["inactive", "scheduled", "running", "ambiguous", "unknown"]


@dataclass(frozen=True, slots=True)
class GardenSelection:
    """Conservative configured stage projection for one successful plan read."""

    recipe: str
    state: GardenStageStatus
    stage: GardenStage | None


@dataclass(frozen=True, slots=True)
class GardenScheduleClock:
    """Proven zero-offset phase and wall clock in the garden's own timezone."""

    now_seconds: int
    phase_seconds: int
    timezone: str


def garden_stage_clock(
    stage: GardenStage, timezone: str, now_utc: datetime,
) -> tuple[GardenScheduleClock | None, str]:
    """Use the app's zero-timeOffset stage-start hour/minute as the slot phase.

    Nonzero timeOffset invokes app maintenance behavior and is not verified as a
    read-only schedule contract. DST transitions may require additional correction
    outside this proven path, so neither case is presented as a known schedule.
    """
    if stage.time_offset != 0:
        return None, "unverified_time_offset"
    if not stage.start_time:
        return None, "unknown_start"
    try:
        zone = ZoneInfo(timezone)
        started = datetime.fromtimestamp(stage.start_time, tz=zone)
        now = now_utc.astimezone(zone)
        for local in (started, now):
            midnight = datetime.combine(local.date(), time.min, tzinfo=zone)
            tomorrow = midnight + timedelta(days=1)
            if midnight.utcoffset() != tomorrow.utcoffset():
                return None, "unverified_dst"
    except (OverflowError, OSError, ValueError, ZoneInfoNotFoundError):
        return None, "unknown_time"
    if started.utcoffset() != now.utcoffset() or started.fold or now.fold:
        return None, "unverified_dst"
    return (
        GardenScheduleClock(
            now_seconds=now.hour * 3600 + now.minute * 60 + now.second,
            phase_seconds=started.hour * 3600 + started.minute * 60,
            timezone=timezone,
        ),
        "current",
    )


def select_garden_plan(plans: GardenPlans, now_epoch: int) -> GardenSelection:
    """Never infer a plan/stage winner from cloud ordering or a physical shadow."""
    if not plans.plans:
        return GardenSelection("No recipe", "inactive", None)
    if len(plans.plans) != 1:
        return GardenSelection("Multiple recipes", "ambiguous", None)

    plan = plans.plans[0]
    states = [(stage, stage.state_at(now_epoch)) for stage in plan.stages]
    running = [stage for stage, state in states if state == "running"]
    unknown = any(state == "unknown" for _, state in states)
    if len(running) > 1:
        return GardenSelection(plan.name, "ambiguous", None)
    if unknown:
        return GardenSelection(plan.name, "unknown", None)
    if running:
        return GardenSelection(plan.name, "running", running[0])

    scheduled = [stage for stage, state in states if state == "scheduled"]
    if scheduled:
        first_start = min(stage.start_time for stage in scheduled if stage.start_time is not None)
        next_stages = [stage for stage in scheduled if stage.start_time == first_start]
        if len(next_stages) != 1:
            return GardenSelection(plan.name, "ambiguous", None)
        return GardenSelection(plan.name, "scheduled", next_stages[0])
    return GardenSelection(plan.name, "inactive", None)

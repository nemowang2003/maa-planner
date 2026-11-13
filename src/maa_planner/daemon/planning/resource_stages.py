"""Verified Official/Bilibili resource calendars, distinct from account unlock state.

Regular weekdays/drop pairs follow MAA v6.21.3 StageManager.AddPermanentStages.
The shared StageActivityV2 snapshot only supplies the all-open event window.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from maa_planner.daemon.planning.activity import ActivitySnapshot, ActivityStatus, require_aware


@dataclass(frozen=True, slots=True)
class ChipStage:
    stage: str
    item_ids: tuple[str, str]
    weekdays: tuple[int, ...]
    open_by_calendar: bool
    reason: str


# ISO weekdays, Monday=1. The two drops are alternatives, not guaranteed together.
_CHIP_ZONES = (
    ("A", (1, 4, 5, 7), ("3261", "3231"), ("3262", "3232")),
    ("B", (1, 2, 5, 6), ("3251", "3241"), ("3252", "3242")),
    ("C", (3, 4, 6, 7), ("3211", "3271"), ("3212", "3272")),
    ("D", (2, 3, 6, 7), ("3221", "3281"), ("3222", "3282")),
)

CHIP_ITEM_IDS = tuple(
    item for _, _, first, second in _CHIP_ZONES for pair in (first, second) for item in pair
)

# MAA v6.21.3 StageManager.AddPermanentStages, ISO weekdays.
_SUPPLY_ZONES = {
    "AP": (5, (1, 4, 6, 7)),
    "CA": (5, (2, 3, 5, 7)),
    "CE": (6, (2, 4, 6, 7)),
    "LS": (6, (1, 2, 3, 4, 5, 6, 7)),
    "SK": (5, (1, 3, 5, 6)),
}


def validate_fallback_stage(stage: object) -> str:
    if not isinstance(stage, str) or not (
        re.fullmatch(r"S?[0-9]+-[1-9][0-9]*", stage)
        or re.fullmatch(r"PR-[A-D]-[12]", stage)
        or any(
            stage in {f"{zone}-{level}" for level in range(1, maximum + 1)}
            for zone, (maximum, _) in _SUPPLY_ZONES.items()
        )
    ):
        raise ValueError(f"后备关卡不支持判断开放日: {stage}；请使用主线、物资筹备或芯片关卡")
    return stage


def _all_open(snapshot: ActivitySnapshot, current: datetime) -> bool:
    event = snapshot.data.resource_collection
    return bool(
        event and event.is_resource_collection and event.status(current) == ActivityStatus.ACTIVE
    )


def stage_open_by_calendar(snapshot: ActivitySnapshot, stage: str, now: datetime) -> bool:
    validate_fallback_stage(stage)
    current = require_aware(now)
    if stage.startswith("PR-"):
        return next(
            s.open_by_calendar for s in chip_calendar(snapshot, current) if s.stage == stage
        )
    zone = stage.split("-", 1)[0]
    if zone not in _SUPPLY_ZONES:
        return True  # Permanent main-story stage; account unlock remains unknown.
    return (
        _all_open(snapshot, current)
        or (current - timedelta(hours=4)).isoweekday() in _SUPPLY_ZONES[zone][1]
    )


def chip_calendar(snapshot: ActivitySnapshot, now: datetime) -> tuple[ChipStage, ...]:
    current = require_aware(now)
    # Game dates change at 04:00 Shanghai, rather than civil midnight.
    weekday = (current - timedelta(hours=4)).isoweekday()
    all_open = _all_open(snapshot, current)
    return tuple(
        ChipStage(
            stage=f"PR-{zone}-{level}",
            item_ids=items,
            weekdays=days,
            open_by_calendar=all_open or weekday in days,
            reason="resource_collection" if all_open else "weekly_calendar",
        )
        for zone, days, first, second in _CHIP_ZONES
        for level, items in ((1, first), (2, second))
    )

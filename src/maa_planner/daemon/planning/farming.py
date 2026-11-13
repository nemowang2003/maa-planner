"""Choose activity materials, chip stock maintenance, then an open fallback stage."""

from dataclasses import dataclass
from datetime import datetime

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import (
    EFFICIENCY_PREFIX,
    ActivitySnapshot,
    efficiency_value,
)
from maa_planner.daemon.planning.resource_stages import (
    CHIP_ITEM_IDS,
    chip_calendar,
    stage_open_by_calendar,
)

# User-confirmed list; shop associations cannot establish unlimited exchange stock.
MATERIAL_BLACKLIST = frozenset(
    {"30073", "30074", "30083", "30084", "30093", "30094", "30103", "31033", "31053"}
)

DEFAULT_CHIP_TARGETS = tuple((item, 20) for item in CHIP_ITEM_IDS)


@dataclass(frozen=True, slots=True)
class FarmingDecision:
    task_name: str
    stage: str
    reason: str
    activity_key: str | None = None
    drop: str | None = None
    item_id: str | None = None
    stock: int | None = None
    target: int | None = None
    deficit: int | None = None


class FarmingDataError(ValueError):
    """An active activity recommendation cannot be used without guessing."""

    def __init__(self, activity_key: str, reason: str):
        self.activity_key = activity_key
        self.reason = reason
        super().__init__(reason)


class FallbackUnavailable(ValueError):
    """Every configured fallback stage is closed for the current game day."""


def decide_farming(
    activity: ActivitySnapshot,
    depot: dict[str, int],
    *,
    now: float,
    task_name: str,
    default_stage: str = "1-7",
    garbage_activities: tuple[str, ...] = (),
    chip_targets: tuple[tuple[str, int], ...] = DEFAULT_CHIP_TARGETS,
    fallback_stages: tuple[str, ...] | None = None,
) -> FarmingDecision:
    current = datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
    active = sorted(
        (
            story
            for story in activity.data.active_side_stories(current)
            if story.key not in garbage_activities
        ),
        key=lambda story: story.ends_at,
    )
    if not active:
        return decide_stock_farming(
            activity,
            depot,
            now=now,
            task_name=task_name,
            chip_targets=chip_targets,
            fallback_stages=fallback_stages if fallback_stages is not None else (default_stage,),
        )
    story = active[0]
    materials = [s for s in story.stages if s.drop.isascii() and s.drop.isdecimal()]
    if not materials or any(not s.value for s in materials):
        raise FarmingDataError(story.key, f"活动 {story.key} 未提供普通材料推荐关卡")
    candidates = [
        s for s in materials if s.drop not in MATERIAL_BLACKLIST and depot.get(s.drop, 0) < 100
    ]
    if candidates:
        selected = min(candidates, key=lambda s: depot.get(s.drop, 0))
        return FarmingDecision(
            task_name,
            selected.value,
            "排除黑名单后，选择库存不足 100 且库存最低的活动材料",
            story.key,
            selected.drop,
            selected.drop,
            depot.get(selected.drop, 0),
        )
    efficient = []
    for stage in story.stages:
        if not stage.drop.startswith(EFFICIENCY_PREFIX):
            continue
        value = efficiency_value(stage.drop)
        if value is None or not stage.value:
            raise FarmingDataError(story.key, f"活动 {story.key} 的搓玉效率推荐无效: {stage.drop}")
        efficient.append((stage, value))
    if not efficient:
        raise FarmingDataError(story.key, f"活动 {story.key} 未提供搓玉效率推荐关卡")
    selected, _ = max(efficient, key=lambda entry: entry[1])
    return FarmingDecision(
        task_name,
        selected.value,
        "没有需要补库存的材料，选择活动搓玉效率推荐关卡",
        story.key,
        selected.drop,
    )


def decide_stock_farming(
    activity: ActivitySnapshot,
    depot: dict[str, int],
    *,
    now: float,
    task_name: str,
    chip_targets: tuple[tuple[str, int], ...],
    fallback_stages: tuple[str, ...],
) -> FarmingDecision:
    current = datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
    stages = {
        item: s.stage
        for s in chip_calendar(activity, current)
        if s.open_by_calendar
        for item in s.item_ids
    }
    candidates = [
        (item, target, target - depot.get(item, 0))
        for item, target in chip_targets
        if item in stages and target > depot.get(item, 0)
    ]
    if candidates:
        item, target, deficit = max(candidates, key=lambda entry: entry[2])
        return FarmingDecision(
            task_name,
            stages[item],
            "今日开放的芯片本中，选择目标库存减当前库存缺口最大的芯片",
            item_id=item,
            stock=depot.get(item, 0),
            target=target,
            deficit=deficit,
        )
    for stage in fallback_stages:
        if stage_open_by_calendar(activity, stage, current):
            return FarmingDecision(
                task_name, stage, "今日没有可补的芯片缺口，按后备关卡顺序选择开放关卡"
            )
    raise FallbackUnavailable("后备关卡今日均未开放；请在 fallback_stages 末尾配置 1-7 等常驻关卡")

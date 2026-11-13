from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class InfrastMode(StrEnum):
    NORMAL = "normal"
    THREE_GOLD = "three_gold"
    ORUNDUM = "orundum"
    SELL_ONLY = "sell_only"


class ShardRecipe(StrEnum):
    ORIROCK = "orirock"
    DEVICE = "device"


_INFRAST_MODES = frozenset(InfrastMode)
_GOLD = "3003"
_SHARDS = "3141"
_LMD = "4001"
_LMD_STOP = 100_000
_GOLD_ENTER_THREE = 500
_GOLD_LEAVE_THREE = 2_000
_SHARDS_ENTER_SELL = 100
_SHARDS_LEAVE_SELL = 10
_RECIPE_LIMITS = {
    ShardRecipe.ORIROCK: ("30012", "固源岩", 200, 100),
    ShardRecipe.DEVICE: ("30062", "装置", 100, 50),
}


@dataclass(frozen=True, slots=True)
class InfrastWarning:
    """A low-material observation; the daemon owns delivery and deduplication."""

    item_id: str
    count: int
    warning_threshold: int
    stop_threshold: int


@dataclass(frozen=True, slots=True)
class InfrastDecision:
    mode: InfrastMode
    file: Path
    reason: str
    warnings: tuple[InfrastWarning, ...] = ()


def infrast(
    depot: Mapping[str, int],
    files: Mapping[str, Path],
    *,
    current_mode: InfrastMode | str,
    recipe: ShardRecipe | str | None = None,
) -> Path:
    """Return the selected native JSON path; use decide_infrast for diagnostics."""
    return decide_infrast(depot, files, current_mode=current_mode, recipe=recipe).file


def decide_infrast(
    depot: Mapping[str, int],
    files: Mapping[str, Path],
    *,
    current_mode: InfrastMode | str,
    recipe: ShardRecipe | str | None = None,
) -> InfrastDecision:
    """Choose a base plan for an explicitly expert-enabled profile, without I/O.

    The caller supplies all four files, the last successfully applied mode, and
    inventory/recipe observations from the latest fetched snapshot. Its storeTs
    may predate the HTTP request and has no maximum age; missing counts are not zero.
    Only counts needed by the selected branch are required. Orundum additionally
    requires its actual current recipe, LMD and that recipe's material count.

    No automatic transition enters orundum. Manual entry and applying/persisting
    the result belong to the caller, as do time-based subplans and notifications.
    """
    if set(files) != _INFRAST_MODES:
        raise ValueError("expert 基建必须提供 orundum、sell_only、three_gold、normal 四种配置")
    if any(not isinstance(path, Path) or not path.name for path in files.values()):
        raise ValueError("expert 基建配置必须是具体文件的 Path 路径")
    current = InfrastMode(current_mode)
    current_recipe = None if recipe is None else ShardRecipe(recipe)

    def select(
        mode: InfrastMode, reason: str, warnings: tuple[InfrastWarning, ...] = ()
    ) -> InfrastDecision:
        return InfrastDecision(mode=mode, file=files[mode], reason=reason, warnings=warnings)

    if current is InfrastMode.ORUNDUM:
        if current_recipe is None:
            raise ValueError("搓玉模式必须提供当前实际使用的碎片配方，不能推测固源岩或装置")
        item_id, material, warning_at, stop_at = _RECIPE_LIMITS[current_recipe]
        count = _depot_count(depot, item_id)
        lmd = _depot_count(depot, _LMD)
        warnings = (
            (InfrastWarning(item_id, count, warning_at, stop_at),) if count < warning_at else ()
        )
        exits = []
        if lmd < _LMD_STOP:
            exits.append(f"龙门币 {lmd} < {_LMD_STOP}")
        if count < stop_at:
            exits.append(f"{material} {count} < {stop_at}")
        if not exits:
            return select(current, "龙门币和当前配方材料充足，继续搓玉", warnings)

        reason = "；".join(exits)
        shards = _depot_count(depot, _SHARDS)
        if shards > _SHARDS_ENTER_SELL:
            return select(
                InfrastMode.SELL_ONLY,
                f"{reason}；停止搓玉，源石碎片 {shards} > {_SHARDS_ENTER_SELL}，只卖不搓",
                warnings,
            )
        gold = _depot_count(depot, _GOLD)
        return select(
            _gold_entry(gold), f"{reason}；停止搓玉，按赤金数量 {gold} 选择基建", warnings
        )

    shards = _depot_count(depot, _SHARDS)
    if shards > _SHARDS_ENTER_SELL:
        return select(
            InfrastMode.SELL_ONLY,
            f"非搓玉模式下源石碎片 {shards} > {_SHARDS_ENTER_SELL}，只卖不搓",
        )
    if current is InfrastMode.SELL_ONLY and shards >= _SHARDS_LEAVE_SELL:
        return select(current, f"源石碎片 {shards} >= {_SHARDS_LEAVE_SELL}，继续只卖不搓")

    gold = _depot_count(depot, _GOLD)
    if current is InfrastMode.SELL_ONLY:
        return select(
            _gold_entry(gold),
            f"源石碎片 {shards} < {_SHARDS_LEAVE_SELL}，按赤金数量 {gold} 选择基建",
        )
    if current is InfrastMode.NORMAL and gold < _GOLD_ENTER_THREE:
        return select(InfrastMode.THREE_GOLD, f"赤金 {gold} < {_GOLD_ENTER_THREE}，转三赤金")
    if current is InfrastMode.THREE_GOLD and gold > _GOLD_LEAVE_THREE:
        return select(InfrastMode.NORMAL, f"赤金 {gold} > {_GOLD_LEAVE_THREE}，转两赤金")
    return select(current, "赤金数量未触发切换阈值，保持当前基建")


def _gold_entry(gold: int) -> InfrastMode:
    return InfrastMode.THREE_GOLD if gold < _GOLD_ENTER_THREE else InfrastMode.NORMAL


def _depot_count(depot: Mapping[str, int], item_id: str) -> int:
    try:
        count = depot[item_id]
    except KeyError as error:
        raise ValueError(f"缺少物品 {item_id} 的库存；已确认无库存时请显式传入 0") from error
    if type(count) is not int or count < 0:
        raise ValueError(f"物品 {item_id} 的库存必须是非负整数")
    return count

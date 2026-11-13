from pathlib import Path

import pytest

from maa_planner.daemon.planning.expert import (
    InfrastMode,
    InfrastWarning,
    ShardRecipe,
    decide_infrast,
    infrast,
)


def plans(root: Path) -> dict[str, Path]:
    return {
        "orundum": root / "搓玉.json",
        "sell_only": root / "只卖不搓.json",
        "three_gold": root / "三赤金.json",
        "normal": root / "两赤金.json",
    }


@pytest.fixture
def files(tmp_path):
    return plans(tmp_path)


@pytest.fixture
def depot():
    return {"30012": 1000, "30062": 500, "3003": 1000, "3141": 0, "4001": 100000}


@pytest.mark.parametrize("missing", ["orundum", "sell_only", "three_gold", "normal"])
def test_infrast_expert_rejects_incomplete_modes(files, missing):
    del files[missing]
    with pytest.raises(ValueError, match="四种配置"):
        infrast({}, files, current_mode="normal")


def test_infrast_expert_rejects_unknown_mode(tmp_path):
    files = plans(tmp_path)
    files["fallback"] = tmp_path / "fallback.json"
    with pytest.raises(ValueError, match="四种配置"):
        infrast({}, files, current_mode="normal")


@pytest.mark.parametrize("invalid", [Path(), "搓玉.json", None])
def test_infrast_expert_requires_file_paths(files, invalid):
    files["orundum"] = invalid
    with pytest.raises(ValueError, match="具体文件"):
        infrast({}, files, current_mode="normal")


@pytest.mark.parametrize(
    ("current", "gold", "expected"),
    [
        ("normal", 0, "three_gold"),
        ("normal", 499, "three_gold"),
        ("normal", 500, "normal"),
        ("normal", 2000, "normal"),
        ("normal", 2001, "normal"),
        ("three_gold", 0, "three_gold"),
        ("three_gold", 499, "three_gold"),
        ("three_gold", 500, "three_gold"),
        ("three_gold", 2000, "three_gold"),
        ("three_gold", 2001, "normal"),
    ],
)
def test_gold_hysteresis_and_strict_boundaries(files, depot, current, gold, expected):
    depot.update({"3003": gold, "3141": 100, "4001": 0})
    decision = decide_infrast(depot, files, current_mode=current)
    assert decision.mode == expected
    assert decision.file == files[expected]
    assert decision.reason
    assert decision.warnings == ()


@pytest.mark.parametrize("current", ["normal", "three_gold", "sell_only"])
@pytest.mark.parametrize("gold", [0, 2001])
def test_shards_take_priority_over_gold_and_lmd_outside_orundum(files, depot, current, gold):
    depot.update({"3141": 101, "3003": gold, "4001": 0})
    assert infrast(depot, files, current_mode=current) == files["sell_only"]


@pytest.mark.parametrize("shards", [10, 50, 100, 101])
@pytest.mark.parametrize("gold", [0, 2001])
def test_sell_only_stays_until_strictly_below_ten(files, depot, shards, gold):
    depot.update({"3141": shards, "3003": gold, "4001": 0})
    decision = decide_infrast(depot, files, current_mode="sell_only")
    assert decision.mode is InfrastMode.SELL_ONLY
    assert decision.warnings == ()


@pytest.mark.parametrize("shards", [0, 9])
@pytest.mark.parametrize(("gold", "expected"), [(499, "three_gold"), (500, "normal")])
def test_sell_only_returns_to_gold_by_entry_threshold(files, depot, shards, gold, expected):
    depot.update({"3141": shards, "3003": gold, "4001": 0})
    assert infrast(depot, files, current_mode="sell_only") == files[expected]


@pytest.mark.parametrize(
    ("recipe", "item_id", "minimum"), [("orirock", "30012", 100), ("device", "30062", 50)]
)
@pytest.mark.parametrize("shards", [0, 101, 10000])
def test_healthy_orundum_ignores_shards_gold_and_unused_material(
    files, depot, recipe, item_id, minimum, shards
):
    depot.update({"30012": 0, "30062": 0, "3003": 0, "3141": shards})
    depot[item_id] = minimum
    decision = decide_infrast(depot, files, current_mode="orundum", recipe=recipe)
    assert decision.mode is InfrastMode.ORUNDUM
    assert decision.file == files["orundum"]


@pytest.mark.parametrize(
    ("recipe", "rock", "device", "lmd", "cause"),
    [
        ("orirock", 99, 1000, 100000, "固源岩"),
        ("device", 1000, 49, 100000, "装置"),
        ("orirock", 1000, 1000, 99999, "龙门币"),
        ("device", 1000, 1000, 99999, "龙门币"),
        ("orirock", 99, 1000, 99999, "固源岩"),
        ("device", 1000, 49, 99999, "装置"),
    ],
)
@pytest.mark.parametrize(
    ("shards", "gold", "expected"),
    [
        (101, 0, "sell_only"),
        (101, 2001, "sell_only"),
        (100, 499, "three_gold"),
        (100, 500, "normal"),
    ],
)
def test_orundum_stops_for_either_lmd_or_active_material_and_routes_by_stock(
    files, depot, recipe, rock, device, lmd, cause, shards, gold, expected
):
    depot.update({"30012": rock, "30062": device, "4001": lmd, "3141": shards, "3003": gold})
    decision = decide_infrast(depot, files, current_mode="orundum", recipe=recipe)
    assert decision.mode == expected
    assert decision.file == files[expected]
    assert cause in decision.reason


@pytest.mark.parametrize(
    ("recipe", "item_id", "count", "warning_at", "stop_at", "warns"),
    [
        ("orirock", "30012", 200, 200, 100, False),
        ("orirock", "30012", 199, 200, 100, True),
        ("orirock", "30012", 100, 200, 100, True),
        ("orirock", "30012", 99, 200, 100, True),
        ("device", "30062", 100, 100, 50, False),
        ("device", "30062", 99, 100, 50, True),
        ("device", "30062", 50, 100, 50, True),
        ("device", "30062", 49, 100, 50, True),
    ],
)
def test_recipe_specific_warnings_include_counts_and_both_thresholds(
    files, depot, recipe, item_id, count, warning_at, stop_at, warns
):
    depot[item_id] = count
    decision = decide_infrast(depot, files, current_mode="orundum", recipe=recipe)
    expected = (InfrastWarning(item_id, count, warning_at, stop_at),) if warns else ()
    assert decision.warnings == expected


def test_manual_recipe_switch_gives_time_to_continue_then_stops_without_auto_resume(files, depot):
    depot.update({"30012": 150, "30062": 100})
    decision = decide_infrast(
        depot, files, current_mode=InfrastMode.ORUNDUM, recipe=ShardRecipe.ORIROCK
    )
    assert decision.mode is InfrastMode.ORUNDUM
    assert decision.warnings == (InfrastWarning("30012", 150, 200, 100),)

    # A manual switch changes only which ingredient is checked; no automatic switch occurs.
    depot.update({"30012": 1, "30062": 100})
    decision = decide_infrast(depot, files, current_mode=decision.mode, recipe=ShardRecipe.DEVICE)
    assert decision.mode is InfrastMode.ORUNDUM
    assert decision.warnings == ()
    depot["30062"] = 80
    decision = decide_infrast(depot, files, current_mode=decision.mode, recipe=ShardRecipe.DEVICE)
    assert decision.mode is InfrastMode.ORUNDUM
    assert decision.warnings == (InfrastWarning("30062", 80, 100, 50),)

    depot.update({"30062": 49, "3141": 101, "3003": 499})
    decision = decide_infrast(depot, files, current_mode=decision.mode, recipe=ShardRecipe.DEVICE)
    assert decision.mode is InfrastMode.SELL_ONLY
    depot.update({"30012": 1000, "30062": 1000, "4001": 0})
    for shards in (100, 10):
        depot["3141"] = shards
        decision = decide_infrast(depot, files, current_mode=decision.mode)
        assert decision.mode is InfrastMode.SELL_ONLY

    depot["3141"] = 9
    decision = decide_infrast(depot, files, current_mode=decision.mode)
    assert decision.mode is InfrastMode.THREE_GOLD
    depot.update({"4001": 100000, "3003": 2000})
    decision = decide_infrast(depot, files, current_mode=decision.mode)
    assert decision.mode is InfrastMode.THREE_GOLD
    depot["3003"] = 2001
    decision = decide_infrast(depot, files, current_mode=decision.mode)
    assert decision.mode is InfrastMode.NORMAL


@pytest.mark.parametrize("current", ["normal", "three_gold", "sell_only"])
def test_abundant_materials_and_lmd_never_automatically_enter_orundum(files, depot, current):
    depot.update({"30012": 100000, "30062": 100000, "4001": 10000000})
    assert decide_infrast(depot, files, current_mode=current).mode is not InfrastMode.ORUNDUM


@pytest.mark.parametrize(
    ("current", "recipe", "stock", "expected"),
    [
        ("normal", None, {"3141": 101}, "sell_only"),
        ("sell_only", None, {"3141": 10}, "sell_only"),
        ("normal", None, {"3141": 0, "3003": 500}, "normal"),
        ("orundum", "orirock", {"30012": 100, "4001": 100000}, "orundum"),
        ("orundum", "device", {"30062": 50, "4001": 100000}, "orundum"),
        ("orundum", "device", {"30062": 49, "4001": 100000, "3141": 101}, "sell_only"),
    ],
)
def test_only_reads_inventory_relevant_to_the_decision(files, current, recipe, stock, expected):
    assert infrast(stock, files, current_mode=current, recipe=recipe) == files[expected]


@pytest.mark.parametrize(
    ("current", "recipe", "missing"),
    [
        ("normal", None, "3141"),
        ("normal", None, "3003"),
        ("sell_only", None, "3003"),
        ("orundum", "orirock", "30012"),
        ("orundum", "device", "30062"),
        ("orundum", "device", "4001"),
    ],
)
def test_missing_required_stock_is_never_assumed_zero(files, depot, current, recipe, missing):
    del depot[missing]
    with pytest.raises(ValueError, match=f"缺少物品 {missing}"):
        infrast(depot, files, current_mode=current, recipe=recipe)


@pytest.mark.parametrize("invalid", [-1, True, "100", 100.0, None])
def test_rejects_invalid_stock_counts(files, depot, invalid):
    depot["3141"] = invalid
    with pytest.raises(ValueError, match="非负整数"):
        infrast(depot, files, current_mode="normal")


def test_orundum_requires_actual_recipe_instead_of_guessing(files, depot):
    with pytest.raises(ValueError, match="当前实际使用"):
        infrast(depot, files, current_mode="orundum")


def test_rejects_unknown_modes_and_recipes(files, depot):
    with pytest.raises(ValueError, match="InfrastMode"):
        infrast(depot, files, current_mode="unknown")
    with pytest.raises(ValueError, match="ShardRecipe"):
        infrast(depot, files, current_mode="orundum", recipe="unknown")


def test_decision_does_not_mutate_inventory_or_file_configuration(files, depot):
    original_depot, original_files = depot.copy(), files.copy()
    decide_infrast(depot, files, current_mode="orundum", recipe="orirock")
    assert depot == original_depot
    assert files == original_files

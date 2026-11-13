import json
from dataclasses import replace
from datetime import datetime

import pytest

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.daily import (
    build_daily_plan,
    observe_infrast,
    select_plan,
)
from maa_planner.daemon.planning.expert import InfrastMode, ShardRecipe
from maa_planner.daemon.planning.schedule import parse_schedules
from tests.helpers.planning import NOW, skland_base, warehouse


@pytest.mark.parametrize("mode", ["normal", "three_gold", "sell_only", "orundum"])
def test_observation_recognizes_all_four_actual_production_modes(mode):
    observation = observe_infrast(warehouse(), skland_base(mode), now=NOW)
    assert observation.mode == mode
    assert observation.depot["30012"] == 150
    assert observation.recipe == (ShardRecipe.ORIROCK if mode == "orundum" else None)


def test_device_recipe_is_read_from_formula_costs_and_missing_items_are_not_zero():
    observation = observe_infrast({"items": []}, skland_base(ingredient="30062"), now=NOW)
    assert observation.recipe is ShardRecipe.DEVICE
    assert observation.depot == {}


@pytest.mark.parametrize(
    "timestamp", [None, NOW + 1, True, -1, 0, float("nan"), float("inf"), "123"]
)
def test_missing_and_invalid_snapshot_timestamps_are_rejected(timestamp):
    player = skland_base()
    player["status"]["storeTs"] = timestamp
    with pytest.raises(ValueError, match="快照"):
        observe_infrast(warehouse(), player, now=NOW)


@pytest.mark.parametrize("count", [True, -1, "-1", "1.5", None])
def test_bad_inventory_counts_are_not_coerced_to_zero(count):
    with pytest.raises(ValueError, match="数量"):
        observe_infrast({"items": [{"id": "30012", "count": count}]}, skland_base(), now=NOW)


def test_unknown_layout_recipe_and_incomplete_inventory_are_not_guessed():
    player = skland_base()
    player["building"]["manufactures"].pop()
    with pytest.raises(ValueError, match="四种"):
        observe_infrast(warehouse(), player, now=NOW)
    with pytest.raises(ValueError, match="仓库数据缺失"):
        observe_infrast({}, skland_base(), now=NOW)
    with pytest.raises(ValueError, match="配方"):
        observe_infrast(warehouse(), skland_base(ingredient="unknown"), now=NOW)


PERIODS = [
    ["19:00", "22:59"],
    ["02:00", "07:59"],
    ["11:00", "14:59"],
    ["15:00", "18:59"],
    ["23:00", "01:59"],
    ["08:00", "10:59"],
]


@pytest.mark.parametrize(
    ("time", "expected"),
    [
        ("00:00", 4),
        ("01:59", 4),
        ("02:00", 1),
        ("07:59", 1),
        ("08:00", 5),
        ("10:59", 5),
        ("11:00", 2),
        ("14:59", 2),
        ("15:00", 3),
        ("18:59", 3),
        ("19:00", 0),
        ("22:59", 0),
        ("23:00", 4),
        ("23:59", 4),
    ],
)
def test_native_period_boundaries_include_the_whole_last_minute(time, expected):
    hour, minute = map(int, time.split(":"))
    local = datetime(2026, 11, 2, hour, minute, 59, tzinfo=BUSINESS_TIMEZONE)
    document = {
        "plans": [{"period": [period], "name": "ignore obsolete name"} for period in PERIODS]
    }
    assert select_plan(document, local) == expected


def test_auto_plan_rejects_gaps_overlap_and_unknown_periods():
    local = datetime.fromtimestamp(NOW, BUSINESS_TIMEZONE)
    for plans in (
        [],
        [{}, {}],
        [{"period": [["00:00", "01:00"]]}],
        [{"period": [["00:00", "23:59"]]}, {"period": [["12:00", "13:00"]]}],
    ):
        with pytest.raises(ValueError):
            select_plan({"plans": plans}, local)
    assert select_plan({"plans": [{}]}, local) == 0
    assert select_plan({"plans": [{}, {}]}, local, 1) == 1
    with pytest.raises(ValueError, match="范围"):
        select_plan({"plans": [{}]}, local, 1)


def configured(tmp_path, *, expert=False):
    sources = {}
    for mode in InfrastMode:
        path = tmp_path / f"{mode}.json"
        path.write_text(json.dumps({"plans": [{"name": mode, "rooms": {}}]}))
        sources[mode.value] = str(path)
    task = {"type": "Infrast", "name": "基建", "params": {"threshold": 0.37, "replenish": True}}
    task.update({"strategy": "expert", "files": sources} if expert else {"file": sources["normal"]})
    schedule = parse_schedules(
        [
            {
                "id": "daily",
                "profiles": ["a"],
                "times": ["12:10"],
                "client_type": "Official",
                "session": {"startup": {"start_game_enabled": True}, "closedown": {}},
                "tasks": [
                    task,
                    {"type": "Fight", "params": {"stage": "1-7", "medicine": 0, "stone": 0}},
                ],
            }
        ],
        {"a"},
        tmp_path / "config.toml",
    )[0]
    return schedule


def test_fixed_tasks_keep_order_params_and_immutable_file_contents(tmp_path):
    schedule = configured(tmp_path)
    plan = build_daily_plan(schedule, now=NOW)
    assert plan.tasks is not None
    doc = json.loads(plan.tasks.document)
    assert [t["type"] for t in doc["tasks"]] == ["StartUp", "Infrast", "Fight", "CloseDown"]
    assert doc["tasks"][2]["params"] == {"stage": "1-7", "medicine": 0, "stone": 0, "series": 0}
    assert doc["tasks"][1]["params"] == {
        "threshold": 0.37,
        "replenish": True,
        "mode": 10000,
        "plan_index": 0,
        "filename": "@file:task-0.json",
    }
    assert plan.decisions == ()
    schedule.tasks[0].file.write_text('{"plans": []}')
    assert json.loads(plan.tasks.attachments)["task-0.json"]["plans"][0]["name"] == "normal"


def test_expert_uses_observed_recipe_returns_warning_and_later_selects_sell_only(tmp_path):
    schedule = configured(tmp_path, expert=True)
    observation = observe_infrast(warehouse(), skland_base(), now=NOW)
    plan = build_daily_plan(schedule, now=NOW, observation=observation)
    assert plan.decisions[0].mode is InfrastMode.ORUNDUM
    assert plan.decisions[0].warnings[0].item_id == "30012"
    observation = replace(
        observation, recipe=ShardRecipe.DEVICE, depot=observation.depot | {"30062": 49}
    )
    plan = build_daily_plan(schedule, now=NOW, observation=observation)
    assert plan.decisions[0].mode is InfrastMode.SELL_ONLY
    assert plan.tasks is not None
    assert json.loads(plan.tasks.attachments)["task-0.json"]["plans"][0]["name"] == "sell_only"


def test_expert_does_not_run_without_observation(tmp_path):
    schedule = configured(tmp_path, expert=True)
    with pytest.raises(ValueError, match="快照"):
        build_daily_plan(schedule, now=NOW)


@pytest.mark.parametrize("timestamp", [1, NOW - 3 * 3600, NOW - 7 * 86400])
def test_expert_accepts_old_store_ts_and_keeps_it_as_metadata(tmp_path, timestamp):
    schedule = configured(tmp_path, expert=True)
    player = skland_base()
    player["status"]["storeTs"] = timestamp
    observation = observe_infrast(warehouse(), player, now=NOW)
    assert observation.store_ts == timestamp
    plan = build_daily_plan(schedule, now=NOW, observation=observation)
    assert plan.tasks is not None
    assert plan.decisions[0].mode is InfrastMode.ORUNDUM


def test_weekdays_and_disabled_tasks_are_resolved_before_runner(tmp_path):
    schedule = configured(tmp_path)
    tasks = tuple(replace(task, weekdays=(2,)) for task in schedule.tasks)
    assert build_daily_plan(replace(schedule, tasks=tasks), now=NOW).tasks is None


def test_daily_custom_resources_are_fixed_and_conflicting_nodes_rejected(tmp_path):
    from maa_planner.daemon.planning.schedule import DailySchedule, DailyTask

    resource = tmp_path / "nodes.json"
    original = {"Planner@Test": {"algorithm": "JustReturn"}}
    resource.write_text(json.dumps(original))
    task = DailyTask("Custom", "custom", '{"task_names":["Planner@Test"]}', resource_file=resource)
    schedule = DailySchedule("daily", ("nemo",), (720,), (task, task))
    plan = build_daily_plan(schedule, now=NOW)
    resource.write_text('{"Planner@Test":{"algorithm":"OcrDetect"}}')
    assert plan.tasks is not None
    assert json.loads(plan.tasks.document)["resource_tasks"] == original

    other = tmp_path / "other.json"
    other.write_text(json.dumps(original))
    conflicting = replace(schedule, tasks=(task, replace(task, resource_file=other)))
    with pytest.raises(ValueError, match="冲突"):
        build_daily_plan(conflicting, now=NOW)


def test_private_images_frozen_before_source_changes_and_duplicates_share_attachment(tmp_path):
    from maa_planner.daemon.execution.png import decode_png
    from tests.helpers.png import PNG

    image = tmp_path / "entry.png"
    image.write_bytes(PNG)
    schedule = parse_schedules(
        [
            {
                "id": "images",
                "profiles": ["a"],
                "times": ["12:00"],
                "tasks": [
                    {
                        "type": "Custom",
                        "name": "entry",
                        "resource_images": {"Planner@Entry.png": "entry.png"},
                    }
                ],
            }
        ],
        {"a"},
        tmp_path / "config.toml",
    )[0]
    assert schedule.tasks[0].name == "entry"
    plan = build_daily_plan(replace(schedule, tasks=schedule.tasks * 2), now=NOW)
    assert plan.tasks is not None
    image.write_bytes(b"changed")
    files = json.loads(plan.tasks.attachments)
    assert len(files) == 1
    assert decode_png(next(iter(files.values()))) == PNG

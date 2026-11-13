import json

import pytest

from maa_planner.daemon.planning.clearance import prepare_clearance
from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.planning.schedule import parse_schedules
from tests.helpers.clearance import NOW, activity, catalog, clearance


def test_targets_are_paired_normal_raid_and_missing_ledger_is_not_precleared():
    plan, files, nodes = clearance()
    assert [(e["stage_id"], e["is_raid"]) for e in plan["targets"]] == [
        ("act_01", False),
        ("act_01", True),
        ("act_02", False),
        ("act_02", True),
    ]
    assert plan["completed"] == []
    assert plan["required"] == [
        ["act_01", False],
        ["act_01", True],
        ["act_02", False],
        ["act_02", True],
    ]
    assert plan["catalog_complete"] is True
    assert all(name.startswith("Planner@") for name in nodes)
    assert len(files) == 2


def test_daily_inserts_before_regular_fight_after_annihilation_and_freezes_sources(tmp_path):
    schedule = parse_schedules(
        [
            {
                "id": "daily",
                "profiles": ["nemo"],
                "times": ["12:00"],
                "session": {"startup": {}},
                "tasks": [
                    {"type": "Fight", "params": {"stage": "Annihilation", "times": 1}},
                    {"type": "Fight", "params": {"stage": "1-7", "times": 1}},
                    {"type": "Award"},
                ],
            }
        ],
        {"nemo"},
        tmp_path / "config.toml",
    )[0]
    prepared = clearance()
    plan = build_daily_plan(schedule, now=NOW, clearance=prepared)
    assert plan.tasks is not None
    document = json.loads(plan.tasks.document)
    assert document["activity_copilot"]["before_task"] == 2
    assert [t["type"] for t in document["tasks"]] == ["StartUp", "Fight", "Fight", "Award"]
    assert "clearance-1.json" in json.loads(plan.tasks.attachments)
    prepared[1].clear()
    assert "clearance-1.json" in json.loads(plan.tasks.attachments)


def test_closed_or_mismatched_activity_never_becomes_an_executable_plan():
    scripts, data = catalog()
    with pytest.raises(ValueError, match="当前开放"):
        prepare_clearance(scripts, data, activity(), now=3000)
    data["activity_table"]["basicInfo"]["event"]["name"] = "other"
    with pytest.raises(ValueError, match="当前开放"):
        prepare_clearance(scripts, data, activity(), now=NOW)


def test_current_rerun_uses_its_own_account_activity_namespace():
    scripts, data = catalog()
    basic = data["activity_table"]["basicInfo"]
    basic["event"]["endTime"] = 900
    basic["rerun"] = {"id": "rerun", "name": "测试活动", "startTime": 1000, "endTime": 3000}
    stages = data["stage_table"]["stages"]
    for key, stage in list(stages.items()):
        new = "rerun_" + key
        stages[new] = {
            **stage,
            "stageId": new,
            "zoneId": "rerun_zone",
            "hardStagedId": "rerun_" + stage["hardStagedId"] if stage["hardStagedId"] else None,
            "unlockCondition": [
                {**c, "stageId": "rerun_" + c["stageId"]} for c in stage["unlockCondition"]
            ],
        }
    data["activity_table"]["zoneToActivity"]["rerun_zone"] = "rerun"
    data["zone_table"]["zones"]["rerun_zone"] = {"zoneNameSecond": "区域"}
    plan, _, _ = prepare_clearance(scripts, data, activity(), now=NOW)
    assert plan["activity_id"] == "rerun"
    assert plan["targets"][0]["stage_id"] == "rerun_act_01"
    assert plan["targets"][0]["script_stage_id"] == "act_01"


def test_optional_reward_metadata_cannot_break_battle_navigation_preparation():
    scripts, data = catalog()
    data["activity_table"]["missionData"] = None
    plan, _, _ = prepare_clearance(scripts, data, activity(), now=NOW)
    assert len(plan["targets"]) == 4

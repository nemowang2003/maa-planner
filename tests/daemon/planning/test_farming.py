import json
from dataclasses import replace
from datetime import timedelta

import pytest

from maa_planner.daemon.planning.annihilation import WeeklyProgress
from maa_planner.daemon.planning.daily import build_daily_plan, observe_depot
from maa_planner.daemon.planning.farming import decide_farming
from maa_planner.daemon.planning.schedule import parse_schedules
from tests.helpers.planning import NOW, farming_activity


def choose(depot, activity=None, **kwargs):
    return decide_farming(
        activity or farming_activity(),
        depot,
        now=NOW,
        task_name="刷理智",
        default_stage="1-7",
        **kwargs,
    )


def test_lowest_inventory_and_missing_individual_item_zero():
    decision = choose({"31113": 99, "31083": 12, "31043": 35})
    assert (decision.stage, decision.item_id, decision.stock) == ("TL-8", "31083", 12)
    decision = choose(
        observe_depot({"items": [{"id": "31113", "count": "2"}, {"id": "31043", "count": 3}]})
    )
    assert decision.stage == "TL-8"
    assert decision.stock == 0


@pytest.mark.parametrize(
    "blacklisted", ["30073", "30074", "30083", "30084", "30093", "30094", "30103", "31033", "31053"]
)
def test_confirmed_blacklist_is_excluded_even_when_empty(blacklisted):
    decision = choose(
        {"31043": 20, "31113": 100}, farming_activity((blacklisted, "31043", "31113"))
    )
    assert decision.stage == "TL-8"


def test_threshold_is_strict_and_no_candidate_uses_efficiency_text():
    decision = choose({"31113": 100, "31083": 120, "31043": 100})
    assert (decision.stage, decision.drop) == ("TL-4", "搓玉效率0.69")
    assert decision.item_id is None and decision.stock is None
    assert choose({}, farming_activity(("30073", "30083", "30093"))).stage == "TL-4"
    assert choose({"31113": 100, "31083": 100, "31043": 99}).stage == "TL-7"


def test_equal_inventory_uses_first_recommendation():
    assert choose({"31113": 20, "31083": 20, "31043": 30}).stage == "TL-9"
    assert choose(observe_depot({"items": []})).stage == "TL-9"


@pytest.mark.parametrize("offset", [-1, 21 * 86400])
def test_activity_window_is_half_open(offset):
    decision = decide_farming(
        farming_activity(), {}, now=NOW + offset, task_name="刷理智", default_stage="CE-6"
    )
    assert decision.stage == "PR-A-1" and decision.activity_key is None


def test_explicit_garbage_activity_is_excluded():
    assert choose({}, garbage_activities=("TL",)).stage == "PR-A-1"


@pytest.mark.parametrize(
    "garbage,expected",
    [((), "EARLY"), (("EARLY",), "TL"), (("EARLY", "TL"), None)],
)
def test_overlapping_activities_use_earliest_end_and_skip_garbage(garbage, expected):
    activity = farming_activity()
    story = activity.data.side_stories[0]
    early = replace(story, key="EARLY", ends_at=story.ends_at - timedelta(days=1))
    activity = replace(activity, data=replace(activity.data, side_stories=(story, early)))
    assert choose({}, activity, garbage_activities=garbage).activity_key == expected


def test_equal_activity_deadlines_preserve_source_order():
    activity = farming_activity()
    story = activity.data.side_stories[0]
    activity = replace(
        activity, data=replace(activity.data, side_stories=(story, replace(story, key="OTHER")))
    )
    assert choose({}, activity).activity_key == "TL"


def test_missing_efficiency_is_an_error():
    activity = farming_activity()
    story = activity.data.side_stories[0]
    incomplete = replace(story, stages=story.stages[:3])
    activity = replace(activity, data=replace(activity.data, side_stories=(incomplete,)))
    with pytest.raises(ValueError, match="搓玉效率"):
        choose({"31113": 100, "31083": 100, "31043": 100}, activity)


def test_material_recommendation_count_is_not_fixed_to_three():
    activity = farming_activity()
    story = activity.data.side_stories[0]
    four_materials = replace(
        story,
        stages=story.stages[:3]
        + (replace(story.stages[2], value="TL-6", drop="31063"),)
        + story.stages[3:],
    )
    activity = replace(activity, data=replace(activity.data, side_stories=(four_materials,)))
    decision = choose({"31113": 100, "31083": 100, "31043": 100, "31063": 7}, activity)
    assert (decision.stage, decision.item_id) == ("TL-6", "31063")

    two_materials = replace(story, stages=story.stages[:2] + story.stages[3:])
    activity = replace(activity, data=replace(activity.data, side_stories=(two_materials,)))
    assert choose({"31113": 100, "31083": 4}, activity).stage == "TL-8"


def test_highest_numeric_efficiency_wins_and_ties_use_first_recommendation():
    activity = farming_activity()
    story = activity.data.side_stories[0]
    recommendation = story.stages[-1]
    story = replace(
        story,
        stages=story.stages
        + tuple(
            replace(recommendation, value=code, drop="搓玉效率" + efficiency)
            for code, efficiency in (("LOW", "2"), ("BEST", "10"), ("TIE", "10.0"))
        ),
    )
    activity = replace(activity, data=replace(activity.data, side_stories=(story,)))
    decision = choose({"31113": 100, "31083": 100, "31043": 100}, activity)
    assert (decision.stage, decision.drop) == ("BEST", "搓玉效率10")


@pytest.mark.parametrize("description", ["搓玉效率未知", "搓玉效率NaN", "搓玉效率-1", "搓玉效率0"])
def test_invalid_efficiency_is_not_guessed(description):
    activity = farming_activity()
    story = activity.data.side_stories[0]
    story = replace(story, stages=story.stages[:3] + (replace(story.stages[-1], drop=description),))
    activity = replace(activity, data=replace(activity.data, side_stories=(story,)))
    with pytest.raises(ValueError, match="搓玉效率"):
        choose({"31113": 100, "31083": 100, "31043": 100}, activity)


@pytest.mark.parametrize(
    "cultivate",
    [
        {},
        {"items": None},
        {"items": [{}]},
        {"items": [{"id": "x", "count": -1}]},
        {"items": [{"id": "x", "count": True}]},
        {"items": [{"id": "x", "count": "1.2"}]},
        {"items": [{"id": "x", "count": 1}, {"id": "x", "count": 2}]},
    ],
)
def test_invalid_inventory_cannot_be_used_as_zero(cultivate):
    with pytest.raises(ValueError, match="仓库"):
        observe_depot(cultivate)


def test_daily_plan_selects_activity_stage_and_preserves_explicit_consumption(tmp_path):
    row = {
        "id": "daily",
        "profiles": ["a"],
        "times": ["12:00"],
        "tasks": [
            {
                "type": "Fight",
                "strategy": "expert",
                "params": {"stage": "Annihilation", "times": 5},
            },
            {
                "type": "Fight",
                "strategy": "expert",
                "params": {"stage": "1-7", "times": 30, "medicine": 0, "stone": 0},
            },
        ],
    }
    schedule = parse_schedules([row], {"a"}, tmp_path / "config.toml")[0]
    with pytest.raises(ValueError, match="仓库"):
        build_daily_plan(schedule, now=NOW, activity=farming_activity())
    plan = build_daily_plan(
        schedule,
        now=NOW,
        activity=farming_activity(),
        depot={"31083": 20, "31043": 30},
        weekly_progress=WeeklyProgress(1800, 1800, NOW),
    )
    assert plan.tasks is not None
    assert json.loads(plan.tasks.document)["tasks"] == [
        {
            "name": "Fight",
            "type": "Fight",
            "params": {"stage": "TL-9", "series": 0, "medicine": 0, "stone": 0},
        }
    ]
    assert plan.farming[0].stage == "TL-9"


@pytest.mark.parametrize("value", [True, "TL", ["TL", "TL"], [1], [""]])
def test_garbage_configuration_rejects_malformed_values(tmp_path, value):
    row = {
        "id": "daily",
        "profiles": ["a"],
        "times": ["12:00"],
        "tasks": [
            {
                "type": "Fight",
                "strategy": "expert",
                "params": {"stage": "1-7"},
                "garbage_activities": value,
            }
        ],
    }
    with pytest.raises(ValueError, match="garbage_activities"):
        parse_schedules([row], {"a"}, tmp_path / "config.toml")

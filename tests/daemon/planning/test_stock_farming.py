import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ResourceCollection
from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.planning.farming import DEFAULT_CHIP_TARGETS, decide_farming
from maa_planner.daemon.planning.schedule import parse_schedules
from tests.helpers.activity import empty_activity
from tests.helpers.planning import NOW, farming_activity


def choose(
    depot, *, activity=None, targets=DEFAULT_CHIP_TARGETS, fallback=("1-7",), now=NOW, garbage=()
):
    return decide_farming(
        activity or empty_activity(),
        depot,
        now=now,
        task_name="理智作战",
        chip_targets=targets,
        fallback_stages=fallback,
        garbage_activities=garbage,
    )


def full_depot():
    return dict(DEFAULT_CHIP_TARGETS)


def test_largest_absolute_deficit_among_open_stages_including_secondary_chips():
    depot = full_depot() | {"3261": 19, "3232": 7, "3211": 0}
    decision = choose(depot)
    # Monday: PR-C is closed despite having a larger shortage.
    assert decision.stage == "PR-A-2"
    assert (decision.item_id, decision.stock, decision.target, decision.deficit) == (
        "3232",
        7,
        20,
        13,
    )


def test_each_alternative_drop_counts_individually_and_ties_follow_target_order():
    depot = full_depot() | {"3261": 100, "3231": 0, "3251": 0}
    assert choose(depot).item_id == "3231"
    assert choose(depot, targets=(("3251", 20), ("3231", 20))).stage == "PR-B-1"
    # Successful inventory without this ID means zero, not unknown.
    del depot["3231"]
    assert choose(depot).stock == 0


def test_targets_use_absolute_shortage_not_percent_and_can_disable_individual_chips():
    assert choose({"3261": 10, "3251": 98}, targets=(("3261", 20), ("3251", 110))).stage == "PR-B-1"
    assert choose({}, targets=(("3261", 0),)).stage == "1-7"
    assert choose({}, targets=()).stage == "1-7"


def test_closed_deficit_falls_back_without_claiming_all_targets_reached():
    depot = full_depot() | {"3211": 0}
    assert choose(depot).stage == "1-7"
    assert choose(depot | {"3261": 19}).stage == "PR-A-1"
    assert choose(depot | {"3261": 20}).stage == "1-7"


def test_active_activity_precedes_chip_shortages_and_garbage_uses_stock_selection():
    depot = {"31113": 1, "31083": 5, "31043": 8}
    assert choose(depot, activity=farming_activity()).stage == "TL-9"
    assert choose(depot, activity=farming_activity(), garbage=("TL",)).stage == "PR-A-1"
    assert (
        choose({"31113": 100, "31083": 100, "31043": 100}, activity=farming_activity()).stage
        == "TL-4"
    )


def test_ordered_fallback_observes_four_am_boundary_and_resource_event():
    thursday = datetime(2026, 11, 5, 4, tzinfo=BUSINESS_TIMEZONE)
    fallback = ("AP-5", "1-7")
    assert choose(full_depot(), fallback=fallback, now=thursday.timestamp() - 1).stage == "1-7"
    assert choose(full_depot(), fallback=fallback, now=thursday.timestamp()).stage == "AP-5"
    snapshot = empty_activity()
    event = ResourceCollection(
        starts_at=thursday - timedelta(hours=1),
        ends_at=thursday,
        timezone_offset=8,
        tip="all",
        is_resource_collection=True,
    )
    snapshot = replace(snapshot, data=replace(snapshot.data, resource_collection=event))
    assert (
        choose(
            full_depot(), activity=snapshot, fallback=fallback, now=thursday.timestamp() - 1
        ).stage
        == "AP-5"
    )


def test_no_open_fallback_is_an_error_without_hidden_replacement():
    with pytest.raises(ValueError, match="均未开放"):
        choose(full_depot(), fallback=("CE-6",))  # Monday


def schedule(tmp_path, **task_fields):
    row = {
        "id": "daily",
        "profiles": ["a"],
        "times": ["12:00"],
        "tasks": [
            {"type": "Fight", "strategy": "expert", **task_fields},
        ],
    }
    return parse_schedules([row], {"a"}, tmp_path / "config.toml")[0]


def test_config_defaults_all_sixteen_targets_to_twenty_and_fallback_to_one_seven(tmp_path):
    task = schedule(tmp_path).tasks[0]
    assert len(task.chip_targets) == 16
    assert all(target == 20 for _, target in task.chip_targets)
    assert task.fallback_stages == ("1-7",)


def test_configured_selection_removes_times_and_preserves_consumption(tmp_path):
    configured = schedule(
        tmp_path,
        chip_targets={"3231": 20},
        fallback_stages=["AP-5", "1-7"],
        params={"times": 30, "series": 0, "medicine": 2, "stone": 0},
    )
    assert configured.tasks[0].chip_targets == (("3231", 20),)
    selected = []
    for depot in ({}, {"3231": 20}):
        plan = build_daily_plan(configured, now=NOW, activity=empty_activity(), depot=depot)
        assert plan.tasks is not None
        params = json.loads(plan.tasks.document)["tasks"][0]["params"]
        assert {k: v for k, v in params.items() if k != "stage"} == {
            "series": 0,
            "medicine": 2,
            "stone": 0,
        }
        assert "drops" not in params
        selected.append(params["stage"])
    assert selected == ["PR-A-1", "AP-5"]


@pytest.mark.parametrize(
    "fields",
    [
        {"chip_targets": {"3263": 20}},
        {"chip_targets": {"3261": -1}},
        {"chip_targets": {"3261": True}},
        {"chip_targets": {"3261": "20"}},
        {"chip_targets": []},
        {"fallback_stages": []},
        {"fallback_stages": "1-7"},
        {"fallback_stages": ["AUTO"]},
        {"fallback_stages": ["AP-9"]},
        {"fallback_stages": ["TL-4"]},
        {"fallback_stages": ["1-7", "1-7"]},
    ],
)
def test_invalid_targets_and_unknown_calendars_fail_at_configuration_time(tmp_path, fields):
    with pytest.raises(ValueError):
        schedule(tmp_path, **fields)

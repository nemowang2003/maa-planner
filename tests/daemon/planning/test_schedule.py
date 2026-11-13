from dataclasses import replace

import pytest

from maa_planner.daemon.config import settings_from_bytes
from maa_planner.daemon.planning.schedule import ScheduledOccurrence, parse_schedules
from maa_planner.daemon.policy import ConfigSnapshot, Context, DailyPolicy
from tests.helpers.scheduling import timestamp


def schedule_row():
    return {
        "id": "daily",
        "profiles": ["a", "b", "c", "d"],
        "times": ["19:40", "02:40", "12:10", "15:40"],
        "tasks": [{"type": "Award", "params": {"award": True, "mail": True}}],
    }


def schedules(tmp_path, row=None):
    return parse_schedules([row or schedule_row()], {"a", "b", "c", "d"}, tmp_path / "config.toml")


def context(schedule, now, revision="v1", enabled=("a", "b", "c", "d")):
    return Context(
        ConfigSnapshot(revision, b""),
        (),
        timestamp(now),
        schedules=schedule,
        enabled_profiles=enabled,
    )


def test_daily_policy_emits_all_four_accounts_once_and_sets_next_business_time(tmp_path):
    policy = DailyPolicy()
    configured = schedules(tmp_path)
    before = context(configured, "2026-11-02T02:39:59")
    decision = policy.evaluate(before)
    assert decision.occurrences == ()
    assert decision.wake_at == timestamp("2026-11-02T02:40:00")
    due = replace(before, now=decision.wake_at)
    decision = policy.evaluate(due)
    assert [o.profile for o in decision.occurrences] == ["a", "b", "c", "d"]
    assert len({o.id for o in decision.occurrences}) == 4
    assert decision.wake_at == timestamp("2026-11-02T12:10:00")
    assert policy.evaluate(due).occurrences == ()
    decision = policy.evaluate(replace(due, now=timestamp("2026-11-02T19:40:00")))
    assert len(decision.occurrences) == 4
    assert decision.occurrences[0].scheduled_at == timestamp("2026-11-02T19:40:00")
    assert decision.wake_at == timestamp("2026-11-03T02:40:00")


def test_startup_only_schedules_future_times_and_disabled_accounts_do_not_run(tmp_path):
    policy = DailyPolicy()
    current = context(schedules(tmp_path), "2026-11-02T12:10:01", enabled=("a",))
    assert policy.evaluate(current).wake_at == timestamp("2026-11-02T15:40:00")
    decision = policy.evaluate(replace(current, now=timestamp("2026-11-02T15:40:00")))
    assert [o.profile for o in decision.occurrences] == ["a"]


def test_late_wake_uses_latest_live_slot_and_reload_recomputes_next_time(tmp_path):
    policy = DailyPolicy()
    configured = schedules(tmp_path)
    policy.evaluate(context(configured, "2026-11-02T02:39:00"))
    decision = policy.evaluate(context(configured, "2026-11-02T16:00:00"))
    assert len(decision.occurrences) == 4
    assert all(o.scheduled_at == timestamp("2026-11-02T15:40:00") for o in decision.occurrences)
    changed = (replace(configured[0], times=(20 * 60,)),)
    decision = policy.evaluate(context(changed, "2026-11-02T16:00:00", revision="v2"))
    assert decision.occurrences == ()
    assert decision.wake_at == timestamp("2026-11-02T20:00:00")
    decision = policy.evaluate(
        context((replace(changed[0], enabled=False),), "2026-11-02T16:00:00", "v3")
    )
    assert decision.wake_at is None


def test_weekday_and_midnight_use_shanghai_calendar(tmp_path):
    row = schedule_row() | {"times": ["00:00"], "weekdays": [1]}
    configured = schedules(tmp_path, row)
    assert configured[0].next_after(timestamp("2026-11-01T23:59:59")) == timestamp(
        "2026-11-02T00:00:00"
    )
    assert configured[0].next_after(timestamp("2026-11-02T00:00:00")) == timestamp(
        "2026-11-09T00:00:00"
    )


def test_occurrence_identity_is_stable_without_configuration_revision():
    occurrence = ScheduledOccurrence("daily", "a", 100.0, 200.0)
    assert replace(occurrence, scheduled_at=100, expires_at=300).id == occurrence.id
    assert replace(occurrence, scheduled_at=101).id != occurrence.id
    assert replace(occurrence, profile="b").id != occurrence.id
    assert replace(occurrence, schedule_id="other").id != occurrence.id


@pytest.mark.parametrize(
    "change",
    [
        {"id": "../daily"},
        {"profiles": ["unknown"]},
        {"profiles": ["a", "a"]},
        {"times": []},
        {"times": ["24:00"]},
        {"times": ["2:40"]},
        {"times": ["02:40", "02:40"]},
        {"tasks": []},
        {"enabled": 1},
        {"weekdays": [0]},
        {"weekdays": [True]},
        {"weekdays": []},
        {"execution_timeout": True},
        {"execution_timeout": float("inf")},
        {"options": {"instance_options": {"kill_adb_on_exit": True}}},
        {"tasks": [{"type": "Unknown"}]},
        {"tasks": [{"type": "Award", "strategy": "expert"}]},
        {"tasks": [{"type": "Infrast", "strategy": "expert", "files": {}}]},
        {"tasks": [{"type": "Infrast", "plan_index": -1}]},
        {"tasks": [{"type": "Infrast", "plan_index": True}]},
        {"tasks": [{"type": "Infrast", "params": {"filename": "unfrozen.json"}}]},
        {"tasks": [{"type": "Award", "params": {"value": float("nan")}}]},
    ],
)
def test_invalid_schedule_configuration_is_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        schedules(tmp_path, schedule_row() | change)


def test_expert_paths_and_all_task_parameters_are_frozen_in_configuration(tmp_path):
    row = schedule_row()
    row["tasks"] = [
        {
            "type": "Infrast",
            "strategy": "expert",
            "plan_index": "auto",
            "files": {
                mode: f"base/{mode}.json"
                for mode in ("normal", "three_gold", "orundum", "sell_only")
            },
            "params": {"drones": "CombatRecord", "threshold": 0.37},
        }
    ]
    task = schedules(tmp_path, row)[0].tasks[0]
    assert dict(task.files)["normal"] == tmp_path / "base/normal.json"
    row["tasks"][0]["params"]["drones"] = "Money"
    assert '"CombatRecord"' in task.params_json


def test_processing_operators_are_explicit_ordered_infrast_configuration(tmp_path):
    row = schedule_row()
    row["tasks"] = [
        {
            "type": "Infrast",
            "processing": {"operators": ["年", "九色鹿", "芳汀"], "targets": {"T3": 20, "T4": 10}},
        }
    ]
    task = schedules(tmp_path, row)[0].tasks[0]
    assert task.processing_operators == ("年", "九色鹿", "芳汀")
    row["tasks"][0]["processing"]["operators"].reverse()
    assert task.processing_operators == ("年", "九色鹿", "芳汀")
    assert schedules(tmp_path, schedule_row())[0].tasks[0].processing_operators == ()


@pytest.mark.parametrize("operators", ["年", [""], [" 年"], [True], ["年", "年"], {}])
def test_invalid_processing_operator_list_is_rejected(tmp_path, operators):
    row = schedule_row()
    row["tasks"] = [{"type": "Infrast", "processing": {"operators": operators}}]
    with pytest.raises(ValueError, match="processing.operators"):
        schedules(tmp_path, row)


def test_processing_operator_list_is_only_valid_on_infrast(tmp_path):
    row = schedule_row()
    row["tasks"] = [{"type": "Award", "processing": {"operators": []}}]
    with pytest.raises(ValueError, match="Infrast"):
        schedules(tmp_path, row)


def test_config_without_schedules_remains_valid_and_schedule_targets_are_checked(tmp_path):
    path = tmp_path / "config.toml"
    assert settings_from_bytes(b'[[profiles]]\nname="a"', path).schedules == ()
    document = b"""[[profiles]]
name="a"
[[schedules]]
id="daily"
profiles=["a"]
times=["02:40"]
[[schedules.tasks]]
type="Award"
[schedules.tasks.params]
award=true
"""
    assert settings_from_bytes(document, path).schedules[0].times == (160,)
    with pytest.raises(ValueError, match="profiles"):
        settings_from_bytes(document.replace(b'profiles=["a"]', b'profiles=["b"]'), path)


def test_processing_targets_and_operator_strategies_remain_explicit_and_frozen(tmp_path):
    row = schedule_row()
    row["tasks"] = [
        {
            "type": "Infrast",
            "processing": {
                "operators": ["年"],
                "targets": {"T3": 20, "T4": 10, "聚合剂": 15, "炽合金块": 0},
                "strategy": {"年": ["聚合剂", "T3", "-炽合金块"]},
            },
        }
    ]
    task = schedules(tmp_path, row)[0].tasks[0]
    assert dict(task.processing_targets) == {"T3": 20, "T4": 10, "聚合剂": 15, "炽合金块": 0}
    row["tasks"][0]["processing"]["strategy"]["年"].clear()
    assert dict(task.processing_strategy)["年"] == ("聚合剂", "T3", "-炽合金块")


@pytest.mark.parametrize(
    "processing",
    [
        {"operators": ["年"]},
        {"operators": ["年"], "targets": {"T3": True}},
        {"operators": ["年"], "targets": {"T3": -1}},
        {"operators": ["年"], "targets": {"T3": 20}, "strategy": {"芳汀": ["T3"]}},
        {"operators": ["年"], "targets": {"T3": 20}, "strategy": {"年": "T3"}},
        {"operators": ["年"], "targets": {"T3": 20}, "strategy": {"年": ["-"]}},
    ],
)
def test_invalid_processing_configuration_is_rejected(tmp_path, processing):
    row = schedule_row()
    row["tasks"] = [{"type": "Infrast", "processing": processing}]
    with pytest.raises(ValueError, match="processing"):
        schedules(tmp_path, row)

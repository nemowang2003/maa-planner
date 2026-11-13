from copy import deepcopy

import pytest

from maa_planner.daemon.planning.clearance import prepare_clearance
from maa_planner.daemon.planning.tutorials import add_tutorial_barriers
from tests.helpers.clearance import NOW, activity, catalog


def prepared():
    scripts, data = catalog()
    stages = data["stage_table"]["stages"]
    stages["story"] = {
        **stages["act_01"],
        "stageId": "story",
        "code": "EV-ST-1",
        "levelId": None,
        "hardStagedId": None,
        "isStoryOnly": True,
        "unlockCondition": [{"stageId": "act_01", "completeState": "PASS"}],
    }
    stages["tutorial"] = {
        **stages["act_01"],
        "stageId": "tutorial",
        "code": "EV-TR-1",
        "hardStagedId": None,
        "unlockCondition": [{"stageId": "story", "completeState": "PASS"}],
    }
    stages["act_02"]["unlockCondition"] = [{"stageId": "tutorial", "completeState": "PASS"}]
    return prepare_clearance(scripts, data, activity(), now=NOW), data


def test_tutorial_barrier_follows_preceding_battles_through_core_owned_story():
    source, data = prepared()
    resources = deepcopy(source[2])
    plan, files, nodes = add_tutorial_barriers(source, data, now=NOW)
    assert [entry["stage_id"] for entry in plan["targets"]] == [
        "act_01",
        "act_01",
        "tutorial",
        "act_02",
        "act_02",
    ]
    assert plan["targets"][2]["kind"] == "missing_tutorial"
    assert not any(entry["stage_id"] == "story" for entry in plan["targets"])
    assert nodes == resources
    assert len(files) == 2


def test_completed_tutorial_never_becomes_a_barrier_and_story_missing_does_not_block_core():
    source, data = prepared()
    source[0]["completed"] = [["tutorial", False]]
    plan, _, _ = add_tutorial_barriers(source, data, now=NOW)
    assert not any(entry.get("kind") for entry in plan["targets"])
    assert not any("prerequisites" in entry for entry in plan["targets"])


def test_future_tutorial_is_not_inserted_and_cycles_fail_closed():
    source, data = prepared()
    data["stage_table"]["stageValidInfo"] = {"tutorial": {"startTs": NOW + 100}}
    plan, _, _ = add_tutorial_barriers(source, data, now=NOW)
    assert not any(entry.get("kind") for entry in plan["targets"])
    source, data = prepared()
    data["stage_table"]["stages"]["story"]["unlockCondition"] = [
        {"stageId": "story", "completeState": "PASS"}
    ]
    with pytest.raises(ValueError, match="循环"):
        add_tutorial_barriers(source, data, now=NOW)


def test_missing_tutorial_survives_frozen_plan_and_stops_before_any_core_request(
    tmp_path, monkeypatch
):
    import threading

    from maa_planner.daemon.execution.activity_copilot import run_clearance
    from maa_planner.daemon.execution.clearance_core import CoreClearanceDriver
    from maa_planner.daemon.execution.core import CallbackResults
    from maa_planner.daemon.planning.copilot import CopilotTarget
    from maa_planner.daemon.planning.daily import build_daily_plan
    from maa_planner.daemon.planning.schedule import parse_schedules

    source, data = prepared()
    context = add_tutorial_barriers(source, data, now=NOW)
    schedule = parse_schedules(
        [
            {
                "id": "test",
                "profiles": ["nemo"],
                "times": ["12:00"],
                "tasks": [{"type": "Fight", "params": {"stage": "1-7"}}],
            }
        ],
        {"nemo"},
        tmp_path / "config.toml",
    )[0]
    daily = build_daily_plan(schedule, now=NOW, clearance=context)
    assert daily.tasks is not None
    frozen = daily.tasks.materialize(tmp_path)
    barrier = next(
        v for v in frozen["activity_copilot"]["targets"] if v.get("kind") == "missing_tutorial"
    )
    assert "filename" not in barrier

    def forbidden(*args, **kwargs):
        pytest.fail("missing tutorial must stop before forwarding anything to Core")

    # The exact materialized metadata, rather than a separately hand-built
    # placeholder, reaches the actual clearance driver.
    target = CopilotTarget(
        **{k: barrier[k] for k in ("script_id", "stage_id", "code", "is_raid", "game_stage_id")}
    )
    monkeypatch.setattr("maa_planner.daemon.execution.clearance_core.time.time", lambda: NOW)
    driver = CoreClearanceDriver(
        frozen["activity_copilot"], forbidden, CallbackResults(), threading.Event()
    )
    report = run_clearance((target,), frozenset(), driver)
    assert report.outcome == "stopped"
    assert report.warning is not None
    assert report.warning["reason"] == "no_tutorial_copilot"

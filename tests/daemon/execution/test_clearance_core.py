import json
from collections import Counter
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.core import run
from tests.helpers.clearance import NOW, clearance
from tests.helpers.clearance_core import ClearanceCore
from tests.helpers.core import spec


@pytest.fixture(autouse=True)
def business_clock(monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)


def document(tmp_path):
    result = spec(tmp_path)
    result["tasks"] = [
        {"type": "StartUp"},
        {"type": "Fight", "params": {"stage": "1-7", "times": 1}},
        {"type": "Award"},
    ]
    result["activity_copilot"] = {**clearance()[0], "before_task": 1}
    return result


def test_only_initial_map_navigation_then_core_selects_paired_targets(tmp_path):
    core = ClearanceCore()
    doc = document(tmp_path)
    streamed = []

    def callback(report):
        assert not any(kind == "Fight" for kind, _ in core.appended)
        streamed.append(report)

    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core), on_clearance=callback) == 0
    assert core.visited == [
        ("EV-EX-1", False),
        ("EV-EX-1", True),
        ("EV-EX-2", False),
        ("EV-EX-2", True),
    ]
    assert Counter(kind for kind, _ in core.appended)["Custom"] == 4
    assert all(p["formation"] for kind, p in core.appended if kind == "Copilot")
    report = json.loads((tmp_path / "core-result.json").read_text())
    assert report["activity_copilot_results"] == json.loads(json.dumps(streamed[-1:]))
    assert [t["type"] for t in report["task_results"]] == ["StartUp", "Fight", "Award"]


@pytest.mark.parametrize("skip_first", [False, True])
def test_shared_formation_only_forms_first_actually_executed_target(tmp_path, skip_first):
    core = ClearanceCore()
    doc = document(tmp_path)
    for target in doc["activity_copilot"]["targets"]:
        target["formation_group"] = "set-30"
    if skip_first:
        doc["activity_copilot"]["completed"] = [["act_01", False]]
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    formations = [p["formation"] for kind, p in core.appended if kind == "Copilot"]
    assert formations == [True] + [False] * (2 if skip_first else 3)


def test_failed_battle_rebuilds_shared_formation_before_retry(tmp_path):
    core = ClearanceCore(("completed", "failed", "completed"))
    doc = document(tmp_path)
    for target in doc["activity_copilot"]["targets"]:
        target["formation_group"] = "set-30"
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert [p["formation"] for kind, p in core.appended if kind == "Copilot"] == [
        True,
        False,
        True,
        False,
        False,
    ]


def test_switching_collection_or_using_ordinary_formation_invalidates_reuse(tmp_path):
    core = ClearanceCore()
    doc = document(tmp_path)
    for target, group in zip(
        doc["activity_copilot"]["targets"], ("set-30", "set-30", None, "set-30")
    ):
        if group:
            target["formation_group"] = group
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert [p["formation"] for kind, p in core.appended if kind == "Copilot"] == [
        True,
        False,
        True,
        True,
    ]


def test_three_consecutive_losses_skip_farming_but_continue_daily(tmp_path):
    core = ClearanceCore(("completed", "failed", "failed", "failed"))
    assert run(document(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert core.visited == [("EV-EX-1", False)] + [("EV-EX-1", True)] * 3
    assert core.appended[-1][0] == "Award"
    assert (
        sum(
            p["task_names"] == ["Copilot@ClickCornerUntilStartButton"]
            for kind, p in core.appended
            if kind == "Custom"
        )
        == 2
    )
    assert not any(kind == "Fight" for kind, _ in core.appended)
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["warning"]["reason"] == "three_consecutive_defeats"
    assert report["results"][0]["outcome"] == "completed"
    assert report["results"][1]["failures"] == 3
    assert report["farming_gate"] == {
        "allowed": False,
        "reason": "stages_incomplete",
        "missing": [["act_01", True], ["act_02", False], ["act_02", True]],
    }


def test_navigation_failure_never_starts_copilot_and_returns_to_daily(tmp_path):
    core = ClearanceCore(navigation_failure=True)
    assert run(document(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    assert core.visited == []
    assert core.appended[-1][0] == "Award"
    assert not any(kind == "Fight" for kind, _ in core.appended)
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["outcome"] == "stopped" and report["warning"] is not None
    assert report["warning"]["reason"] == "navigation_failed"

    business_report = json.loads((tmp_path / "core-result.json").read_text())["business_results"]
    assert business_report[1]["id"] == "task:1" and business_report[1]["status"] == "error"
    assert business_report[-1]["status"] == "completed"


def test_activity_closing_before_the_phase_skips_battles_and_keeps_daily(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 3000)
    core = ClearanceCore()
    assert run(document(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert not core.visited
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["warning"]["reason"] == "activity_closed"
    assert not any(kind == "Fight" for kind, _ in core.appended)


def test_ledger_skips_only_exact_account_activity_stage_mode_snapshot(tmp_path):
    core = ClearanceCore()
    doc = document(tmp_path)
    doc["activity_copilot"]["completed"] = [["act_01", False], ["act_02", True]]
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert core.visited == [("EV-EX-1", True), ("EV-EX-2", False)]
    assert any(kind == "Fight" for kind, _ in core.appended)


def test_missing_settlement_skips_all_regular_fights_and_keeps_remaining_daily(tmp_path):
    core = ClearanceCore(("completed", "unknown"))
    doc = document(tmp_path)
    doc["tasks"].extend(
        [
            {"type": "Fight", "params": {"stage": "CE-6"}},
            {"type": "Mall"},
            {"type": "Recruit"},
            {"type": "CloseDown"},
        ]
    )
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    assert not any(kind == "Fight" for kind, _ in core.appended)
    assert [kind for kind, _ in core.appended][-4:] == ["Award", "Mall", "Recruit", "CloseDown"]
    report = json.loads((tmp_path / "core-result.json").read_text())
    phase = report["activity_copilot_results"][0]
    assert report["status"] == "failed"
    assert phase["warning"]["core_error"]["status"] == "error"
    assert phase["warning"]["core_error"]["evidence"].startswith("callbacks.jsonl:")
    assert phase["warning"]["reason"] == "battle_unknown"
    assert phase["farming_gate"]["allowed"] is False
    assert phase["results"][0]["outcome"] == "completed"
    assert [r["type"] for r in report["task_results"]] == [
        "StartUp",
        "Award",
        "Mall",
        "Recruit",
        "CloseDown",
    ]


@pytest.mark.parametrize("confirmed_tutorial", [False, True])
def test_script_completion_requires_tutorial_but_not_story_ledger(tmp_path, confirmed_tutorial):
    from maa_planner.daemon.planning.clearance import prepare_clearance
    from tests.helpers.activity_shop import shop_catalog
    from tests.helpers.clearance import activity, catalog

    scripts, _ = catalog()
    data = shop_catalog()  # Includes a story stage outside the script collection.
    data["stage_table"]["stages"]["act_tr"] = {
        **data["stage_table"]["stages"]["act_st"],
        "code": "EV-TR-1",
        "stageId": "act_tr",
        "isStoryOnly": False,
        "levelId": "activities/act/level_tr",
    }
    plan, _, _ = prepare_clearance(scripts, data, activity(), now=NOW)
    doc = document(tmp_path)
    doc["activity_copilot"] = {**plan, "before_task": 1}
    if confirmed_tutorial:
        doc["activity_copilot"]["completed"] = [["act_tr", False]]
    core = ClearanceCore()
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert any(kind == "Fight" for kind, _ in core.appended) == confirmed_tutorial
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["outcome"] == "completed"
    assert report["farming_gate"] == {
        "allowed": confirmed_tutorial,
        "reason": "ready" if confirmed_tutorial else "stages_incomplete",
        "missing": [] if confirmed_tutorial else [["act_tr", False]],
    }


def test_script_subset_finishing_cannot_certify_uncovered_battle_stages(tmp_path):
    core = ClearanceCore()
    doc = document(tmp_path)
    doc["activity_copilot"]["targets"] = doc["activity_copilot"]["targets"][:2]
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert not any(kind == "Fight" for kind, _ in core.appended)
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["outcome"] == "completed"
    assert report["farming_gate"]["missing"] == [["act_02", False], ["act_02", True]]


@pytest.mark.parametrize("scope", ["complete", "incomplete", "legacy"])
def test_fully_confirmed_ledger_requires_a_complete_catalog_before_farming(tmp_path, scope):
    doc = document(tmp_path)
    doc["activity_copilot"]["completed"] = doc["activity_copilot"]["required"]
    if scope == "incomplete":
        doc["activity_copilot"]["catalog_complete"] = False
    elif scope == "legacy":
        del doc["activity_copilot"]["required"]
        del doc["activity_copilot"]["catalog_complete"]
    core = ClearanceCore()
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert core.visited == []
    assert any(kind == "Fight" for kind, _ in core.appended) == (scope == "complete")
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["farming_gate"]["reason"] == (
        "ready" if scope == "complete" else "catalog_incomplete"
    )


def test_search_selection_and_support_policy_survive_worker_and_failure_is_reported(tmp_path):
    core = ClearanceCore()
    doc = document(tmp_path)
    doc["activity_copilot"]["selection"] = [
        {"region": "EX", "status": "selected", "set_id": 1},
        {"region": "S", "status": "no_usable_set"},
    ]
    doc["activity_copilot"]["required"].append(["uncovered_s", False])
    for entry in doc["activity_copilot"]["targets"]:
        entry["support_unit_usage"] = 1
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert all(
        params["support_unit_usage"] == 1 for kind, params in core.appended if kind == "Copilot"
    )
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["selection"] == doc["activity_copilot"]["selection"]
    assert report["warning"]["reason"] == "no_usable_set"
    assert report["outcome"] == "stopped"
    assert report["farming_gate"]["allowed"] is False
    assert not any(kind == "Fight" for kind, _ in core.appended)


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_raid", "false"),
        ("zone_id", ""),
        ("navigation", "Fight"),
        ("formation_group", ""),
        ("formation_group", 30),
        ("formation_group", "set-0"),
        ("formation_group", "other"),
    ],
)
def test_malformed_frozen_target_never_passes_plan_validation(field, value):
    from maa_planner.daemon.execution.activity_copilot import validate_clearance_plan

    plan: dict = {**clearance()[0], "before_task": 1}
    plan["targets"][0][field] = value
    with pytest.raises(ValueError):
        validate_clearance_plan(plan, 3)


@pytest.mark.parametrize(
    "change",
    [
        {"catalog_complete": "true"},
        {"required": []},
        {"required": [["act_01", "false"]]},
        {"required": [["", False]]},
        {"required": [["act_01", False], ["act_01", False]]},
        {"required": None},
    ],
)
def test_invalid_full_activity_scope_cannot_pass_plan_validation(change):
    from maa_planner.daemon.execution.activity_copilot import validate_clearance_plan

    plan = {**clearance()[0], "before_task": 1, **change}
    with pytest.raises(ValueError, match="完整活动清单"):
        validate_clearance_plan(plan, 3)


def test_uncovered_tutorial_stops_at_that_target_after_earlier_battles(tmp_path):
    doc = document(tmp_path)
    plan = doc["activity_copilot"]
    plan["targets"].insert(
        2,
        {
            "kind": "missing_tutorial",
            "script_id": 0,
            "stage_id": "tutorial",
            "code": "EV-TR-1",
            "game_stage_id": "tutorial",
            "is_raid": False,
            "filename": "",
            "script_stage_id": "tutorial",
            "navigation": "Planner@TutorialStop",
            "zone_id": "zone",
        },
    )
    plan["required"].append(["tutorial", False])
    core = ClearanceCore()
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert core.visited == [("EV-EX-1", False), ("EV-EX-1", True)]
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["warning"]["reason"] == "no_tutorial_copilot"
    assert report["warning"]["stage_id"] == "tutorial"
    assert report["warning"]["failures"] == 0
    assert report["farming_gate"]["allowed"] is False
    assert not any(kind == "Fight" for kind, _ in core.appended)
    assert core.appended[-1][0] == "Award"


def test_failed_target_retries_via_core_settlement_exit_without_reentering_activity(tmp_path):
    core = ClearanceCore(("failed", "completed"))
    assert run(document(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert core.visited[:2] == [("EV-EX-1", False), ("EV-EX-1", False)]
    phases = [params["task_names"][0] for kind, params in core.appended if kind == "Custom"]
    assert sum(name.endswith("@ToHome") for name in phases) == 1
    assert phases.count("Copilot@ClickCornerUntilStartButton") == 1
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["results"][0]["failures"] == 1
    assert report["warning"] is None


@pytest.mark.parametrize("exit_mode", ["error", "missing_evidence"])
def test_unverified_core_settlement_exit_stops_without_home_fallback_or_another_battle(
    tmp_path, exit_mode
):
    core = ClearanceCore(("failed",), settlement_exit=exit_mode)
    assert run(document(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == (
        1 if exit_mode == "error" else 0
    )
    assert core.visited == [("EV-EX-1", False)]
    phases = [p["task_names"][0] for kind, p in core.appended if kind == "Custom"]
    assert sum(name.endswith("@ToHome") for name in phases) == 1
    assert phases.count("Copilot@ClickCornerUntilStartButton") == 1
    assert not any(kind == "Fight" for kind, _ in core.appended)
    assert core.appended[-1][0] == "Award"
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["warning"]["reason"] == "settlement_exit_failed"
    assert report["warning"]["failures"] == 1


def test_two_star_clearance_uses_same_core_exit_and_retry_policy(tmp_path):
    core = ClearanceCore(("two_star", "completed"))
    assert run(document(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert core.visited[:2] == [("EV-EX-1", False), ("EV-EX-1", False)]
    phases = [p["task_names"][0] for kind, p in core.appended if kind == "Custom"]
    assert sum(name.endswith("@ToHome") for name in phases) == 1
    assert phases.count("Copilot@ClickCornerUntilStartButton") == 1
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_copilot_results"][0]
    assert report["results"][0]["failures"] == 1
    assert report["results"][0]["outcome"] == "completed"
    assert report["warning"] is None


def test_disconnect_during_copilot_stops_daily_and_preserves_only_certified_targets(tmp_path):
    class DisconnectCore(ClearanceCore):
        def task_outcome(self, identifier, kind, params):
            if kind == "Copilot" and self.visited:
                self.emit(
                    20001,
                    {
                        "taskid": identifier,
                        "taskchain": kind,
                        "subtask": "ProcessTask",
                        "details": {"task": "OfflineConfirmAfterBattle"},
                    },
                )
                return 10002
            return super().task_outcome(identifier, kind, params)

    core = DisconnectCore()
    assert run(document(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    report = json.loads((tmp_path / "core-result.json").read_text())
    assert report["session_failure"]["kind"] == "game_disconnected"
    assert [t["type"] for t in report["task_results"]] == ["StartUp"]
    assert [r["outcome"] for r in report["activity_copilot_results"][0]["results"]] == ["completed"]
    assert report["activity_copilot_results"][0]["results"][0]["target"]["is_raid"] is False
    assert not any(kind in {"Fight", "Award"} for kind, _ in core.appended)


@pytest.mark.parametrize("shortcut", ["ready", "error", "no_map", "locked"])
def test_cross_region_navigation_uses_current_activity_and_requires_target_map(tmp_path, shortcut):
    from maa_planner.daemon.planning.clearance import prepare_clearance
    from tests.helpers.clearance import activity, catalog

    scripts, data = catalog()
    for stage in ("act_02", "act_02#f#"):
        data["stage_table"]["stages"][stage]["zoneId"] = "second"
    data["zone_table"]["zones"]["second"] = {"zoneNameSecond": "另一区域"}
    data["zone_table"]["zoneValidInfo"]["second"] = {"startTs": 1000, "endTs": 2999}
    data["activity_table"]["zoneToActivity"]["second"] = "event"
    plan, _, nodes = prepare_clearance(scripts, data, activity(), now=NOW)
    assert "Planner@Clearance1@SwitchArea" in nodes

    class RegionCore(ClearanceCore):
        def task_outcome(self, identifier, kind, params):
            if kind == "Custom" and params["task_names"][0].endswith("@SwitchArea"):
                if shortcut == "locked":
                    self.emit(
                        20002,
                        {
                            "taskid": identifier,
                            "taskchain": kind,
                            "subtask": "ProcessTask",
                            "details": {"task": "Locked"},
                        },
                    )
                elif shortcut == "ready":
                    self.emit(
                        20002,
                        {
                            "taskid": identifier,
                            "taskchain": kind,
                            "subtask": "ProcessTask",
                            "details": {"task": "Map"},
                        },
                    )
                return 10000 if shortcut == "error" else 10002
            return super().task_outcome(identifier, kind, params)

    doc = document(tmp_path)
    doc["activity_copilot"] = {**plan, "before_task": 1}
    core = RegionCore()
    code = run(doc, library_factory=lambda _: SimpleNamespace(lib=core))
    assert code == 0
    phases = [p["task_names"][0] for k, p in core.appended if k == "Custom"]
    assert "Planner@Clearance1@SwitchArea" in phases
    assert ("Planner@Clearance1@ToHome" in phases) == (shortcut in {"error", "no_map"})
    assert core.visited == (
        [("EV-EX-1", False), ("EV-EX-1", True)]
        if shortcut == "locked"
        else [("EV-EX-1", False), ("EV-EX-1", True), ("EV-EX-2", False), ("EV-EX-2", True)]
    )
    back = nodes["Planner@Clearance1@BackToActivity"]
    assert back["maxTimes"] == 4 and back["exceededNext"] == []
    assert back["next"] == [
        "Planner@Clearance1@Map",
        "Planner@Clearance1@Region",
        "Planner@Clearance1@BackToActivity",
    ]

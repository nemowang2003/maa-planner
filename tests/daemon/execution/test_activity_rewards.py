import json
import threading
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.execution.core import run
from maa_planner.daemon.execution.tasks import CoreTasks
from maa_planner.daemon.planning.activity_rewards import reward_task
from maa_planner.daemon.planning.daily import build_daily_plan
from tests.helpers.activity_shop import NOW, activity, configured, navigation_assets, shop_plan
from tests.helpers.core import FakeCore, spec


def mission_plan():
    return {**shop_plan(), "mission_entry": navigation_assets(missions=True)["entries"]["missions"]}


def test_rewards_before_award_independent_of_full_clear_garbage_and_shop_disabled(tmp_path):
    plan = {**mission_plan(), "navigation_status": "disabled", "garbage": True}
    schedule = configured(tmp_path)
    daily = build_daily_plan(
        schedule, now=NOW, activity=activity(), depot={}, shops=(plan,), skip_farming=True
    )
    assert daily.tasks is not None
    tasks = json.loads(daily.tasks.document)["tasks"]
    assert [t["type"] for t in tasks][-2:] == ["Custom", "Award"]
    assert tasks[-2]["params"]["activity_reward"]["activity_id"] == "event"
    assert (
        json.loads(daily.tasks.document)["resource_tasks"]["Planner@Rewards0@Received"]["next"]
        == []
    )


@pytest.mark.parametrize(
    "controls,claim,proof,outcome",
    [
        ("MissionControls", "Claim", True, "claimed"),
        ("MissionControls", "Claim", False, "unknown"),
        ("CachedControls", "CachedClaim", True, "claimed"),
    ],
)
def test_reward_callback_parser_requires_page_claim_and_receipt(
    tmp_path, monkeypatch, controls, claim, proof, outcome
):
    task, _ = reward_task(mission_plan(), 0)
    value = spec(tmp_path)
    value["tasks"] = [task]
    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)

    class RewardsCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            assert "activity_reward" not in params
            common = {"taskid": identifier, "taskchain": kind}
            nodes = ["MissionReady", controls, claim, "Received"] if proof else ["MissionReady"]
            for node in nodes:
                self.emit(20002, {**common, "subtask": "ProcessTask", "details": {"task": node}})
            return 10002

    run(value, stop=threading.Event(), library_factory=lambda _: SimpleNamespace(lib=RewardsCore()))
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["activity_reward_results"][0]["outcome"] == outcome
    assert result["status"] == "succeeded"
    restored = RunResult.from_dict(
        asdict(
            RunResult(
                "attempt",
                "succeeded",
                activity_reward_results=tuple(result["activity_reward_results"]),
            )
        )
    )
    assert restored.activity_reward_results[0]["outcome"] == outcome


def test_invalid_reward_marker_rejected_before_worker():
    task, _ = reward_task(mission_plan(), 0)
    task["params"]["activity_reward"]["navigation"] = "Fight"
    with pytest.raises(ValueError, match="活动奖励身份"):
        CoreTasks(json.dumps({"tasks": [task]}))


@pytest.mark.parametrize(
    "node,outcome",
    [("Locked", "unavailable"), ("Incomplete", "incomplete"), ("Received", "unknown")],
)
def test_domain_states_require_page_context_and_receipt_requires_claim(node, outcome):
    from maa_planner.daemon.execution.core import CallbackResults

    result = CallbackResults()
    result.rewards[4] = {"outcome": "unknown", "page_verified": False, "evidence": []}

    def emit(name, task=4):
        result.consume(
            20002,
            {
                "taskid": task,
                "taskchain": "Custom",
                "subtask": "ProcessTask",
                "details": {"task": name},
            },
            "callback:1",
            NOW,
        )

    emit(node)
    assert result.rewards[4]["outcome"] == "unknown"
    emit("MissionReady")
    emit("MissionControls")
    emit(node)
    assert result.rewards[4]["outcome"] == outcome


def test_daily_reward_graph_reaches_receipt_through_page_and_claim_checkpoints(tmp_path):
    daily = build_daily_plan(
        configured(tmp_path),
        now=NOW,
        activity=activity(),
        depot={},
        shops=({**mission_plan(), "navigation_status": "disabled"},),
        skip_farming=True,
    )
    assert daily.tasks is not None
    document = json.loads(daily.tasks.document)
    nodes = document["resource_tasks"]
    reward = next(t for t in document["tasks"] if "activity_reward" in t.get("params", {}))
    root = reward["params"]["task_names"][0]
    prefix = reward["params"]["activity_reward"]["navigation"]

    def reachable(start, blocked=()):
        seen, todo = set(), [start]
        while todo:
            node = todo.pop()
            if node in seen or node in blocked:
                continue
            seen.add(node)
            todo.extend(nodes.get(node, {}).get("next", []))
        return seen

    checkpoints = [
        prefix + "@" + name for name in ("MissionReady", "MissionControls", "Claim", "Received")
    ]
    assert set(checkpoints) <= reachable(root)
    # Each proof must be on the route to receipt, not an unreachable resource node
    # or a bypass that would announce success without visiting/claiming the page.
    for checkpoint in checkpoints[:-1]:
        assert checkpoints[-1] not in reachable(root, blocked={checkpoint})
    assert nodes[checkpoints[2]]["action"] == "ClickSelf"
    assert nodes[checkpoints[-1]]["baseTask"] == "SignInEventEarnRewards"
    assert nodes[checkpoints[-1]]["next"] == []


@pytest.mark.parametrize(
    "trace_name,outcome,code",
    [
        ("activity-reward-navigation-failed.jsonl", "unknown", 1),
        ("activity-reward-claimed.jsonl", "claimed", 0),
    ],
)
def test_recorded_reward_callbacks_distinguish_navigation_failure_from_actual_claim(
    tmp_path, monkeypatch, trace_name, outcome, code
):
    from pathlib import Path

    # MaaCore v6.22.0: failed navigation (00:16) and successful claim (11:55)
    # on 2026-11-12, Asia/Shanghai. Keep the Custom chain without device UUID.
    trace = Path(__file__).with_name("fixtures") / trace_name
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    task, _ = reward_task(mission_plan(), 0)
    monkeypatch.setattr("time.time", lambda: NOW)

    class RecordedCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            if kind != "Custom":
                return super().task_outcome(identifier, kind, params)
            terminal = None
            for event in events:
                message = event["message"]
                if message in {10000, 10002, 10004}:
                    terminal = message
                elif message not in {10001, 3}:
                    self.emit(message, {**event["payload"], "taskid": identifier})
            return terminal

    document = spec(tmp_path)
    document["tasks"] = [task, {"type": "Award"}]
    core = RecordedCore()
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == code
    report = json.loads((tmp_path / "core-result.json").read_text())
    expected = "error" if code else "completed"
    assert [t["status"] for t in report["task_results"]] == [expected, "completed"]
    assert report["activity_reward_results"][0]["outcome"] == outcome
    assert report["activity_reward_results"][0]["page_verified"] is (outcome == "claimed")
    assert bool(report["activity_reward_results"][0].get("claim_roi")) is (outcome == "claimed")
    assert [b["status"] for b in report["business_results"]] == [expected, "completed"]


@pytest.mark.parametrize("phase", ["MissionControls", "Claim"])
def test_reward_nodes_accept_recorded_task_page_ocr_inside_their_roi(phase):
    # 2026-11-12 10:57:24.985, Core 6.22.0 WordOcr: title and button
    # lose leading glyphs, while the task-list column heading remains intact.
    observations = [
        ("见证的", [560, 31, 48, 20]),
        ("当前有多个已完成待领取的任务", [638, 160, 280, 22]),
        ("键领取", [1058, 157, 71, 29]),
        ("REQUIREMENT", [454, 256, 68, 20]),
    ]
    _, nodes = reward_task(mission_plan(), 0)
    node = nodes["Planner@Rewards0@" + phase]
    x, y, w, h = node["roi"]
    matches = [
        text
        for text, (a, b, c, d) in observations
        if text in node["text"] and x <= a and y <= b and a + c <= x + w and b + d <= y + h
    ]
    assert matches
    assert node["fullMatch"] is True
    if phase == "Claim":
        assert node["action"] == "ClickSelf"
    else:
        assert node["action"] == "DoNothing"


def test_activity_shortcuts_enter_reward_and_shop_pages_without_home_routes():
    from maa_planner.daemon.planning.activity_shop import shop_navigation

    _, rewards = reward_task(mission_plan(), 0)
    shops = shop_navigation(mission_plan(), "Planner@Shop0")
    for prefix, nodes, ready in [
        ("Planner@Rewards0", rewards, "MissionReady"),
        ("Planner@Shop0", shops, "ShopReady"),
    ]:
        candidates = [prefix + "@" + name for name in (ready, "Entry", "BackToActivity")]
        assert nodes[prefix + "@FromActivity"]["next"] == candidates
        assert nodes[prefix + "@BackToActivity"]["next"] == candidates
        assert nodes[prefix + "@BackToActivity"]["maxTimes"] == 4
        assert nodes[prefix + "@BackToActivity"]["exceededNext"] == []


@pytest.mark.parametrize("page_confirmed", [False, True])
def test_absent_claim_button_is_normal_only_after_positive_task_page_recognition(
    tmp_path, page_confirmed, monkeypatch
):
    task, nodes = reward_task(mission_plan(), 0)
    assert nodes["Planner@Rewards0@MissionReady"]["next"][-1] == "Planner@Rewards0@NoClaim"
    assert nodes["Planner@Rewards0@NoClaim"] == {"algorithm": "JustReturn", "next": []}

    class NoButtonCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            common = {"taskid": identifier, "taskchain": kind, "subtask": "ProcessTask"}
            if page_confirmed:
                self.emit(20002, {**common, "details": {"task": "MissionReady"}})
            self.emit(20002, {**common, "details": {"task": "NoClaim"}})
            return 10002

    monkeypatch.setattr("time.time", lambda: NOW)
    document = spec(tmp_path)
    document["tasks"] = [task]
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=NoButtonCore())) == 0
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_reward_results"][0]
    assert report["outcome"] == ("nothing_to_claim" if page_confirmed else "unknown")
    assert report["page_verified"] is page_confirmed
    assert not report.get("claim_attempted")
    assert "claim_roi" not in report


def test_mission_page_anchor_is_derived_from_game_data_not_observed_header_text():
    from pathlib import Path

    from maa_planner.daemon.planning.navigation import activity_page_data

    catalog = json.loads(
        (Path(__file__).parents[1] / "fixtures/activity-navigation/catalog.json").read_text()
    )
    generated = activity_page_data(catalog, "act55side")
    plan = {**mission_plan(), **generated, "activity_id": "act55side"}
    _, nodes = reward_task(plan, 0)
    anchor = nodes["Planner@Rewards0@MissionReady"]
    assert "通关TL-EX-1" in anchor["text"]
    assert "REQUIREMENT" not in anchor["text"]
    # The same raw OCR trace also read this complete mission description.
    assert "通关TL-EX-1" in anchor["text"]
    assert anchor["roi"] == [0, 0, 1280, 720]
    assert generated["mission_page_source"]["catalog_commit"] == catalog["commit"]


def test_missing_mission_data_never_falls_back_to_screenshot_derived_anchor():
    plan = {**mission_plan(), "mission_texts": []}
    with pytest.raises(ValueError, match="游戏数据"):
        reward_task(plan, 0)


def test_reward_plan_freezes_activity_specific_game_data_provenance_before_worker():
    task, _ = reward_task(mission_plan(), 0)
    source = task["params"]["activity_reward"]["page_source"]
    assert source["activity_id"] == "event"
    assert source["mission_ids"] == ["mission_1"]
    source["activity_id"] = "different-account-activity"
    with pytest.raises(ValueError, match="游戏数据来源"):
        CoreTasks(json.dumps({"tasks": [task]}))


def test_missing_reward_anchor_data_keeps_ordinary_farming_and_independent_daily_tasks(tmp_path):
    plan = {**mission_plan(), "mission_texts": [], "navigation_status": "disabled"}
    daily = build_daily_plan(
        configured(tmp_path), now=NOW, activity=activity(), depot={}, shops=(plan,)
    )
    assert daily.tasks is not None
    tasks = json.loads(daily.tasks.document)["tasks"]
    assert sum(t["type"] == "Fight" for t in tasks) == 2
    assert any(t["type"] == "Award" for t in tasks)
    assert not any("activity_reward" in t.get("params", {}) for t in tasks)
    assert daily.failures[0]["id"] == "reward:event"
    assert daily.failures[0]["error"]["kind"] == "preparation"

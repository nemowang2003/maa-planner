import threading
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.clearance_core import CoreClearanceDriver
from maa_planner.daemon.planning.clearance import prepare_clearance
from maa_planner.daemon.planning.copilot import CopilotTarget
from maa_planner.daemon.planning.tutorials import add_tutorial_barriers
from tests.helpers.clearance import NOW, activity
from tests.helpers.stories import story_catalog


def plan():
    scripts, data = story_catalog()
    prepared = prepare_clearance(scripts, data, activity(), now=NOW)
    prepared[0]["completed"] = [["act_02", False]]
    return add_tutorial_barriers(prepared, data, now=NOW)[0]


@pytest.mark.parametrize(
    "nodes,expected",
    [
        (["PlotPanel", "PlotStart", "PlotSkipped", "PlotReturned"], "completed"),
        ([], "story_unverified"),
        (["EndOfPlot"], "story_unverified"),
        (["PlotPanel", "PlotStart", "PlotReturned"], "story_unverified"),
        (["PlotReturned", "PlotPanel", "PlotStart", "PlotSkipped"], "story_unverified"),
    ],
)
def test_story_requires_targeted_start_skip_and_return_evidence(monkeypatch, nodes, expected):
    monkeypatch.setattr("time.time", lambda: NOW)
    source = plan()
    entry = next(e for e in source["targets"] if e.get("kind") == "tail_story")
    target = CopilotTarget(0, entry["stage_id"], entry["code"], False, entry["stage_id"])
    results = SimpleNamespace(tasks={}, nodes={})
    calls = []

    def execute(tasks, **kwargs):
        calls.extend(tasks)
        results.tasks[1] = {"status": "completed"}
        results.nodes[1] = [(name, f"callbacks.jsonl:{i + 1}") for i, name in enumerate(nodes)]
        return [1]

    driver = CoreClearanceDriver(source, execute, results, threading.Event())
    result = driver.battle(target)
    assert result.outcome == expected
    assert len(calls) == 1 and calls[0]["type"] == "Custom"
    assert calls[0]["params"]["task_names"] == [entry["navigation"] + "@PlotBegin"]


@pytest.mark.parametrize(
    "story_nodes,story_failure,allowed",
    [
        (("PlotPanel", "PlotStart", "PlotSkipped", "PlotReturned"), False, True),
        (("EndOfPlot",), False, False),
        (("PlotPanel", "PlotStart", "PlotSkipped", "PlotReturned"), True, False),
    ],
)
def test_clearance_watches_tail_before_next_region_and_streams_only_proven_results(
    tmp_path, monkeypatch, story_nodes, story_failure, allowed
):
    import json

    from maa_planner.daemon.execution.core import run
    from tests.helpers.clearance_core import ClearanceCore
    from tests.helpers.core import spec

    monkeypatch.setattr("time.time", lambda: NOW)
    scripts, data = story_catalog()
    scripts = (*scripts, (3, {"stage_name": "later", "actions": [], "opers": []}))
    source = add_tutorial_barriers(
        prepare_clearance(scripts, data, activity(), now=NOW), data, now=NOW
    )
    document = spec(tmp_path)
    document["tasks"] = [{"type": "Fight", "params": {"stage": "1-7"}}, {"type": "Award"}]
    document["activity_copilot"] = {**source[0], "before_task": 0}
    document["resource_tasks"] = source[2]
    core = ClearanceCore(
        story_nodes=story_nodes,
        story_failure=story_failure,
        stage_ids={"EV-EX-1": "act_01", "EV-EX-2": "act_02", "EV-S-1": "later"},
    )
    streamed = []
    exit_code = run(
        document, library_factory=lambda _: SimpleNamespace(lib=core), on_clearance=streamed.append
    )
    assert exit_code == (1 if story_failure else 0)
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["activity_copilot_results"][0]["farming_gate"]["allowed"] == allowed
    assert any(k == "Fight" for k, _ in core.appended) == allowed
    seen = [
        r["target"]["stage_id"]
        for report in streamed
        for r in report["results"]
        if r["target"]["script_id"] == 0 and r["outcome"] == "completed"
    ]
    assert set(seen) == ({"tail", "tail2"} if allowed else set())
    if allowed:
        tail_position = next(
            i
            for i, (k, p) in enumerate(core.appended)
            if k == "Custom" and p["task_names"][0].endswith("@PlotBegin")
        )
        next_region = next(
            i
            for i, (k, p) in enumerate(core.appended)
            if k == "Copilot" and p["copilot_list"][0]["nav_name_override"] == "EV-S-1"
        )
        assert tail_position < next_region


@pytest.mark.parametrize("cancelled", [False, True])
def test_story_does_not_start_without_confirmed_prerequisite_or_after_cancel(
    monkeypatch, cancelled
):
    monkeypatch.setattr("time.time", lambda: NOW)
    source = plan()
    source["completed"] = []
    entry = next(e for e in source["targets"] if e.get("kind") == "tail_story")
    target = CopilotTarget(0, entry["stage_id"], entry["code"], False, entry["stage_id"])
    stop = threading.Event()
    if cancelled:
        stop.set()

    def execute(*args, **kwargs):
        pytest.fail("Unconfirmed or cancelled story must not start Core tasks")

    driver = CoreClearanceDriver(source, execute, SimpleNamespace(tasks={}, nodes={}), stop)
    assert driver.battle(target).outcome == (
        "cancelled" if cancelled else "prerequisites_unconfirmed"
    )

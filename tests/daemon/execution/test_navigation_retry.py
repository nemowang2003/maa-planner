import threading
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution import clearance_core
from maa_planner.daemon.execution.clearance_core import CoreClearanceDriver
from maa_planner.daemon.planning.copilot import CopilotTarget
from tests.helpers.clearance import NOW, clearance


@pytest.fixture(autouse=True)
def clock_and_retry_delay(monkeypatch):
    monkeypatch.setattr(clearance_core.time, "time", lambda: NOW)
    monkeypatch.setattr(clearance_core, "NAVIGATION_RETRY_DELAY", 0)


def scenario(outcomes):
    plan = clearance()[0]
    config = plan["targets"][0]
    target = CopilotTarget(
        **{
            key: config[key]
            for key in ("script_id", "stage_id", "code", "is_raid", "game_stage_id")
        }
    )
    results = SimpleNamespace(tasks={}, nodes={})
    calls = []
    outcomes = iter(outcomes)
    stop = threading.Event()

    def execute(tasks, *, handled):
        assert handled
        identifier = len(calls) + 1
        calls.append(tasks[0]["params"]["task_names"][0])
        status, names = next(outcomes)
        results.tasks[identifier] = {"status": status}
        results.nodes[identifier] = [(name, f"callbacks:{identifier}:{name}") for name in names]
        return [identifier]

    return CoreClearanceDriver(plan, execute, results, stop), target, calls, stop


PREPARED = [("completed", ["Home"]), ("completed", ["TerminalReady"]), ("completed", ["Activity"])]


def test_area_error_only_retries_the_unfinished_area_after_activity_already_opened():
    driver, target, calls, _ = scenario(
        [
            *PREPARED,
            ("error", ["Area"]),
            ("completed", ["Area", "Map"]),
        ]
    )
    observation = driver.navigate(target)
    assert observation.outcome == "ready"
    assert [name.rsplit("@", 1)[-1] for name in calls] == [
        "ToHome",
        "Terminal",
        "Activity",
        "Area",
        "Area",
    ]
    assert observation.evidence == ("callbacks:5:Map",)
    assert driver.navigate(target) == observation
    assert len(calls) == 5


def test_terminal_error_retries_only_the_unfinished_terminal_check():
    driver, target, calls, _ = scenario(
        [
            ("completed", ["Home"]),
            ("error", ["Terminal-Entry", "TerminalDefault", "Terminal"]),
            ("completed", ["TerminalReady"]),
            ("completed", ["Activity"]),
            ("completed", ["Map"]),
        ]
    )
    assert driver.navigate(target).outcome == "ready"
    assert [name.rsplit("@", 1)[-1] for name in calls] == [
        "ToHome",
        "Terminal",
        "TerminalReady",
        "Activity",
        "Area",
    ]


def test_repeat_error_stops_after_three_attempts_without_replaying_completed_phases():
    driver, target, calls, _ = scenario(
        [
            *PREPARED,
            ("error", ["Area"]),
            ("error", []),
            ("error", []),
        ]
    )
    assert driver.navigate(target).outcome == "navigation_failed"
    assert [name.rsplit("@", 1)[-1] for name in calls] == [
        "ToHome",
        "Terminal",
        "Activity",
        "Area",
        "Area",
        "Area",
    ]


@pytest.mark.parametrize(
    "status,names,outcome",
    [
        ("pending", [], "navigation_failed"),
        ("running", [], "navigation_failed"),
        ("completed", [], "navigation_failed"),
        ("completed", ["Locked"], "locked"),
    ],
)
def test_unknown_or_unverified_or_locked_result_is_not_blindly_replayed(status, names, outcome):
    driver, target, calls, _ = scenario([*PREPARED, (status, names)])
    assert driver.navigate(target).outcome == outcome
    assert len(calls) == 4


def test_cancel_before_navigation_starts_no_tasks():
    driver, target, calls, stop = scenario([])
    stop.set()
    assert driver.navigate(target).outcome == "cancelled"
    assert not calls

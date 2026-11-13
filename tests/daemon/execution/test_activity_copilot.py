from maa_planner.daemon.execution.activity_copilot import TargetObservation, run_clearance
from maa_planner.daemon.planning.copilot import CopilotTarget


class Driver:
    def __init__(self, battles=(), inspections=()):
        self.battles = iter(battles)
        self.inspections = iter(inspections)
        self.stopped = False
        self.calls = []

    def cancelled(self):
        return self.stopped

    def navigate(self, target):
        self.calls.append(("navigate", target.stage_id, target.is_raid))
        return next(self.inspections, TargetObservation("ready", ("ui:1",)))

    def battle(self, target):
        self.calls.append(("battle", target.stage_id, target.is_raid))
        return TargetObservation(next(self.battles), ("callbacks.jsonl:1",))


def targets():
    return (CopilotTarget(1, "stage1", "E-1", False), CopilotTarget(2, "stage2", "E-2", False))


def test_retry_current_stage_three_defeats_stop_only_this_phase():
    driver = Driver(("failed", "failed", "failed", "completed"))
    report = run_clearance(targets(), frozenset(), driver)
    assert report.outcome == "stopped"
    assert report.warning is not None
    assert report.warning["reason"] == "three_consecutive_defeats"
    assert report.warning is not None
    assert report.warning["failures"] == 3
    assert [c for c in driver.calls if c[0] == "battle"] == [("battle", "stage1", False)] * 3


def test_success_advances_and_next_stage_gets_a_new_failure_counter():
    driver = Driver(("failed", "failed", "completed", "failed", "completed"))
    report = run_clearance(targets(), frozenset(), driver)
    assert report.outcome == "completed" and report.warning is None
    assert [r.failures for r in report.results] == [2, 1]


def test_confirmed_history_is_skipped_and_unknown_history_is_inspected():
    driver = Driver(("completed",))
    report = run_clearance(targets(), frozenset({("stage1", False)}), driver)
    assert report.outcome == "completed"
    assert driver.calls == [("navigate", "stage2", False), ("battle", "stage2", False)]


def test_ui_history_cannot_mark_stage_completed_or_start_battle():
    driver = Driver(inspections=(TargetObservation("completed", ("ui:1",)),))
    report = run_clearance(targets()[:1], frozenset(), driver)
    assert report.outcome == "stopped" and not report.results
    assert len(driver.calls) == 1
    driver = Driver(inspections=(TargetObservation("ready"),))
    report = run_clearance(targets()[:1], frozenset(), driver)
    assert report.warning is not None
    assert report.outcome == "stopped" and report.warning["reason"] == "unknown"


def test_locked_or_navigation_failure_returns_to_caller_without_battles():
    for outcome in ("locked", "navigation_failed", "unknown"):
        driver = Driver(inspections=(TargetObservation(outcome, ("ui:1",)),))
        report = run_clearance(targets(), frozenset(), driver)
        assert report.warning is not None
        assert report.outcome == "stopped" and report.warning["reason"] == outcome
        assert len(driver.calls) == 1


def test_unclassified_error_is_not_retried_as_defeat():
    driver = Driver(("unknown", "completed"))
    report = run_clearance(targets(), frozenset(), driver)
    assert report.warning is not None
    assert report.warning["reason"] == "battle_unknown"
    assert len(driver.calls) == 2


def test_cancellation_prevents_inspection_and_battle():
    driver = Driver()
    driver.stopped = True
    assert run_clearance(targets(), frozenset(), driver).outcome == "cancelled"
    assert not driver.calls

from dataclasses import asdict, replace

import pytest

from maa_planner.daemon.execution.activity_copilot import ClearanceReport, ClearanceResult
from maa_planner.daemon.planning.copilot import CopilotTarget
from maa_planner.daemon.state import StateStore


def report(outcome="completed", *, warning=None, evidence=("callbacks.jsonl:1",)):
    return ClearanceReport(
        outcome,
        (
            ClearanceResult(CopilotTarget(1, "s1", "E-1", False), "completed", 0, evidence),
            ClearanceResult(CopilotTarget(2, "s1#f#", "E-1", True), "unknown", 0, ()),
        ),
        warning,
    )


def test_clearance_success_survives_restart_and_is_isolated_by_activity_and_account(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    store.record_clearance("nemo", "event1", report(), now=100)
    store.close()
    store = StateStore(path)
    assert store.completed_stages("nemo", "event1") == {("s1", False)}
    assert not store.completed_stages("other", "event1")
    assert not store.completed_stages("nemo", "rerun-event1")
    store.close()


def test_no_evidence_never_creates_completion_or_erases_old_success(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.record_clearance("nemo", "event1", report(evidence=()), now=100)
    assert not store.completed_stages("nemo", "event1")
    store.record_clearance("nemo", "event1", report(), now=101)
    store.record_clearance("nemo", "event1", ClearanceReport("cancelled", ()), now=102)
    assert store.completed_stages("nemo", "event1") == {("s1", False)}
    store.close()


def test_farming_gate_survives_report_roundtrip_and_event_persistence(tmp_path):
    gate = {"allowed": False, "reason": "stages_incomplete", "missing": [["s1", True]]}
    observation = replace(report(), farming_gate=gate)
    restored = ClearanceReport.from_dict(asdict(observation))
    assert restored == observation
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    store.record_clearance("nemo", "event1", restored, now=100)
    store.close()
    store = StateStore(path)
    event = next(e for e in store.events(0) if e["kind"] == "activity_copilot_result")
    assert event["data"]["farming_gate"] == gate
    assert store.completed_stages("nemo", "event1") == {("s1", False)}
    store.close()


def test_three_losses_raise_deduplicated_alert_and_other_events_do_not_resolve_it(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    failed = ClearanceReport("stopped", (), {"reason": "three_consecutive_defeats", "failures": 3})
    store.record_clearance("nemo", "event1", failed, now=100)
    identifier = store.alerts("nemo")[0]["alert_id"]
    store.record_clearance("nemo", "event1", failed, now=101)
    assert store.alerts("nemo")[0]["alert_id"] == identifier
    store.record_clearance("nemo", "event2", report(), now=102)
    assert store.alerts("nemo")[0]["alert_id"] == identifier
    store.record_clearance("nemo", "event1", ClearanceReport("cancelled", ()), now=103)
    assert store.alerts("nemo")[0]["alert_id"] == identifier
    store.record_clearance("nemo", "event1", report(), now=104)
    assert not store.alerts("nemo")
    store.close()


@pytest.mark.parametrize("outcome", ["unknown", "completed"])
def test_zero_id_barrier_does_not_bypass_explicit_ledger_policy(tmp_path, outcome):
    store = StateStore(tmp_path / "state.sqlite3")
    observed = ClearanceReport(
        "completed",
        (
            ClearanceResult(
                CopilotTarget(0, "tutorial", "EV-TR-1", False, "tutorial"),
                outcome,
                0,
                ("callbacks.jsonl:12", "callbacks.jsonl:13"),
            ),
        ),
    )
    store.record_clearance("nemo", "event", observed, now=100)
    assert not store.completed_stages("nemo", "event")
    assert not store.stage_progress("nemo", "event")
    assert store.events(0)[-1]["data"]["results"][0]["outcome"] == outcome
    store.close()


def test_explicit_story_evidence_is_saved_and_survives_restart_without_certifying_barriers(
    tmp_path,
):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    observed = ClearanceReport(
        "running",
        (
            ClearanceResult(
                CopilotTarget(0, "story", "EV-ST-4", False, "story"),
                "completed",
                0,
                (
                    "callbacks.jsonl:1",
                    "callbacks.jsonl:2",
                    "callbacks.jsonl:3",
                    "callbacks.jsonl:4",
                ),
            ),
            ClearanceResult(
                CopilotTarget(0, "tutorial", "EV-TR-1", False, "tutorial"),
                "completed",
                0,
                ("callbacks.jsonl:5",),
            ),
        ),
    )
    store.record_clearance("nemo", "event", observed, now=100, story_stages=frozenset({"story"}))
    store.close()
    store = StateStore(path)
    assert store.completed_stages("nemo", "event") == {("story", False)}
    assert store.stage_progress("nemo", "event")[0]["source"] == "story"
    store.close()


def test_daemon_uses_frozen_story_targets_and_next_plan_skips_completed_story(tmp_path):
    import json

    from maa_planner.daemon.coordinator import Daemon
    from maa_planner.daemon.execution.tasks import CoreTasks
    from maa_planner.daemon.planning.clearance import prepare_clearance
    from maa_planner.daemon.planning.tutorials import add_tutorial_barriers
    from tests.helpers.clearance import NOW, activity
    from tests.helpers.execution import request as make_request
    from tests.helpers.stories import story_catalog

    scripts, catalog = story_catalog()
    source = add_tutorial_barriers(
        prepare_clearance(scripts, catalog, activity(), now=NOW), catalog, now=NOW
    )
    store = StateStore(tmp_path / "state.sqlite3")
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control.json", root=tmp_path / "daemon")
    daemon.store = store
    scheduler = daemon.daily
    request = make_request(tmp_path)
    assert request.core is not None
    document = {
        "tasks": [{"type": "Fight"}],
        "activity_copilot": {**source[0], "before_task": 0},
        "resource_tasks": source[2],
    }
    request = replace(
        request,
        core=replace(request.core, tasks=CoreTasks(json.dumps(document), json.dumps(source[1]))),
    )
    evidence = ("callbacks.jsonl:1", "callbacks.jsonl:2", "callbacks.jsonl:3", "callbacks.jsonl:4")
    result = ClearanceResult(
        CopilotTarget(0, "tail", "EV-ST-2", False, "tail"), "completed", 0, evidence
    )
    scheduler.clearance_report(
        request, {"activity_id": "event", **asdict(ClearanceReport("running", (result,)))}
    )
    assert store.completed_stages("nemo", "event") == {("tail", False)}
    assert store.stage_progress("nemo", "event")[0]["evidence"] == list(evidence)
    store.close()
    store = StateStore(tmp_path / "state.sqlite3")
    following = prepare_clearance(scripts, catalog, activity(), now=NOW)
    following[0]["completed"] = [list(v) for v in store.completed_stages("nemo", "event")]
    planned = add_tutorial_barriers(following, catalog, now=NOW)[0]
    assert "tail" not in {entry["stage_id"] for entry in planned["targets"]}
    assert "tail2" in {entry["stage_id"] for entry in planned["targets"]}
    store.close()

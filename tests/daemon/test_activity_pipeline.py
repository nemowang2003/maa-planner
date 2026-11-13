"""Exercise real planning, worker execution, progress registration and retry selection."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.execution.copilot import _Source
from maa_planner.daemon.planning.schedule import ScheduledOccurrence
from maa_planner.daemon.policy import ConfigSnapshot
from maa_planner.daemon.scheduled_retry import ScheduledRetries
from maa_planner.daemon.state import StateStore
from tests.helpers.activity_pipeline import ActivityPipeline
from tests.helpers.clearance_core import ClearanceCore
from tests.helpers.execution import request


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite3")
    scenario = ActivityPipeline(tmp_path, store)
    monkeypatch.setattr(_Source, "get", lambda self, url: scenario.response(url))
    monkeypatch.setattr("time.time", lambda: 2000)
    yield scenario
    store.close()


@pytest.mark.parametrize(
    "now,available,fights", [(2000, False, True), (2500, False, False), (2500, True, True)]
)
def test_farming_uses_only_open_regions_and_rechecks_when_s_opens(
    pipeline, monkeypatch, now, available, fights
):
    monkeypatch.setattr("time.time", lambda: now)
    pipeline.confirm_open()
    plan = asyncio.run(pipeline.prepare(now, available=available))
    core = ClearanceCore(stage_ids=pipeline.stage_ids)
    code, report = pipeline.execute(plan, core, "run")
    assert code == 0
    assert ("Fight" in [kind for kind, _ in core.appended]) is fights
    assert core.visited == ([("EV-S-1", False), ("EV-S-1", True)] if available else [])
    assert report["activity_copilot_results"][0]["farming_gate"]["allowed"] is fights
    assert [
        kind for kind, _ in core.appended if kind in {"StartUp", "Recruit", "Award", "CloseDown"}
    ] == ["StartUp", "Recruit", "Award", "CloseDown"]
    assert not report["activity_shop_results"]
    assert "activity_shops" not in json.loads(plan.tasks.document)
    if now == 2000:
        assert not pipeline.reads
        # Shops intentionally retain the full scope even before S opens.
        assert ["act_s", False] in plan.shops[0]["required"]
    elif available:
        assert pipeline.store.completed_stages("nemo", "event") == frozenset(
            (stage, raid) for stage in ("act_01", "act_02", "act_s") for raid in (False, True)
        )
    else:
        assert report["activity_copilot_results"][0]["farming_gate"]["missing"] == [
            ["act_s", False],
            ["act_s", True],
        ]


def test_disconnect_replans_current_farming_and_later_work_from_registered_progress(
    pipeline, monkeypatch
):
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

    plan = asyncio.run(pipeline.prepare(2000))
    first = DisconnectCore(stage_ids=pipeline.stage_ids)
    registered = []
    code, report = pipeline.execute(
        plan,
        first,
        "first",
        certified=lambda: registered.append(pipeline.store.completed_stages("nemo", "event")),
    )
    assert code == 1 and report["session_failure"]["kind"] == "game_disconnected"
    assert registered[0] == frozenset({("act_01", False)})
    assert pipeline.store.completed_stages("nemo", "event") == registered[0]
    assert not any(kind in {"Fight", "Award", "CloseDown"} for kind, _ in first.appended)

    req = request(pipeline.root)
    req = replace(req, core=replace(req.core, tasks=plan.tasks))
    occurrence = ScheduledOccurrence("daily", "nemo", 2000, 3000)
    snapshot = ConfigSnapshot(req.config_revision, b'[[profiles]]\nname="nemo"\n')
    pipeline.store.configuration(snapshot.revision, snapshot.content)
    pipeline.store.claim_schedule(occurrence, snapshot.revision)
    retries = ScheduledRetries(SimpleNamespace(store=pipeline.store))
    retries.finished(
        occurrence,
        snapshot,
        req,
        RunResult(
            req.attempt_id,
            report["status"],
            business_results=tuple(report["business_results"]),
            session_failure=report["session_failure"],
        ),
    )
    retry = pipeline.store.scheduled_retries()[0]
    assert retry["pending"] == ["lifecycle:closedown", "task:1", "task:2"]
    assert retry["due_at"] == 2300

    monkeypatch.setattr("time.time", lambda: retry["due_at"])
    replanned = asyncio.run(pipeline.prepare(retry["due_at"], pending=frozenset(retry["pending"])))
    second = ClearanceCore(stage_ids=pipeline.stage_ids)
    code, report = pipeline.execute(replanned, second, "second")
    assert code == 0
    assert second.visited == [("EV-1", True), ("EV-EX-2", False), ("EV-EX-2", True)]
    assert [
        kind
        for kind, _ in second.appended
        if kind in {"StartUp", "Recruit", "Fight", "Award", "CloseDown"}
    ] == ["StartUp", "Fight", "Award", "CloseDown"]
    assert pipeline.store.completed_stages("nemo", "event") == frozenset(
        (stage, raid) for stage in ("act_01", "act_02") for raid in (False, True)
    )
    req = replace(req, core=replace(req.core, tasks=replanned.tasks))
    retries.finished(
        occurrence,
        snapshot,
        req,
        RunResult("retry", report["status"], business_results=tuple(report["business_results"])),
    )
    assert not pipeline.store.scheduled_retries()

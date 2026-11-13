import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.execution.tasks import CoreTasks
from maa_planner.daemon.planning.schedule import ScheduledOccurrence
from maa_planner.daemon.policy import ConfigSnapshot
from maa_planner.daemon.scheduled_retry import ScheduledRetries
from maa_planner.daemon.state import StateStore
from tests.helpers.async_tools import eventually
from tests.helpers.execution import request
from tests.helpers.scheduling import Clock, document, running, wake


def result(req, statuses, *, status="failed", session_failure=None):
    doc = json.loads(req.core.tasks.document)
    keys = doc.get("business_ids", [f"task:{i}" for i in range(len(doc["tasks"]))])
    return RunResult(
        req.attempt_id,
        status,
        session_failure=session_failure,
        business_results=tuple(
            {
                "id": key,
                "status": state,
                **(
                    {"error": {"kind": "core", "evidence": "callbacks.jsonl:4"}}
                    if state == "error"
                    else {}
                ),
            }
            for key, state in zip(keys, statuses, strict=True)
        ),
    )


def isolated(tmp_path, monkeypatch, kinds=("StartUp", "Recruit", "Award", "CloseDown")):
    monkeypatch.setattr("time.time", lambda: 100)
    store = StateStore(tmp_path / "state.sqlite3")
    req = request(tmp_path)
    req = replace(
        req,
        core=replace(
            req.core,
            tasks=CoreTasks(
                json.dumps(
                    {
                        "tasks": [{"type": t} for t in kinds],
                        "business_ids": [
                            {
                                "StartUp": "lifecycle:startup",
                                "CloseDown": "lifecycle:closedown",
                            }.get(kind, f"task:{i}")
                            for i, kind in enumerate(kinds)
                        ],
                    }
                )
            ),
        ),
    )
    occurrence = ScheduledOccurrence("daily", "nemo", 100, 10000)
    snapshot = ConfigSnapshot(req.config_revision, b'[[profiles]]\nname="nemo"\n')
    store.configuration(snapshot.revision, snapshot.content)
    store.claim_schedule(occurrence, snapshot.revision)
    scheduler = SimpleNamespace(
        store=store,
        control=SimpleNamespace(is_temporary=lambda _: False, snapshot=lambda _: snapshot),
    )
    return store, ScheduledRetries(scheduler), req, occurrence, snapshot


def test_disconnect_keeps_current_business_and_unexecuted_later_tasks(tmp_path, monkeypatch):
    store, manager, req, occurrence, snapshot = isolated(
        tmp_path, monkeypatch, ("StartUp", "Award", "Infrast", "Recruit")
    )
    manager.finished(
        occurrence,
        snapshot,
        req,
        result(
            req,
            ["completed", "completed", "error", "pending"],
            session_failure={"kind": "game_disconnected"},
        ),
    )
    retry = store.scheduled_retries()[0]
    assert retry["pending"] == ["task:2", "task:3"]
    assert "request" not in retry
    store.close()


def test_single_business_error_does_not_repeat_successful_independent_work(tmp_path, monkeypatch):
    store, manager, req, occurrence, snapshot = isolated(tmp_path, monkeypatch)
    manager.finished(
        occurrence, snapshot, req, result(req, ["completed", "error", "completed", "completed"])
    )
    assert store.scheduled_retries()[0]["pending"] == ["task:1"]
    store.close()


def test_farming_suffix_error_retries_entire_business_without_storing_phases(tmp_path, monkeypatch):
    store, manager, req, occurrence, snapshot = isolated(
        tmp_path, monkeypatch, ("StartUp", "Fight", "Award", "CloseDown")
    )
    report = result(req, ["completed", "error", "completed", "completed"])
    report = replace(report, task_checkpoints=({"index": 1, "status": "completed"},))
    manager.finished(occurrence, snapshot, req, report)
    retry = store.scheduled_retries()[0]
    assert retry["pending"] == ["task:1"]
    assert not {"request", "activity_copilot", "activity_shops", "completed"} & retry.keys()
    store.close()


def test_waiting_recovery_survives_restart_but_running_attempt_does_not_replay(
    tmp_path, monkeypatch
):
    store, manager, req, occurrence, snapshot = isolated(tmp_path, monkeypatch)
    manager.finished(
        occurrence, snapshot, req, result(req, ["completed", "error", "completed", "completed"])
    )
    retry = store.scheduled_retries()[0]
    store.close()
    store = StateStore(tmp_path / "state.sqlite3")
    store.recover_schedules()
    assert store.scheduled_retries()[0] == retry
    store.save_scheduled_retry({**retry, "status": "running"})
    store.schedule_update(occurrence.id, "preparing")
    store.recover_schedules()
    assert store.scheduled_retries()[0]["status"] == "recovery_required"
    store.close()


@pytest.mark.parametrize("status", ["cancelled", "recovery_required", "blocked", "failed"])
def test_unknown_or_cancelled_execution_does_not_create_recovery(tmp_path, monkeypatch, status):
    store, manager, req, occurrence, snapshot = isolated(tmp_path, monkeypatch)
    manager.finished(occurrence, snapshot, req, RunResult(req.attempt_id, status))
    assert not store.scheduled_retries()
    store.close()


def test_retry_delays_are_measured_from_each_completed_attempt_and_bounded(tmp_path, monkeypatch):
    store, manager, req, occurrence, snapshot = isolated(tmp_path, monkeypatch)
    for number, delay in enumerate((300, 600, 900), 1):
        monkeypatch.setattr("time.time", lambda: 100 + number * 120)
        manager.finished(
            occurrence, snapshot, req, result(req, ["completed", "error", "completed", "completed"])
        )
        retry = store.scheduled_retries()[0]
        assert retry["due_at"] == 100 + number * 120 + delay
        assert retry["status"] == "waiting"
    manager.finished(
        occurrence, snapshot, req, result(req, ["completed", "error", "completed", "completed"])
    )
    assert not store.scheduled_retries()
    exhausted = store.alerts("nemo")[0]["details"]
    assert exhausted["status"] == "exhausted" and exhausted["number"] == 3
    assert "due_at" not in exhausted and "pending" not in exhausted
    assert store.history.get("retry", occurrence.id)["status"] == "exhausted"
    store.close()


@pytest.mark.parametrize(
    "guard",
    [
        "expired",
        "config_changed",
        "busy",
        "maintenance",
        "maintenance_pending",
        "draining",
        "config_error",
    ],
)
def test_recovery_revalidates_expiry_configuration_and_busy_guards(tmp_path, monkeypatch, guard):
    store, manager, req, occurrence, snapshot = isolated(tmp_path, monkeypatch)
    manager.finished(
        occurrence, snapshot, req, result(req, ["completed", "error", "completed", "completed"])
    )
    daemon = SimpleNamespace(
        configuration=snapshot,
        config_error=None,
        stopping=threading.Event(),
        draining=False,
        maintenance=None,
        maintenance_pending=False,
        running={},
        _unit_busy=lambda _: guard == "busy",
    )
    manager.scheduler.daemon = daemon
    manager.scheduler.preparing = {}
    manager.scheduler.pending = {}
    monkeypatch.setattr("time.time", lambda: 10001 if guard == "expired" else 400)
    if guard == "config_changed":
        daemon.configuration = replace(snapshot, revision="changed")
    if guard == "maintenance":
        daemon.maintenance = "maintenance"
    if guard == "maintenance_pending":
        daemon.maintenance_pending = True
    if guard == "draining":
        daemon.draining = True
    if guard == "config_error":
        daemon.config_error = "bad"
    manager.dispatch()
    if guard in {"expired", "config_changed"}:
        assert not store.scheduled_retries()
    else:
        assert store.scheduled_retries()[0]["status"] == "waiting"
    assert not manager.scheduler.pending
    store.close()


def simple_content():
    return (
        document(("nemo",))
        .replace('type="Fight"', 'type="Recruit"')
        .replace(
            'params={stage="1-7", medicine=0, stone=0, expiring_medicine=999, medicine_expire_days=2}',
            "",
        )
    )


def test_timer_starts_after_current_tasks_finish_and_replans_only_failed_business(
    tmp_path, monkeypatch
):
    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        async with running(tmp_path, monkeypatch, clock, content=simple_content()) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            first = state.workers[0]
            first.result = result(first.request, ["completed", "error", "completed"])
            wake(state, clock, "2026-11-02T12:20:00")
            await asyncio.sleep(0)
            assert not state.daemon.store.scheduled_retries()
            first.finish.set()
            await eventually(lambda: not state.daemon.running)
            wake(state, clock, "2026-11-02T12:24:59")
            await asyncio.sleep(0)
            assert len(state.workers) == 1
            wake(state, clock, "2026-11-02T12:25:00")
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            restored = state.workers[1]
            assert [
                t["type"] for t in json.loads(restored.request.core.tasks.document)["tasks"]
            ] == ["StartUp", "Recruit", "CloseDown"]
            assert restored.request.options.resume_game_session
            assert len(state.calls) == 2
            restored.result = result(restored.request, ["completed"] * 3, status="succeeded")
            restored.finish.set()
            await eventually(lambda: not state.daemon.running)
            assert not state.daemon.store.scheduled_retries()

    asyncio.run(scenario())


def test_recovery_replans_after_restart_and_skips_successful_award(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        content = simple_content().replace(
            'type="Recruit"', 'type="Award"\n[[schedules.tasks]]\ntype="Recruit"'
        )
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            first = state.workers[0]
            first.result = result(first.request, ["completed", "completed", "error", "completed"])
            first.finish.set()
            await eventually(lambda: not state.daemon.running)
        clock.set("2026-11-02T12:15:00")
        async with running(tmp_path, monkeypatch, clock) as restored:
            await eventually(lambda: len(restored.workers) == 1 and restored.workers[0].allowed)
            worker = restored.workers[0]
            assert [t["type"] for t in json.loads(worker.request.core.tasks.document)["tasks"]] == [
                "StartUp",
                "Recruit",
                "CloseDown",
            ]
            worker.finish.set()
            await eventually(lambda: not restored.daemon.running)

    asyncio.run(scenario())


def test_retry_reads_current_base_file_instead_of_replaying_old_attachment(tmp_path, monkeypatch):
    base = tmp_path / "base.json"
    base.write_text('{"plans":[{"name":"old"}]}')
    content = simple_content().replace(
        'type="Recruit"', f'type="Infrast"\nfile={json.dumps(str(base))}'
    )

    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            first = state.workers[0]
            assert (
                json.loads(first.request.core.tasks.attachments)["task-0.json"]["plans"][0]["name"]
                == "old"
            )
            first.result = result(first.request, ["completed", "error", "completed"])
            first.finish.set()
            await eventually(lambda: not state.daemon.running)
            base.write_text('{"plans":[{"name":"current"}]}')
            wake(state, clock, "2026-11-02T12:15:00")
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            restored = state.workers[1]
            assert [
                t["type"] for t in json.loads(restored.request.core.tasks.document)["tasks"]
            ] == ["StartUp", "Infrast", "CloseDown"]
            assert (
                json.loads(restored.request.core.tasks.attachments)["task-0.json"]["plans"][0][
                    "name"
                ]
                == "current"
            )
            restored.finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())


def test_failed_replanning_consumes_next_backoff_without_launching_stale_request(
    tmp_path, monkeypatch
):
    base = tmp_path / "base.json"
    base.write_text('{"plans":[{}]}')
    content = simple_content().replace(
        'type="Recruit"', f'type="Infrast"\nfile={json.dumps(str(base))}'
    )

    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            first = state.workers[0]
            first.result = result(first.request, ["completed", "error", "completed"])
            first.finish.set()
            await eventually(lambda: not state.daemon.running)
            base.write_text("invalid JSON")
            wake(state, clock, "2026-11-02T12:15:00")
            await eventually(lambda: state.daemon.store.scheduled_retries()[0]["number"] == 2)
            retry = state.daemon.store.scheduled_retries()[0]
            assert retry["status"] == "waiting" and retry["due_at"] == clock.now() + 600
            assert len(state.workers) == 1

    asyncio.run(scenario())


def test_missing_pending_business_ids_isolates_recovery_instead_of_guessing(tmp_path, monkeypatch):
    store, manager, req, occurrence, snapshot = isolated(tmp_path, monkeypatch)
    manager.finished(
        occurrence, snapshot, req, result(req, ["completed", "error", "completed", "completed"])
    )
    retry = store.scheduled_retries()[0]
    retry.pop("pending")
    store.save_scheduled_retry(retry)
    manager.scheduler.daemon = SimpleNamespace(configuration=snapshot)
    manager.dispatch()
    assert store.scheduled_retries()[0]["status"] == "recovery_required"
    store.close()


def test_expiry_while_replanning_closes_recovery_instead_of_leaving_running_state(
    tmp_path, monkeypatch
):
    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        calls = []
        gate = asyncio.Event()

        async def loader():
            calls.append(True)
            if len(calls) == 2:
                await gate.wait()

        async with running(
            tmp_path, monkeypatch, clock, content=simple_content(), loader=loader
        ) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            first = state.workers[0]
            identifier = first.request.job_id
            first.result = result(first.request, ["completed", "error", "completed"])
            first.finish.set()
            await eventually(lambda: not state.daemon.running)
            wake(state, clock, "2026-11-02T12:15:00")
            await eventually(lambda: len(calls) == 2)
            wake(state, clock, "2026-11-02T15:40:00")
            await eventually(lambda: not state.daemon.store.scheduled_retries())
            assert state.daemon.store.scheduled_run(identifier)["status"] == "expired"
            gate.set()
            for worker in state.workers:
                worker.finish.set()

    asyncio.run(scenario())


def test_failed_session_cleanup_does_not_replay_successful_business(tmp_path, monkeypatch):
    store, manager, req, occurrence, snapshot = isolated(
        tmp_path, monkeypatch, ("StartUp", "Award", "CloseDown")
    )
    manager.finished(occurrence, snapshot, req, result(req, ["completed", "completed", "error"]))
    assert store.scheduled_retries()[0]["pending"] == ["lifecycle:closedown"]
    store.close()


def test_insufficient_window_does_not_report_a_retry_that_never_started(tmp_path, monkeypatch):
    store, manager, req, occurrence, snapshot = isolated(tmp_path, monkeypatch)
    occurrence = replace(occurrence, expires_at=200)
    manager.finished(
        occurrence, snapshot, req, result(req, ["completed", "error", "completed", "completed"])
    )
    assert not store.scheduled_retries()
    details = store.alerts("nemo")[0]["details"]
    assert details["number"] == 0 and details["status"] == "exhausted"
    assert "due_at" not in details
    store.close()

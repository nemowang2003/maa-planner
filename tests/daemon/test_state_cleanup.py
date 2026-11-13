import sqlite3

import pytest

from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.planning.schedule import ScheduledOccurrence
from maa_planner.daemon.state import StateStore
from tests.helpers.execution import request


def count(store, table):
    return store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_terminal_execution_leaves_only_logs_and_schedule_cursor(tmp_path):
    req = request(tmp_path)
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    store.configuration(req.config_revision, b"active")
    store.enqueue(req)
    store.start(req.attempt_id)
    store.prepared_request(req)
    store.reserve_operation("op", {"action": "run", "target": "nemo"})
    store.reserve_copilot("op", {}, req.attempt_id)
    occurrence = ScheduledOccurrence("daily", "nemo", 100, 200)
    store.claim_schedule(occurrence, req.config_revision)
    result = RunResult(req.attempt_id, "succeeded")
    store.finish(result)
    store.operation_update("op", "succeeded", result={"value": 1})
    store.schedule_update(occurrence.id, "succeeded")
    for table in (
        "attempts",
        "prepared_executions",
        "copilot_requests",
        "operations",
        "scheduled_runs",
    ):
        assert count(store, table) == 0, table
    assert not store.claim_schedule(occurrence, req.config_revision)
    store.close()
    store = StateStore(path)
    assert store.results() == [result]  # Queries read logs, never rehydrate terminal state.
    operation = store.operation("op")
    assert operation is not None and operation["status"] == "succeeded"
    assert store.prepared_execution(req.attempt_id) == req
    assert not store.claim_schedule(occurrence, req.config_revision)
    assert store.claim_schedule(ScheduledOccurrence("daily", "nemo", 200, 300), req.config_revision)
    store.close()


def test_events_and_resolved_alerts_are_not_sqlite_history(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.observe_alert("nemo", "test", "subject", {"error": "failed"}, now=1)
    store.observe_alert("nemo", "test", "subject", None, now=2)
    assert count(store, "alerts") == 0
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "events" not in tables
    assert [e["data"]["transition"] for e in store.events(0)] == ["raised", "resolved"]
    store.close()


def test_log_write_failure_preserves_live_attempt(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite3")
    req = request(tmp_path)
    store.enqueue(req)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store.history, "save", fail)
    with pytest.raises(OSError, match="disk full"):
        store.finish(RunResult(req.attempt_id, "succeeded"))
    assert store.unfinished() == [req.attempt_id]
    store.close()


def test_version_13_migration_moves_history_but_keeps_live_recovery_and_business_state(tmp_path):
    import json
    from dataclasses import replace

    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    req = request(tmp_path)
    store.configuration(req.config_revision, b"current")
    store.enqueue(req)
    store.start(req.attempt_id)
    store.prepared_request(req)
    live = replace(req, attempt_id="live")
    store.enqueue(live)
    store.reserve_operation("finished", {"action": "run", "target": "nemo"})
    occurrence = ScheduledOccurrence("daily", "nemo", 100, 200)
    store.claim_schedule(occurrence, req.config_revision)
    store.close()
    store.history.path.unlink()  # A real v13 database has no structured history log.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 13")
        db.execute(
            "CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        db.execute("INSERT INTO events VALUES (7,'old',?)", ('{"value":1}',))
        db.execute(
            "UPDATE attempts SET status='succeeded',result=? WHERE id=?",
            (json.dumps({"attempt_id": req.attempt_id, "status": "succeeded"}), req.attempt_id),
        )
        db.execute("UPDATE operations SET status='succeeded' WHERE id='finished'")
        db.execute("UPDATE scheduled_runs SET status='succeeded'")
        db.execute(
            "INSERT INTO scheduled_retries VALUES (?,?)",
            (
                occurrence.id,
                json.dumps(
                    {
                        "id": occurrence.id,
                        "status": "exhausted",
                        "due_at": 300,
                        "pending": ["task:1"],
                        "revision": req.config_revision,
                    }
                ),
            ),
        )
        alert = {
            "alert_id": "legacy",
            "profile": "nemo",
            "category": "task_retry",
            "subject": occurrence.id,
            "active": True,
            "details": {"status": "exhausted", "number": 4, "due_at": 999, "pending": ["task:1"]},
        }
        db.execute(
            "INSERT INTO alerts VALUES (?,?,?,?)",
            ("nemo", "task_retry", occurrence.id, json.dumps(alert)),
        )
        db.execute("INSERT INTO stage_completions VALUES ('nemo','event','stage',0,'{}')")
        db.execute(
            "INSERT INTO activity_shop_navigation VALUES ('event',100,'{\"status\":\"disabled\"}')"
        )
    store = StateStore(path)
    assert store.unfinished() == ["live"]
    assert count(store, "attempts") == 1
    assert count(store, "prepared_executions") == 0
    assert count(store, "operations") == 0
    assert count(store, "scheduled_runs") == 0
    assert not store.scheduled_retries()
    details = store.alerts("nemo")[0]["details"]
    assert details["number"] == 3 and "due_at" not in details and "pending" not in details
    assert store.completed_stages("nemo", "event") == frozenset({("stage", False)})
    assert count(store, "activity_shop_navigation") == 1
    assert store.events(0)[0] == {"sequence": 7, "kind": "old", "data": {"value": 1}}
    store.event("new", {})
    assert store.events(7)[-1]["sequence"] > 7
    assert not store.claim_schedule(occurrence, req.config_revision)
    assert store.results()[0].status == "succeeded"
    retry = store.history.get("retry", occurrence.id)
    assert retry is not None and "due_at" not in retry
    store.close()


def test_history_log_repairs_incomplete_tail_and_keeps_cursor(tmp_path):
    from maa_planner.daemon.history import HistoryLog

    path = tmp_path / "history.jsonl"
    log = HistoryLog(path)
    log.event("one", {})
    with path.open("ab") as stream:
        stream.write(b'{"kind":')
    log = HistoryLog(path)
    log.event("two", {})
    assert [v["sequence"] for v in log.events(0, 100)] == [1, 2]


def test_terminal_schedule_reason_is_updated_in_logs_without_retaining_sql_row(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    occurrence = ScheduledOccurrence("daily", "nemo", 100, 200)
    store.claim_schedule(occurrence, "revision")
    store.schedule_update(occurrence.id, "failed", error="core error")
    store.schedule_update(occurrence.id, "failed", error="retry exhausted")
    value = store.scheduled_run(occurrence.id)
    assert value is not None and value["error"] == "retry exhausted"
    assert count(store, "scheduled_runs") == 0
    store.close()


def test_only_current_or_in_use_configuration_snapshots_stay_in_sqlite(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    req = request(tmp_path)
    store.configuration(req.config_revision, b"old")
    store.enqueue(req)
    store.configuration("new", b"current")
    assert count(store, "configurations") == 2
    store.finish(RunResult(req.attempt_id, "succeeded"))
    assert count(store, "configurations") == 1
    assert store.configuration_content(req.config_revision) == b"old"
    store.close()


def test_migration_log_failure_preserves_old_execution_for_retrying_upgrade(tmp_path, monkeypatch):
    import json

    from maa_planner.daemon.history import HistoryLog

    path = tmp_path / "state.sqlite3"
    req = request(tmp_path)
    store = StateStore(path)
    store.enqueue(req)
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=13")
        db.execute(
            "CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        db.execute(
            "UPDATE attempts SET status='succeeded',result=?",
            (json.dumps({"attempt_id": req.attempt_id, "status": "succeeded"}),),
        )
    original = HistoryLog.save

    def fail(log, kind, key, value):
        if kind == "attempt":
            raise OSError("disk full")
        original(log, kind, key, value)

    with monkeypatch.context() as patch:
        patch.setattr(HistoryLog, "save", fail)
        with pytest.raises(OSError, match="disk full"):
            StateStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == 13
    store = StateStore(path)
    assert count(store, "attempts") == 0
    assert store.results()[0].status == "succeeded"
    store.close()


def test_failed_log_flush_removes_uncommitted_record_and_keeps_event_sequence(
    tmp_path, monkeypatch
):
    from maa_planner.daemon import history

    path = tmp_path / "history.jsonl"
    log = history.HistoryLog(path)
    log.event("first", {})
    sync = history.os.fsync
    calls = 0

    def fail_once(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("flush failed")
        sync(fd)

    monkeypatch.setattr(history.os, "fsync", fail_once)
    with pytest.raises(OSError, match="flush failed"):
        log.event("uncommitted", {})
    log.event("next", {})
    reopened = history.HistoryLog(path)
    assert [(v["sequence"], v["kind"]) for v in reopened.events(0, 100)] == [
        (1, "first"),
        (2, "next"),
    ]


@pytest.mark.parametrize("failure_point", ["cleanup", "commit"])
def test_daemon_restart_finishes_logged_result_after_sqlite_failure(
    tmp_path, monkeypatch, failure_point
):
    import asyncio

    from maa_planner.daemon.coordinator import Daemon
    from tests.helpers.async_tools import eventually

    root = tmp_path / "state"
    path = root / "state.sqlite3"
    req = request(tmp_path)
    store = StateStore(path)
    store.enqueue(req)
    store.start(req.attempt_id)
    store.prepared_request(req)
    store.reserve_operation("op", {"action": "run", "target": "nemo"})
    store.reserve_copilot("op", {}, req.attempt_id)
    if failure_point == "cleanup":
        store.db.execute(
            "CREATE TRIGGER reject_cleanup BEFORE DELETE ON attempts BEGIN SELECT RAISE(ABORT,'cleanup rejected'); END"
        )
    else:
        store.db.execute(
            "CREATE TABLE cleanup_guard (attempt_id TEXT REFERENCES attempts(id) DEFERRABLE INITIALLY DEFERRED)"
        )
        store.db.execute("INSERT INTO cleanup_guard VALUES (?)", (req.attempt_id,))
        store.db.commit()
    result = RunResult(req.attempt_id, "succeeded")
    with pytest.raises(sqlite3.IntegrityError):
        store.finish(result)
    assert store.unfinished() == [req.attempt_id]
    assert store.results() == [result]
    if failure_point == "cleanup":
        store.db.execute("DROP TRIGGER reject_cleanup")
    else:
        store.db.execute("DROP TABLE cleanup_guard")
    store.db.commit()
    store.close()

    async def scenario():
        monkeypatch.setattr(
            "maa_planner.daemon.coordinator.state_directory", lambda: tmp_path / "user/daemon"
        )
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n')
        daemon = Daemon(config, tmp_path / "control.json", root=root)
        loop = asyncio.create_task(daemon.run())

        def ready():
            if loop.done():
                loop.result()
            return daemon.configuration is not None and daemon.server.is_serving()

        try:
            await eventually(ready)
            assert daemon.store is not None
            assert daemon.store.unfinished() == []
            assert daemon.store.results() == [result]
            assert count(daemon.store, "attempts") == 0
            assert count(daemon.store, "prepared_executions") == 0
            assert count(daemon.store, "operations") == 0
            assert daemon.operations.get("op")["status"] == "succeeded"
        finally:
            daemon.request_stop()
            await loop

    asyncio.run(scenario())


def test_restart_cleans_logged_terminal_operation_after_delete_rollback(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    store.reserve_operation("op", {"action": "emulators", "target": None})
    store.db.execute(
        "CREATE TRIGGER reject_cleanup BEFORE DELETE ON operations BEGIN SELECT RAISE(ABORT,'cleanup rejected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.operation_update("op", "succeeded", result={"instances": 4})
    store.db.execute("DROP TRIGGER reject_cleanup")
    store.close()
    store = StateStore(path)
    store.recover_operations()
    assert count(store, "operations") == 0
    value = store.operation("op")
    assert (
        value is not None and value["status"] == "succeeded" and value["result"] == {"instances": 4}
    )
    store.close()


def test_restart_cleans_logged_schedule_after_delete_rollback(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    occurrence = ScheduledOccurrence("daily", "nemo", 100, 200)
    store.claim_schedule(occurrence, "revision")
    store.db.execute(
        "CREATE TRIGGER reject_cleanup BEFORE DELETE ON scheduled_runs BEGIN SELECT RAISE(ABORT,'cleanup rejected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.schedule_update(occurrence.id, "succeeded")
    store.db.execute("DROP TRIGGER reject_cleanup")
    store.close()
    store = StateStore(path)
    store.recover_schedules()
    assert count(store, "scheduled_runs") == 0
    value = store.scheduled_run(occurrence.id)
    assert value is not None and value["status"] == "succeeded"
    assert not store.claim_schedule(occurrence, "revision")
    store.close()


def test_exhausted_retry_cannot_reappear_after_terminal_delete_rollback(tmp_path):
    from dataclasses import asdict

    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    occurrence = ScheduledOccurrence("daily", "nemo", 100, 200)
    store.claim_schedule(occurrence, "revision")
    retry = {
        "id": occurrence.id,
        "profile": "nemo",
        "revision": "revision",
        "status": "waiting",
        "number": 3,
        "due_at": 150,
        "occurrence": asdict(occurrence),
    }
    store.save_scheduled_retry(retry)
    store.schedule_update(occurrence.id, "retry_waiting")
    store.db.execute(
        "CREATE TRIGGER reject_cleanup BEFORE DELETE ON scheduled_retries BEGIN SELECT RAISE(ABORT,'cleanup rejected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.save_scheduled_retry({**retry, "status": "exhausted"})
    store.db.execute("DROP TRIGGER reject_cleanup")
    store.close()
    store = StateStore(path)
    store.recover_schedules()
    assert not store.scheduled_retries()
    value = store.scheduled_run(occurrence.id)
    assert value is not None and value["status"] == "failed"
    store.close()

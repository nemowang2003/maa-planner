from dataclasses import replace

import pytest

from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.state import StateStore
from tests.helpers.execution import request


def test_state_survives_reopen_and_deduplicates_atomically(tmp_path):
    req = request(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3")
    assert state.enqueue(req)
    assert not state.enqueue(req)
    with pytest.raises(ValueError, match="请求不同"):
        state.enqueue(replace(req, config_revision="other"))
    state.start(req.attempt_id)
    state.phase(req.attempt_id, "admitted")
    state.close()

    state = StateStore(tmp_path / "state.sqlite3")
    assert state.unfinished() == [req.attempt_id]
    result = RunResult(req.attempt_id, "cancelled")
    state.finish(result)
    state.finish(result)
    with pytest.raises(ValueError, match="其他结果"):
        state.finish(replace(result, status="succeeded"))
    assert state.results() == [result]
    assert state.unfinished() == []
    state.close()


def test_event_shop_business_result_survives_state_reopen(tmp_path):
    from maa_planner.daemon.execution.event_shop import EventShopResult

    req = request(tmp_path)
    path = tmp_path / "shop-state.sqlite3"
    state = StateStore(path)
    state.enqueue(req)
    result = RunResult(
        req.attempt_id,
        "succeeded",
        event_shop_results=(
            EventShopResult(
                1, "insufficient_currency", "insufficient_currency", ("state/debug/asst.log:10",)
            ),
        ),
    )
    state.finish(result)
    state.close()
    state = StateStore(path)
    assert state.results() == [result]
    state.reserve_copilot("shop", {}, req.attempt_id)
    operation = state.copilot("shop")
    assert operation is not None
    assert operation["result"]["event_shop_results"][0]["outcome"] == "insufficient_currency"
    # Older structured log results remain readable without new optional fields.
    old = {"attempt_id": req.attempt_id, "status": "succeeded"}
    archived = state.history.get("attempt", req.attempt_id)
    assert archived is not None
    state.history.save("attempt", req.attempt_id, {**archived, "result": old})
    assert state.results()[0].event_shop_results == ()
    operation = state.copilot("shop")
    assert operation is not None
    assert operation["result"]["event_shop_results"] == []
    state.close()


def test_scheduled_occurrences_survive_reopen_without_replaying_interrupted_work(tmp_path):
    from maa_planner.daemon.planning.schedule import ScheduledOccurrence

    occurrence = ScheduledOccurrence("daily", "nemo", 100, 200)
    path = tmp_path / "state.sqlite3"
    state = StateStore(path)
    assert state.claim_schedule(occurrence, "revision-1")
    state.schedule_update(occurrence.id, "preparing")
    state.close()
    state = StateStore(path)
    state.recover_schedules()
    recovered = state.scheduled_run(occurrence.id)
    assert recovered is not None and recovered["status"] == "recovery_required"
    assert not state.claim_schedule(occurrence, "revision-2")
    assert state.claim_schedule(ScheduledOccurrence("daily", "nemo", 200, 300), "revision-2")
    state.close()


def test_alerts_are_account_scoped_deduplicated_and_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    observed = {"30012": {"count": 150, "warning_threshold": 200}}
    store.reconcile_alerts("nemo", "infrast_material", observed, now=1)
    identifier = store.alerts("nemo")[0]["alert_id"]
    store.close()
    store = StateStore(path)
    store.reconcile_alerts("nemo", "infrast_material", {"30012": {"count": 140}}, now=2)
    store.reconcile_alerts("other", "infrast_material", observed, now=2)
    assert len(store.events(0)) == 2
    assert store.alerts("nemo")[0]["alert_id"] == identifier
    assert store.alerts("nemo")[0]["details"]["count"] == 140
    store.reconcile_alerts("nemo", "infrast_material", {}, now=3)
    assert not store.alerts("nemo")
    assert store.alerts("other")
    store.reconcile_alerts("nemo", "infrast_material", {}, now=4)
    assert len(store.events(0)) == 3
    store.reconcile_alerts("nemo", "infrast_material", observed, now=5)
    assert store.alerts("nemo")[0]["alert_id"] != identifier
    assert [e["data"]["transition"] for e in store.events(0)] == [
        "raised",
        "raised",
        "resolved",
        "raised",
    ]
    store.close()


def test_alert_state_and_event_roll_back_together(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite3")

    def reject(*args, **kwargs):
        raise OSError("event rejected")

    monkeypatch.setattr(store.history, "event", reject)
    with pytest.raises(OSError, match="event rejected"):
        store.reconcile_alerts("nemo", "infrast_material", {"30012": {"count": 150}}, now=1)
    assert not store.alerts(active_only=False)
    assert not store.events(0)
    store.close()

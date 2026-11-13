import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.execution.tasks import CoreTasks
from maa_planner.daemon.planning.activity_shop import prepare_shop_context
from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.scheduling import DailyScheduler
from maa_planner.daemon.state import StateStore
from maa_planner.schema import StageConfirmation
from tests.helpers.activity_shop import NOW, activity, configured, shop_catalog, shop_plan
from tests.helpers.execution import request


def confirmed_store(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    for profile in ("a", "b"):
        for stage, raid in shop_plan()["required"]:
            store.confirm_stage(StageConfirmation(profile, "event", stage, raid), now=NOW)
    return store


def report(plan, navigation="failed", **changes):
    return {
        "activity_id": plan["activity_id"],
        "fingerprint": plan["fingerprint"],
        "after_task": 0,
        "outcome": "unknown",
        "reason": "navigation_failed",
        "navigation_outcome": navigation,
        "navigation_phase": "Open",
        "task_id": 4,
        "observed_at": NOW,
        **changes,
    }


def test_first_failure_is_global_persistent_and_independent_of_scope_or_entry_changes(tmp_path):
    store = confirmed_store(tmp_path)
    plan = shop_plan()
    store.record_shop("a", plan, report(plan), attempt_id="first")
    assert not store.alerts("a")
    alert = store.alerts("admin")[0]
    assert alert["details"]["all_profiles_disabled"]
    store.close()
    store = StateStore(tmp_path / "state.sqlite3")

    async def catalog():
        return shop_catalog()

    for profile in ("a", "b"):
        schedule = replace(
            configured(tmp_path),
            shop_blacklist=(),
        )
        plans = asyncio.run(
            prepare_shop_context(
                schedule,
                activity(),
                now=NOW,
                store=store,
                profile=profile,
                load_catalog=catalog,
                depot={},
            )
        )
        assert plans[0]["fingerprint"] != plan["fingerprint"]
        assert plans[0]["navigation_status"] == "disabled"
        daily = build_daily_plan(schedule, now=NOW, activity=activity(), depot={}, shops=plans)
        assert daily.tasks is not None
        assert "activity_shops" not in json.loads(daily.tasks.document)
        assert json.loads(daily.tasks.document)["tasks"][-1]["type"] == "Award"
    store.record_shop("a", plan, report(plan), attempt_id="duplicate")
    assert store.alerts("admin")[0]["alert_id"] == alert["alert_id"]
    assert len([e for e in store.events(0) if e["kind"] == "alert"]) == 1
    assert not store.shop_navigation_state({**plan, "activity_id": "other"})
    assert not store.shop_navigation_state({**plan, "available_from": 5000})
    store.close()


@pytest.mark.parametrize(
    "navigation,reason",
    [
        ("not_attempted", "fight_failed"),
        ("not_attempted", "entry_missing"),
        ("succeeded", "missing_shop_evidence"),
        ("succeeded", "insufficient_currency"),
    ],
)
def test_only_first_actual_navigation_failure_disables_activity(tmp_path, navigation, reason):
    store = confirmed_store(tmp_path)
    plan = shop_plan()
    store.record_shop("a", plan, report(plan, navigation, reason=reason), attempt_id="one")
    state = store.shop_navigation_state(plan)
    assert state.get("status") == ("succeeded" if navigation == "succeeded" else None)
    assert not store.alerts("admin")
    if navigation == "succeeded":
        store.record_shop("b", plan, report(plan), attempt_id="later")
        assert store.shop_navigation_state(plan)["status"] == "succeeded"
        assert not store.alerts("admin")
    store.close()


def test_failure_expires_at_combat_end_and_resolves_admin_alert(tmp_path):
    store = confirmed_store(tmp_path)
    plan = shop_plan()
    store.record_shop("a", plan, report(plan), attempt_id="first")
    assert store.expire_activity_state(now=2999) == 3000
    assert store.shop_navigation_state(plan)["status"] == "disabled"
    store.expire_activity_state(now=3000)
    assert not store.shop_navigation_state(plan)
    assert not store.alerts("admin")
    assert store.alerts("admin", active_only=False)
    assert any(e["kind"] == "activity_shop_result" for e in store.events(0))
    store.close()


def test_navigation_failure_does_not_require_purchase_completion_proof(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    plan = shop_plan()
    store.record_shop("a", plan, report(plan), attempt_id="first")
    assert store.shop_navigation_state(plan)["status"] == "disabled"
    assert not store.shop_state("a", "event", plan["fingerprint"])["completed"]
    store.close()


def test_shared_probe_prevents_parallel_first_attempts_and_rechecks_stale_plans(tmp_path):
    store = confirmed_store(tmp_path)
    scheduler = DailyScheduler(cast(Daemon, SimpleNamespace(store=store)))
    plan = shop_plan()
    requests = []
    for profile in ("a", "b"):
        value = request(tmp_path, profile)
        tasks = CoreTasks(json.dumps({"tasks": [{"type": "Fight"}], "activity_shops": [plan]}))
        requests.append(replace(value, core=replace(value.core, tasks=tasks)))
    assert scheduler.shop_permission(requests[0], report(plan)) == "untried"
    assert scheduler.shop_permission(requests[1], report(plan)) == "pending"
    scheduler.shop_report(requests[0], report(plan))
    assert scheduler.shop_permission(requests[1], report(plan)) == "disabled"
    store.close()


def test_cancelled_probe_does_not_disable_activity(tmp_path):
    store = confirmed_store(tmp_path)
    scheduler = DailyScheduler(cast(Daemon, SimpleNamespace(store=store)))
    plan = shop_plan()
    value = request(tmp_path)
    value = replace(
        value,
        core=replace(
            value.core,
            tasks=CoreTasks(
                json.dumps(
                    {
                        "tasks": [{"type": "Fight"}],
                        "activity_shops": [plan],
                    }
                )
            ),
        ),
    )
    assert scheduler.shop_permission(value, report(plan)) == "untried"
    scheduler.finished(RunResult(value.attempt_id, "cancelled"))
    assert not scheduler.shop_probes
    assert not store.shop_navigation_state(plan)
    store.close()


def test_first_failure_and_admin_alert_commit_together(tmp_path):
    import sqlite3

    store = confirmed_store(tmp_path)
    plan = shop_plan()
    store.db.execute(
        "CREATE TRIGGER reject_navigation_alert BEFORE INSERT ON alerts "
        "WHEN NEW.category='activity_shop_navigation' BEGIN SELECT RAISE(ABORT,'rejected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="rejected"):
        store.record_shop("a", plan, report(plan), attempt_id="first")
    assert not store.shop_navigation_state(plan)
    assert not store.shop_state("a", "event", plan["fingerprint"])
    store.close()

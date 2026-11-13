import json
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.core import run
from tests.helpers.activity_shop import NOW, shop_plan
from tests.helpers.core import spec
from tests.helpers.shop_core import ShopCore


def configured(tmp_path):
    plan = shop_plan()
    plan["completed"] = plan["required"][:]
    return {
        **spec(tmp_path),
        "tasks": [{"type": "Fight", "params": {"stage": "1-7", "times": 30}}, {"type": "Award"}],
        "activity_shops": [plan],
    }


@pytest.mark.parametrize("outcome", ["completed", "insufficient_currency", "unknown"])
def test_shop_runs_after_fight_and_streams_business_evidence_before_remaining_daily_tasks(
    tmp_path, monkeypatch, outcome
):
    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)
    core = ShopCore(outcome)
    reports = []

    def report(value):
        assert core.appended[-1][1]["task_names"] == ["SS@Store@Begin"]
        reports.append(value)

    assert run(
        configured(tmp_path),
        library_factory=lambda _: SimpleNamespace(lib=core),
        on_shop=report,
    ) == (1 if outcome == "unknown" else 0)
    assert core.appended[0] == ("Fight", {"stage": "1-7", "series": 0})
    assert core.appended[-1][0] == "Award"
    assert core.appended[-2][1]["event_shop"] == {"blacklist": ["家具"]}
    assert reports[0]["outcome"] == outcome
    assert reports[0]["navigation_outcome"] == "succeeded"
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["activity_shop_results"] == reports


@pytest.mark.parametrize(
    "reason",
    [
        "stages_incomplete",
        "catalog_incomplete",
        "regions_not_open",
        "entry_missing",
        "navigation_failed",
        "navigation_unverified",
    ],
)
def test_unready_or_failed_navigation_never_purchases_and_continues_daily(
    tmp_path, monkeypatch, reason
):
    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)
    document = configured(tmp_path)
    plan = document["activity_shops"][0]
    if reason == "stages_incomplete":
        plan["completed"].pop()
    if reason == "catalog_incomplete":
        plan["catalog_complete"] = False
    if reason == "regions_not_open":
        plan["all_regions_from"] = NOW + 1
    if reason == "entry_missing":
        plan["entry"] = None
    core = ShopCore(
        navigation=reason != "navigation_failed", verified=reason != "navigation_unverified"
    )
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == (
        1 if reason in {"entry_missing", "navigation_failed", "navigation_unverified"} else 0
    )
    assert not any(params.get("task_names") == ["SS@Store@Begin"] for _, params in core.appended)
    assert core.appended[-1][0] == "Award"
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_shop_results"][0]
    assert report["reason"] == reason
    assert report["navigation_outcome"] == (
        "failed" if reason in {"navigation_failed", "navigation_unverified"} else "not_attempted"
    )
    if reason in {"navigation_failed", "navigation_unverified"}:
        assert report["navigation_phase"] == "Open"
        assert report["task_id"] is not None


@pytest.mark.parametrize("completed", [True, False])
def test_same_run_copilot_evidence_can_unlock_shop_but_failed_phase_cannot(
    tmp_path, monkeypatch, completed
):
    from maa_planner.daemon.execution.activity_copilot import ClearanceReport, ClearanceResult
    from tests.helpers.clearance import clearance

    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)
    document = configured(tmp_path)
    document["activity_shops"][0]["completed"] = []
    copilot, _, _ = clearance()
    document["activity_copilot"] = {**copilot, "before_task": 0}

    def clear(targets, known, driver, *, on_result):
        entries = []
        for target in targets:
            entry = ClearanceResult(
                target, "completed" if completed else "failed", 0 if completed else 3, ("proof",)
            )
            entries.append(entry)
            on_result(entry)
            if not completed:
                break
        return ClearanceReport("completed" if completed else "stopped", tuple(entries))

    monkeypatch.setattr("maa_planner.daemon.execution.activity_copilot.run_clearance", clear)
    core = ShopCore()
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert (
        any(params.get("task_names") == ["SS@Store@Begin"] for _, params in core.appended)
        == completed
    )


def test_first_navigation_failure_suppresses_later_shop_phases_in_same_run(tmp_path, monkeypatch):
    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)
    document = configured(tmp_path)
    document["tasks"].insert(1, {"type": "Fight", "params": {"stage": "1-7"}})
    plan = document["activity_shops"][0]
    document["activity_shops"].append({**plan, "after_task": 1})
    core = ShopCore(navigation=False)
    reports = []
    assert (
        run(document, library_factory=lambda _: SimpleNamespace(lib=core), on_shop=reports.append)
        == 1
    )
    assert [r["reason"] for r in reports] == ["navigation_failed", "navigation_disabled"]
    assert (
        sum(
            params.get("task_names") == [plan["navigation"] + "@Open"]
            for _, params in core.appended
        )
        == 1
    )
    assert core.appended[-1][0] == "Award"


@pytest.mark.parametrize("permission", ["disabled", "pending"])
def test_daemon_permission_skips_navigation_even_with_old_plan(tmp_path, monkeypatch, permission):
    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)
    core = ShopCore()
    reports = []
    assert (
        run(
            configured(tmp_path),
            library_factory=lambda _: SimpleNamespace(lib=core),
            on_shop=reports.append,
            shop_permission=lambda _: permission,
        )
        == 0
    )
    assert [kind for kind, _ in core.appended] == ["Fight", "Award"]
    assert reports[0]["reason"] == "navigation_" + permission
    assert reports[0]["navigation_outcome"] == "not_attempted"


def test_successful_first_navigation_is_not_reclassified_by_later_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)
    document = configured(tmp_path)
    document["tasks"][1:1] = [{"type": "Fight", "params": {"stage": "1-7"}} for _ in range(2)]
    plan = document["activity_shops"][0]
    document["activity_shops"] = [{**plan, "after_task": index} for index in range(3)]
    core = ShopCore(outcome="insufficient_currency")
    reports = []

    def receive(report):
        reports.append(report)
        core.navigation = len(reports) != 1

    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core), on_shop=receive) == 1
    assert [report["reason"] for report in reports] == [
        "insufficient_currency",
        "navigation_failed",
        "insufficient_currency",
    ]


def test_navigation_cancel_is_not_a_template_failure(monkeypatch):
    import threading

    from maa_planner.daemon.execution.activity_shop import run_shop

    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)
    stop = threading.Event()
    results = SimpleNamespace(tasks={1: {"status": "failed"}})

    def execute(*args, **kwargs):
        stop.set()
        return [1]

    plan = shop_plan()
    with pytest.raises(InterruptedError, match="取消"):
        run_shop(plan, {tuple(v) for v in plan["required"]}, execute, results, stop)

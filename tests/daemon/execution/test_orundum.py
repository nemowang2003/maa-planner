import json
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.core import run
from maa_planner.daemon.execution.orundum import ProductionEvidence, production_targets
from tests.helpers.core import FakeCore, spec
from tests.helpers.orundum import callback, native_plan, plan


@pytest.mark.parametrize(
    "case", ["old", "changed", "failure", "missing", "cross_task", "duplicate"]
)
def test_room_production_requires_complete_current_task_evidence(case):
    frozen = plan()
    evidence = ProductionEvidence(7, frozen)
    reports = []
    for i, target in enumerate(frozen["targets"]):
        if case == "missing" and i == 0:
            continue
        payload = callback(target, task_id=8 if case == "cross_task" and i == 0 else 7)
        if case in {"old", "changed", "failure"} and i == 0:
            payload["details"]["product"] = "PureGold"
        value = evidence.consume(20003, payload, f"callbacks:{i}")
        if value:
            reports.append(value)
    if case in {"changed", "failure"}:
        what = "ProductChanged" if case == "changed" else "ProductChangeFail"
        value = evidence.consume(
            20003,
            callback(frozen["targets"][0], what=what, product="OriginiumShard"),
            "callbacks:changed",
        )
        if value:
            reports.append(value)
    if case == "duplicate":
        assert evidence.consume(20003, callback(frozen["targets"][0]), "again") is None
    assert len(reports) == (1 if case in {"changed", "duplicate"} else 0)
    if reports:
        assert reports[0]["mode"] == "orundum" and len(reports[0]["evidence"]) == 6


def test_product_change_failure_clears_room_certification_before_last_room():
    frozen = plan()
    evidence = ProductionEvidence(7, frozen)
    for target in frozen["targets"][:-1]:
        assert evidence.consume(20003, callback(target), "found") is None
    assert (
        evidence.consume(20003, callback(frozen["targets"][0], what="ProductChangeFail"), "fail")
        is None
    )
    assert evidence.consume(20003, callback(frozen["targets"][-1]), "last") is None
    report = evidence.consume(
        20003,
        callback(frozen["targets"][0], what="ProductChanged", product="OriginiumShard"),
        "changed",
    )
    assert report is not None


@pytest.mark.parametrize("invalid", ["layout", "missing", "wrong_plan", "skip", "callback_alias"])
def test_target_layout_comes_from_selected_native_plan(invalid):
    data = native_plan()
    if invalid == "layout":
        data["plans"][0]["rooms"]["manufacture"][0]["product"] = "Pure Gold"
    elif invalid == "missing":
        data["plans"][0]["rooms"]["trading"].pop()
    elif invalid == "skip":
        data["plans"][0]["rooms"]["manufacture"][0]["skip"] = True
    elif invalid == "callback_alias":
        data["plans"][0]["rooms"]["manufacture"][0]["product"] = "OriginStone"
    else:
        data["plans"].insert(0, {"rooms": {}})
        assert len(production_targets(data, 1)) == 6
    with pytest.raises(ValueError):
        production_targets(data, 0)


@pytest.mark.parametrize(
    "case", ["success", "fail_change", "missing", "later_failure", "denied", "processing"]
)
def test_core_persists_start_ack_before_infrast_and_reports_only_production(
    case, tmp_path, monkeypatch
):
    starts, reports = [], []
    frozen = plan()

    class ProductionCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            if kind == "Infrast":
                assert starts == [{"intent_id": "intent", "business_id": "task:0"}]
                for i, target in enumerate(frozen["targets"]):
                    if case == "missing" and i == 0:
                        continue
                    self.emit(
                        20003,
                        callback(
                            target, task_id=identifier, product="PureGold" if i == 0 else None
                        ),
                    )
                    if i == 0:
                        self.emit(
                            20003,
                            callback(
                                target,
                                task_id=identifier,
                                what="ProductChangeFail"
                                if case == "fail_change"
                                else "ProductChanged",
                                product="OriginiumShard",
                            ),
                        )
                return 10000 if case == "later_failure" else 10002
            return super().task_outcome(identifier, kind, params)

    core = ProductionCore()
    document = spec(tmp_path)
    document.update(
        tasks=[{"type": "Infrast", "params": {}}], business_ids=["task:0"], infrast_orundum=frozen
    )
    if case == "processing":
        document["infrast_processing"] = [{"before_task": 0}]
        monkeypatch.setattr(
            "maa_planner.daemon.execution.processing.execute_processing",
            lambda *args: {"outcome": "error", "reason": "processing_failed"},
        )

    def start(query):
        assert not core.appended
        starts.append(query)
        return case != "denied"

    code = run(
        document,
        library_factory=lambda _: SimpleNamespace(lib=core),
        orundum_start=start,
        on_orundum=reports.append,
    )
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["orundum_results"] == reports
    if case in {"denied", "processing"}:
        assert not core.appended and not reports
    if case == "processing":
        assert not starts
    assert bool(reports) == (case in {"success", "later_failure"})
    assert (code == 0) == (case == "success")
    if case in {"fail_change", "missing"}:
        assert result["business_results"][0]["status"] == "error"
        assert result["business_results"][0]["error"]["kind"] == "orundum_production"
    if case == "later_failure":
        assert result["business_results"][0]["status"] == "error"

import threading
from types import SimpleNamespace

from maa_planner.daemon.execution.core import CallbackResults, run
from maa_planner.daemon.execution.processing import CoreProcessingDriver, run_processing
from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.planning.schedule import parse_schedules
from tests.helpers.core import FakeCore, spec
from tests.helpers.planning import NOW


def phase():
    return {
        "stock": {},
        "operators": [
            {"name": "年", "allowed_items": ["o", "p"]},
            {"name": "芳汀", "allowed_items": ["o", "p"]},
        ],
        "recipes": [
            {"item_id": "o", "target": 1, "costs": {"p": 2}},
            {"item_id": "p", "target": 2, "costs": {"base": 1}},
        ],
    }


class Driver:
    def __init__(self, stock, outcomes=()):
        self.stock, self.outcomes = stock, iter(outcomes)
        self.selected, self.crafts = [], []

    def cancelled(self):
        return False

    def read_stock(self):
        return self.stock

    def select_operator(self, operator):
        self.selected.append(operator["name"])
        return "ready"

    def craft(self, recipe):
        self.crafts.append(recipe["item_id"])
        return next(self.outcomes, "completed")


def test_each_operator_uses_live_stock_and_switches_only_after_mood_exhaustion():
    driver = Driver({"o": 0, "p": 2, "base": 2}, ["low_mood"])
    result = run_processing(phase(), driver)
    assert driver.selected == ["年", "芳汀"]
    assert driver.crafts == ["o", "o", "p", "p"]
    assert result["stock"] == {"o": 1, "p": 2, "base": 0}


def test_locked_recipe_does_not_block_other_materials_and_unknown_craft_stops():
    driver = Driver({"o": 0, "p": 2, "base": 2}, ["locked"])
    result = run_processing(phase(), driver)
    assert driver.crafts == ["o"]
    assert result["outcome"] == "completed"
    uncertain = Driver({"o": 0, "p": 2}, ["unknown"])
    assert run_processing(phase(), uncertain)["outcome"] == "error"
    assert uncertain.crafts == ["o"]


def test_unknown_inventory_never_selects_an_operator_or_consumes_materials():
    driver = Driver(None)
    assert run_processing(phase(), driver)["reason"] == "inventory_unknown"
    assert driver.selected == []
    assert driver.crafts == []


def test_processing_cancellation_does_not_start_an_operator(monkeypatch):
    import pytest

    driver = Driver({"o": 0, "p": 2})
    monkeypatch.setattr(driver, "cancelled", lambda: True)
    with pytest.raises(InterruptedError):
        run_processing(phase(), driver)
    assert driver.selected == []


def test_core_operator_selection_requires_confirmation_and_rejects_kept_room():
    results = CallbackResults()
    results.tasks[1] = {"status": "completed"}
    results.extras[1] = [("CustomInfrastRoomKeptUnchanged", {}), ("InfrastConfirmButton", {})]
    driver = CoreProcessingDriver(
        {"navigation": "Planner@Processing0"}, lambda *a, **k: [1], results, threading.Event()
    )
    assert driver.select_operator({"name": "年", "filename": "private.json"}) == "unavailable"
    results.extras[1] = []
    assert driver.select_operator({"name": "年", "filename": "private.json"}) == "error"


def test_processing_failure_still_runs_normal_shift_and_keeps_same_business_pending(
    tmp_path, monkeypatch
):
    called = []

    def execute(phase, execute_tasks, results, stop, directory):
        execute_tasks(
            [{"type": "Custom", "params": {"task_names": ["Planner@Probe"]}}], handled=True
        )
        called.append(phase["before_task"])
        return {"outcome": "error", "reason": "craft_unverified", "results": []}

    monkeypatch.setattr("maa_planner.daemon.execution.processing.execute_processing", execute)
    data = spec(tmp_path)
    data["tasks"] = [
        {"type": "StartUp"},
        {"type": "Infrast", "params": {"facility": ["Mfg"]}},
        {"type": "Award"},
    ]
    data["business_ids"] = ["lifecycle:startup", "task:0", "task:1"]
    data["infrast_processing"] = [{"before_task": 1}]
    core = FakeCore()
    assert run(data, library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    assert [k for k, _ in core.appended] == ["StartUp", "Custom", "Infrast", "Award"]
    import json

    result = json.loads((tmp_path / "core-result.json").read_text())
    assert {b["id"]: b["status"] for b in result["business_results"]} == {
        "lifecycle:startup": "completed",
        "task:0": "error",
        "task:1": "completed",
    }
    assert called == [1]


def test_only_enabled_pending_infrast_gets_processing_and_deer_never_gets_attachment(tmp_path):
    row = {
        "id": "daily",
        "profiles": ["nemo"],
        "times": ["12:00"],
        "session": {"startup": {}, "closedown": {}},
        "tasks": [
            {
                "type": "Infrast",
                "processing": {"operators": ["年", "九色鹿", "芳汀"], "targets": {"T3": 20}},
            },
            {
                "type": "Infrast",
                "enabled": False,
                "processing": {"operators": ["年"], "targets": {"T3": 20}},
            },
            {"type": "Award"},
        ],
    }
    schedule = parse_schedules([row], {"nemo"}, tmp_path / "config.toml")[0]
    context = {
        "catalog_commit": "a" * 40,
        "stock": {},
        "catalog": {
            "item_table": {
                "items": {"p": {"name": "白马醇", "rarity": "TIER_4", "itemType": "MATERIAL"}}
            },
            "building_data": {
                "workshopFormulas": {
                    "p": {
                        "formulaId": "p",
                        "itemId": "p",
                        "formulaType": "F_EVOLVE",
                        "count": 1,
                        "goldCost": 300,
                        "costs": [{"id": "base", "count": 2}],
                    }
                }
            },
        },
    }
    daily = build_daily_plan(schedule, now=NOW, processing=context)
    (tmp_path / "prepared").mkdir()
    assert daily.tasks is not None
    frozen = daily.tasks.materialize(tmp_path / "prepared")
    phases = frozen["infrast_processing"]
    assert len(phases) == 1 and phases[0]["before_task"] == 1
    assert frozen["tasks"][1]["type"] == "Infrast"
    assert [o["name"] for o in phases[0]["operators"]] == ["年", "芳汀"]
    import json

    for o in phases[0]["operators"]:
        from pathlib import Path

        contents = json.loads(Path(o["filename"]).read_text())
        assert contents["plans"][0]["rooms"]["processing"][0]["operators"] == [o["name"]]
    pending = build_daily_plan(schedule, now=NOW, pending=frozenset({"task:2"}))
    assert pending.tasks is not None
    assert "infrast_processing" not in json.loads(pending.tasks.document)


def test_operator_with_no_allowed_deficit_is_skipped_before_selection_but_next_runs():
    data = phase()
    data["operators"][0]["allowed_items"] = ["o"]
    data["operators"][1]["allowed_items"] = ["p"]
    driver = Driver({"o": 1, "p": 0, "base": 2})
    result = run_processing(data, driver)
    assert driver.selected == ["芳汀"]
    assert driver.crafts == ["p", "p"]
    assert result["outcome"] == "completed"


def test_live_driver_never_submits_start_without_material_and_single_quantity_evidence():
    recipe = {"item_id": "a"}
    for failure in ("material", "quantity", "mood", None):
        results = CallbackResults()
        submitted = []

        def execute(tasks, **kwargs):
            name = tasks[0]["params"]["task_names"][0]
            submitted.append(name.rsplit("@", 1)[-1])
            identifier = len(submitted)
            results.tasks[identifier] = {"status": "completed"}
            if failure == "material" and name.endswith("@Material0"):
                results.tasks[identifier]["status"] = "error"
            emitted = (
                ["LowMood" if failure == "mood" else "Workshop"]
                if name.endswith("@Probe")
                else ["Start", "Obtain", "Workshop"]
                if name.endswith("@Start")
                else [name.rsplit("@", 1)[-1]]
            )
            for node in emitted:
                results.consume(
                    20002,
                    {
                        "taskid": identifier,
                        "subtask": "ProcessTask",
                        "details": {
                            "task": "Planner@Processing0@" + node,
                            "result": {"text": "unknown" if failure == "quantity" else "1"},
                        },
                    },
                    "callback",
                )
            return [identifier]

        driver = CoreProcessingDriver(
            {"navigation": "Planner@Processing0", "recipes": [recipe]},
            execute,
            results,
            threading.Event(),
        )
        outcome = driver.craft(recipe)
        assert ("Start" in submitted) == (failure is None)
        assert outcome == (
            "completed" if failure is None else "low_mood" if failure == "mood" else "unknown"
        )

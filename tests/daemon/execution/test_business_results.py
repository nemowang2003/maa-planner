import json
from types import SimpleNamespace

from maa_planner.daemon.execution.core import run
from tests.helpers.activity_shop import shop_plan
from tests.helpers.core import FakeCore, spec


def test_shop_core_error_fails_farming_business_after_successful_fight(tmp_path, monkeypatch):
    def failed_shop(*args):
        return {
            "activity_id": "event",
            "fingerprint": "shop",
            "outcome": "unknown",
            "reason": "purchase_failed",
            "core_error": {"status": "error", "task_id": 8, "evidence": "callbacks.jsonl:22"},
        }

    monkeypatch.setattr("maa_planner.daemon.execution.activity_shop.run_shop", failed_shop)
    core = FakeCore()
    doc = spec(tmp_path)
    doc["tasks"].append({"type": "Award"})
    doc["activity_shops"] = [{**shop_plan(), "after_task": 1}]
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["business_results"] == [
        {"id": "task:0", "status": "completed"},
        {
            "id": "task:1",
            "status": "error",
            "error": {"status": "error", "task_id": 8, "evidence": "callbacks.jsonl:22"},
        },
        {"id": "task:2", "status": "completed"},
    ]
    assert result["task_checkpoints"][1]["status"] == "completed"

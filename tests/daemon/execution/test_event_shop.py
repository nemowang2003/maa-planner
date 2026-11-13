import json
from dataclasses import asdict
from pathlib import Path

from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.execution.core import CallbackResults

RECORDED = Path(__file__).with_name("fixtures") / "event-shop-insufficient.jsonl"


def test_recorded_callbacks_prove_insufficient_currency_without_reading_core_text_logs():
    tracker = CallbackResults()
    for line, content in enumerate(RECORDED.read_text().splitlines(), 1):
        event = json.loads(content)
        tracker.consume(event["message"], event["payload"], f"callbacks.jsonl:{line}")
    (result,) = tracker.shop_results()
    assert result["outcome"] == "insufficient_currency"
    assert result["evidence"]
    value = RunResult.from_dict(
        {"attempt_id": "one", "status": "succeeded", "event_shop_results": [result]}
    )
    assert RunResult.from_dict(json.loads(json.dumps(asdict(value)))) == value
    assert (
        RunResult.from_dict({"attempt_id": "old", "status": "succeeded"}).event_shop_results == ()
    )


def test_unlimited_requires_started_shop_and_completed_chain():
    tracker = CallbackResults()
    common = {"taskid": 1, "taskchain": "Custom"}
    tracker.consume(20001, {**common, "subtask": "EventShopTask"}, "callbacks.jsonl:1")
    tracker.consume(
        20002,
        {**common, "first": ["SS@Store@PurchaseSelected"], "details": {"task": "CheckUnlimited"}},
        "callbacks.jsonl:2",
    )
    assert tracker.shop_results()[0]["outcome"] == "unknown"
    tracker.consume(10002, common, "callbacks.jsonl:3")
    assert tracker.shop_results()[0]["outcome"] == "completed"
    tracker.consume(10004, common, "callbacks.jsonl:4")
    assert tracker.shop_results()[0]["outcome"] == "unknown"

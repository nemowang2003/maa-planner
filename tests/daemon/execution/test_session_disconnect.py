import json
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.core import run
from tests.helpers.core import FakeCore, spec


@pytest.mark.parametrize("ending", [10002, 10004, None])
@pytest.mark.parametrize("node", ["OfflineConfirm", "OfflineConfirmAfterBattle"])
def test_disconnect_stops_remaining_tasks_and_cannot_be_overwritten_by_chain_completion(
    tmp_path, node, ending
):
    class DisconnectCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            if identifier == 2:
                self.emit(
                    20001,
                    {
                        "taskid": identifier,
                        "taskchain": kind,
                        "subtask": "ProcessTask",
                        "details": {"task": node, "action": "Stop"},
                    },
                )
                self.disconnect_running = ending is None
                return ending
            return 10002

        def AsstRunning(self, handle):
            return getattr(self, "disconnect_running", False)

        def AsstStop(self, handle):
            self.disconnect_running = False
            return super().AsstStop(handle)

    core = DisconnectCore()
    doc = spec(tmp_path)
    doc["tasks"] = [{"type": t} for t in ("StartUp", "Mall", "Recruit", "Award", "CloseDown")]
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    report = json.loads((tmp_path / "core-result.json").read_text())
    assert [kind for kind, _ in core.appended] == ["StartUp", "Mall"]
    assert report["status"] == "failed"
    assert report["session_failure"]["kind"] == "game_disconnected"
    assert report["session_failure"]["task_id"] == 2
    assert report["session_failure"]["node"] == node
    assert report["session_failure"]["evidence"].startswith("callbacks.jsonl:")
    assert [p["status"] for p in report["task_checkpoints"]] == ["completed", "error"]
    assert [b["status"] for b in report["business_results"]] == [
        "completed",
        "error",
        "pending",
        "pending",
        "pending",
    ]


@pytest.mark.parametrize(
    "message,chain,subtask,node,registered,expected",
    [
        (20001, "Mall", "ProcessTask", "Mall@OfflineConfirm", True, True),
        (20001, "StartUp", "ProcessTask", "OfflineConfirm", True, False),
        (20002, "Mall", "ProcessTask", "OfflineConfirm", True, False),
        (20001, "Mall", "OtherTask", "OfflineConfirm", True, False),
        (20001, "Mall", "ProcessTask", "OfflineConfirmImpl", True, False),
        (20001, "Mall", "ProcessTask", "OfflineConfirm", False, False),
    ],
)
def test_only_owned_non_startup_process_start_is_a_disconnect(
    message, chain, subtask, node, registered, expected
):
    from maa_planner.daemon.execution.core import CallbackResults

    tracker = CallbackResults()
    if registered:
        tracker.tasks[7] = {"task_id": 7, "type": chain, "status": "running", "subtask_errors": 0}
    tracker.consume(
        message,
        {"taskid": 7, "taskchain": chain, "subtask": subtask, "details": {"task": node}},
        "callbacks.jsonl:12",
        123,
    )
    assert (tracker.session_failure is not None) == expected
    if expected:
        assert tracker.session_failure is not None
        assert tracker.session_failure["observed_at"] == 123


def test_startup_can_reconnect_and_continue_daily_tasks(tmp_path):
    class ReconnectingCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            if kind == "StartUp":
                self.emit(
                    20001,
                    {
                        "taskid": identifier,
                        "taskchain": kind,
                        "subtask": "ProcessTask",
                        "details": {"task": "OfflineConfirm"},
                    },
                )
            return 10002

    core = ReconnectingCore()
    doc = spec(tmp_path)
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["session_failure"] is None
    assert [kind for kind, _ in core.appended] == ["StartUp", "Fight"]

import json
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.core import run
from maa_planner.daemon.planning.game_update import READY_NODE, readiness_tasks
from tests.helpers.core import FakeCore, spec


@pytest.mark.parametrize("ready", [False, True])
def test_only_yellow_start_callback_allows_success_and_close_down(tmp_path, ready):
    class Core(FakeCore):
        def task_outcome(self, identifier, kind, params):
            assert "game_resource_ready" not in params
            if kind == "Custom" and ready:
                self.emit(
                    20002,
                    {
                        "taskid": identifier,
                        "taskchain": kind,
                        "subtask": "ProcessTask",
                        "details": {"task": READY_NODE},
                    },
                )
            return 10002

    plan = readiness_tasks(30)
    doc = json.loads(plan.document)
    probe = doc["resource_tasks"][READY_NODE]
    assert probe["baseTask"] == "GameStart" and probe["template"] == "GameStart.png"
    assert probe["action"] == "DoNothing" and probe["next"] == []
    assert not any(t["type"] == "StartUp" for t in doc["tasks"])
    data = spec(tmp_path) | {k: v for k, v in doc.items() if k != "resource_tasks"}
    core = Core()
    code = run(data, library_factory=lambda _: SimpleNamespace(lib=core))
    assert code == (0 if ready else 1)
    assert [k for k, _ in core.appended] == (["Custom", "CloseDown"] if ready else ["Custom"])
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["business_results"][0]["status"] == ("completed" if ready else "error")
    if ready:
        assert result["business_results"][0]["evidence"]


def test_resource_wait_cancellation_never_closes_game(tmp_path):
    import threading

    stop = threading.Event()
    core = FakeCore(cancel=stop)
    doc = json.loads(readiness_tasks(60).document)
    data = spec(tmp_path) | {k: v for k, v in doc.items() if k != "resource_tasks"}
    assert run(data, library_factory=lambda _: SimpleNamespace(lib=core), stop=stop) == 1
    assert [k for k, _ in core.appended] == ["Custom"]
    assert json.loads((tmp_path / "core-result.json").read_text())["status"] == "cancelled"


def test_fresh_login_uses_action_start_time_not_delayed_completion():
    from maa_planner.daemon.execution.core import CallbackResults

    results = CallbackResults()
    results.tasks[1] = {"type": "StartUp"}
    common = {"taskid": 1, "taskchain": "StartUp", "subtask": "ProcessTask"}
    results.consume(
        20001, common | {"details": {"task": "StartUp@GameStart"}}, "start", observed_at=10
    )
    results.consume(
        20002, common | {"details": {"task": "StartUp@GameStart"}}, "clicked", observed_at=20
    )
    results.consume(20002, common | {"details": {"task": "StartUp"}}, "home", observed_at=30)
    assert results.logins[1] == {"started_at": 10, "observed_at": 30, "evidence": ["start", "home"]}


@pytest.mark.parametrize(
    "nodes,confirmed",
    [
        (["StartAtHome"], False),
        (["GameStart"], False),
        (["StartUp"], False),
        (["GameStart", "StartUp"], True),
    ],
)
def test_login_receipt_requires_new_login_then_home_and_streams_before_later_tasks(
    tmp_path, nodes, confirmed
):
    reports = []

    class Core(FakeCore):
        def task_outcome(self, identifier, kind, params):
            if kind == "Award":
                assert bool(reports) == confirmed
            if kind == "StartUp":
                for node in nodes:
                    event = {
                        "taskid": identifier,
                        "taskchain": "StartUp",
                        "subtask": "ProcessTask",
                        "details": {"task": node},
                    }
                    self.emit(20001, event)
                    self.emit(20002, event)
            return 10002

    data = spec(tmp_path) | {"tasks": [{"type": "StartUp"}, {"type": "Award"}]}
    core = Core()
    assert (
        run(data, library_factory=lambda _: SimpleNamespace(lib=core), on_login=reports.append) == 0
    )
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert ("login_success" in result["business_results"][0]) == confirmed

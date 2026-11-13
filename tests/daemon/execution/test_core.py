import json
import threading
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.core import run
from tests.helpers.core import FakeCore, spec


def test_same_core_tasks_and_raw_callbacks_are_preserved(tmp_path):
    core = FakeCore()
    code = run(spec(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core))
    assert code == 0
    assert core.destroyed
    assert core.appended[1][1]["stage"] == "1-7"
    assert core.extras == [
        (
            b"MuMuEmulator12",
            {"path": "C:\\MuMu", "index": 3, "touch": False, "client_type": "Official"},
        )
    ]
    assert (5, b"0") in core.options
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert [item["status"] for item in result["task_results"]] == ["completed", "completed"]
    events = [json.loads(line) for line in (tmp_path / "callbacks.jsonl").read_text().splitlines()]
    assert [e["payload"]["taskid"] for e in events if e["message"] == 10001] == [1, 2]
    assert [e["payload"]["taskid"] for e in events if e["payload"].get("what") == "StageDrops"] == [
        2
    ]


def test_screenshot_failure_never_submits_a_game_task(tmp_path):
    core = FakeCore(method="RawByNc")
    code = run(spec(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core))
    assert code == 1 and not core.appended and not core.started
    assert "增强截图未生效" in (tmp_path / "core-result.json").read_text()


@pytest.mark.parametrize("malformed", [False, True])
def test_task_error_or_malformed_callback_is_not_reported_as_success(tmp_path, malformed):
    core = FakeCore(error=not malformed, malformed=malformed)
    assert run(spec(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    assert json.loads((tmp_path / "core-result.json").read_text())["status"] == "failed"
    assert core.destroyed


def test_cancellation_stops_the_owned_core_instance(tmp_path):
    stop = threading.Event()
    core = FakeCore(cancel=stop)
    assert run(spec(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core), stop=stop) == 1
    assert json.loads((tmp_path / "core-result.json").read_text())["status"] == "cancelled"
    assert core.destroyed


def test_dll_load_failure_has_a_structured_reason(tmp_path):
    def failed(_):
        raise OSError("missing dependency")

    assert run(spec(tmp_path), library_factory=failed) == 1
    assert "missing dependency" in (tmp_path / "core-result.json").read_text()


def test_destroy_failure_requires_recovery(tmp_path):
    class BrokenDestroy(FakeCore):
        def AsstDestroy(self, handle):
            raise OSError("destroy failed")

    core = BrokenDestroy()
    assert run(spec(tmp_path), library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    report = json.loads((tmp_path / "core-result.json").read_text())
    assert report["status"] == "recovery_required"
    assert "destroy failed" in report["reason"]


def test_copilot_settlement_evidence_is_exported_with_current_entry(tmp_path):
    class CopilotCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            common = {"taskid": identifier, "taskchain": kind}
            self.emit(
                20003,
                {
                    **common,
                    "what": "CopilotListLoadTaskFileSuccess",
                    "details": {
                        "id": 0,
                        "file_name": "a.json",
                        "stage_name": "act52side_ex01",
                    },
                },
            )
            self.emit(
                20002,
                {
                    **common,
                    "subtask": "ProcessTask",
                    "first": ["Copilot@WaitUntilEndOfAction"],
                    "details": {"task": "StageDrops-Stars-3"},
                },
            )
            return 10002

    core = CopilotCore()
    document = spec(tmp_path)
    document["tasks"] = [
        {
            "type": "Copilot",
            "params": {
                "copilot_list": [
                    {
                        "id": 0,
                        "filename": "a.json",
                        "nav_name_override": "YW-EX-1",
                        "is_raid": True,
                    },
                ]
            },
        }
    ]
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    result = json.loads((tmp_path / "core-result.json").read_text())
    report = result["copilot_results"][0]
    assert report["outcome"] == "completed" and report["is_raid"] is True
    assert report["stage"] == "act52side_ex01"
    assert report["evidence"][1].startswith("callbacks.jsonl:")


def test_legacy_fight_quota_cannot_certify_a_custom_delegation_task(tmp_path):
    class AnnihilationCore(FakeCore):
        def AsstStart(self, handle):
            result = super().AsstStart(handle)
            self.emit(
                20003,
                {
                    "taskid": 2,
                    "taskchain": "Fight",
                    "what": "StageDrops",
                    "details": {"annihilation_weekly_process": [1800, 1800]},
                },
            )
            return result

    core = AnnihilationCore()
    document = spec(tmp_path)
    document["tasks"][1]["params"]["stage"] = "Annihilation"
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    result = json.loads((tmp_path / "core-result.json").read_text())
    quota = result["annihilation_results"][0]
    assert quota["task_id"] == 2 and quota["outcome"] == "unknown"
    assert quota["observed_at"] > 0
    assert quota["evidence"] == []
    assert quota["current"] is None and quota["total"] is None


@pytest.mark.parametrize("kind", ["StartUp", "Recruit"])
@pytest.mark.parametrize("annihilation", [False, True])
def test_task_error_continues_independent_work_except_failed_startup(tmp_path, kind, annihilation):
    class FailingStepCore(FakeCore):
        def task_outcome(self, identifier, task_type, params):
            return 10000 if identifier == 2 else 10002

    core = FailingStepCore()
    doc = spec(tmp_path)
    first = (
        {"type": "Fight", "params": {"stage": "Annihilation"}}
        if annihilation
        else {"type": "Award"}
    )
    doc["tasks"] = [first, {"type": kind}, {"type": "Recruit"}, {"type": "CloseDown"}]
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    result = json.loads((tmp_path / "core-result.json").read_text())
    expected = (
        ["completed", "error"]
        if kind == "StartUp"
        else ["completed", "error", "completed", "completed"]
    )
    assert len(core.appended) == len(expected)
    assert [p["status"] for p in result["task_checkpoints"]] == expected
    assert result["task_checkpoints"][1]["index"] == 1
    assert result["task_checkpoints"][1]["evidence"].startswith("callbacks.jsonl:")

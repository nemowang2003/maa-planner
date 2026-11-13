import json
import threading
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.annihilation_tasks import delegation_task
from maa_planner.daemon.execution.core import run
from tests.helpers.core import FakeCore, spec


def task(**params):
    return {"type": "Fight", "params": {"stage": "Annihilation", "times": 5, **params}}


def test_graph_has_no_normal_deployment_or_unguarded_start():
    custom, nodes = delegation_task(task(), 0)
    prefix = "Planner@Annihilation0@"
    assert custom["type"] == "Custom"
    assert nodes[prefix + "Start"]["maxTimes"] == 5
    starts = []
    for name, value in nodes.items():
        for field in ("next", "onErrorNext", "exceededNext"):
            for target in value.get(field, []):
                assert target in nodes
                if target == prefix + "Start":
                    starts.append(name)
        if "baseTask" in value:
            assert all(k in value for k in ("next", "sub", "onErrorNext", "exceededNext"))
            assert value["baseTask"] not in {"StartButton2", "UsePrts", "FightBegin"}
    assert starts == [prefix + "Ready"]
    assert nodes[prefix + "Ready"]["baseTask"] == "UsePrts-AnnihilationSuccessCheck"
    assert prefix + "Ready" in nodes[prefix + "Dismiss"]["next"]


@pytest.mark.parametrize("stage", ["Chernobog", "LungmenOutskirts", "LungmenDowntown"])
def test_explicit_annihilation_selects_requested_map_before_guard(stage):
    _, nodes = delegation_task(task(stage=stage + "@Annihilation"), 0)
    assert nodes["Planner@Annihilation0@Select"]["baseTask"] == stage + "@AnnihilationStage"
    assert nodes["Planner@Annihilation0@Open"]["next"] == ["Planner@Annihilation0@SelectBack"]


@pytest.mark.parametrize("params", [{"times": -1}, {"times": True}, {"medicine": 1}, {"stone": 1}])
def test_invalid_or_unsupported_allowance_is_rejected_before_connection(params):
    with pytest.raises(ValueError):
        delegation_task(task(**params), 0)


@pytest.mark.parametrize("params", [{"times": 0}, {"enable": False}])
def test_disabled_task_never_navigates_or_starts(params):
    custom, _ = delegation_task(task(**params), 0)
    assert custom["params"]["task_names"] == ["Planner@Annihilation0@Limit"]


class DelegationCore(FakeCore):
    def __init__(self, ending, *, error=False, cancel=None):
        super().__init__()
        self.ending, self.phase_error, self.phase_cancel = ending, error, cancel
        self.executed = []

    def task_outcome(self, identifier, kind, params):
        self.executed.append(kind)
        common = {"taskid": identifier, "taskchain": kind}
        if kind == "Custom":
            # One successful delegation, followed by a fresh guard that cannot
            # certify another. A terminal Core error stops subsequent tasks.
            for name in ("Ready", "Start", "Confirm", "Settled", "Dismiss", self.ending):
                self.emit(
                    20002,
                    {**common, "subtask": "ProcessTask", "details": {"task": name}},
                )
            if self.phase_cancel is not None:
                self.phase_cancel.set()
                self.started = True
                self.cancel = self.phase_cancel
                return None
        return 10000 if kind == "Custom" and self.phase_error else 10002


@pytest.mark.parametrize(
    "ending,outcome",
    [
        ("Unavailable", "skipped"),
        ("OrdinaryProxy", "skipped"),
        ("NoProxy", "copilot_required"),
        ("Uncertain", "unknown"),
        ("Limit", "incomplete"),
        ("InsufficientSanity", "skipped"),
    ],
)
def test_delegation_terminal_evidence_continues_daily_without_weekly_completion(
    tmp_path, ending, outcome
):
    core = DelegationCore(ending, error=ending == "Uncertain")
    document = spec(tmp_path)
    document["tasks"].insert(1, task())
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == (
        1 if ending == "Uncertain" else 0
    )
    assert core.executed == ["StartUp", "Custom", "Fight"]
    report = json.loads((tmp_path / "core-result.json").read_text())
    result = report["annihilation_results"][0]
    assert result["outcome"] == outcome
    assert result["current"] is None and result["total"] is None
    assert result["evidence"][0].startswith("callbacks.jsonl:")
    assert report["handled_task_results"][0]["type"] == "Custom"
    nodes = json.loads((tmp_path / "annihilation-resources/resource/tasks.json").read_text())
    assert nodes["Planner@Annihilation1@Start"]["next"] == [
        "Planner@Annihilation1@Confirm",
        "Planner@Annihilation1@SanityRequired",
        "Planner@Annihilation1@StoneRequired",
    ]


def test_cancellation_never_submits_remaining_daily_tasks(tmp_path):
    stop = threading.Event()
    core = DelegationCore("Unavailable", cancel=stop)
    document = spec(tmp_path)
    document["tasks"].insert(1, task())
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core), stop=stop) == 1
    assert core.executed == ["StartUp", "Custom"]
    assert json.loads((tmp_path / "core-result.json").read_text())["status"] == "cancelled"


def test_nonzero_allowance_is_rejected_during_plan_validation():
    from maa_planner.daemon.execution.tasks import CoreTasks

    with pytest.raises(ValueError, match="自动恢复理智"):
        CoreTasks(json.dumps({"tasks": [task(medicine=1)]}))


def test_missing_terminal_callback_is_unknown_and_does_not_block_later_tasks(tmp_path):
    core = DelegationCore("UnrecognizedNode")
    document = spec(tmp_path)
    document["tasks"].insert(1, task())
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    report = json.loads((tmp_path / "core-result.json").read_text())
    result = report["annihilation_results"][0]
    assert result["outcome"] == "unknown" and result["observed_at"] > 0
    assert core.executed == ["StartUp", "Custom", "Fight"]

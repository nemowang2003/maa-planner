import json
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.core import run
from maa_planner.daemon.planning.annihilation_rescue import rescue_nodes
from tests.helpers.core import FakeCore, spec


def context():
    return {
        "catalog_commit": "a" * 40,
        "prepared_at": 1000,
        "owned_operators": [],
        "candidates": [
            {
                "stage_id": "camp_r_34",
                "name": "灰石剧场",
                "available_from": 1,
                "available_until": 9999999999,
            }
        ],
    }


@pytest.mark.parametrize(
    "settlement,proxy,unique,expected",
    [
        (True, True, True, "rescued"),
        (False, True, True, "copilot_required"),
        (True, False, True, "copilot_required"),
        (True, True, False, "copilot_required"),
    ],
)
def test_rescue_requires_unique_current_map_settlement_and_delegation_before_resuming(
    tmp_path, monkeypatch, settlement, proxy, unique, expected
):
    class RescueCore(FakeCore):
        def __init__(self):
            super().__init__()
            self.delegations = 0
            self.battles = 0

        def task_outcome(self, identifier, kind, params):
            common = {"taskid": identifier, "taskchain": kind}
            nodes = []
            if kind == "Custom":
                name = params["task_names"][0]
                if name.startswith("Planner@Annihilation"):
                    self.delegations += 1
                    nodes = ["NoProxy" if self.delegations == 1 else "Limit"]
                elif name.endswith("Stage0") and unique:
                    nodes = ["Stage0"]
                elif name.endswith("Wait"):
                    nodes = (["Settled"] if settlement else []) + (["Ready"] if proxy else [])
            if kind == "Copilot":
                self.battles += 1
                assert params["formation"] and params["support_unit_usage"] == 0
                assert params["use_sanity_potion"] is False
            for node in nodes:
                self.emit(20002, {**common, "subtask": "ProcessTask", "details": {"task": node}})
            return 10002

    searches = []

    def search(cache, entry, cancel):
        assert core.delegations == 1 and core.battles == 0
        searches.append(entry["stage_id"])
        return ((7, {"stage_name": "camp_r_34", "opers": [], "actions": []}),)

    monkeypatch.setattr("maa_planner.daemon.execution.annihilation_rescue.search_scripts", search)
    core = RescueCore()
    document = spec(tmp_path)
    document["tasks"] = [
        {"type": "Fight", "params": {"stage": "Annihilation", "times": 2}},
        {"type": "Award"},
    ]
    document["annihilation_copilot"] = context()
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["annihilation_results"][0]["outcome"] == expected
    assert core.delegations == (2 if expected == "rescued" else 1)
    assert core.battles == (1 if unique else 0)
    assert searches == (["camp_r_34"] if unique else [])
    if unique:
        import hashlib

        script_file = tmp_path / "annihilation-rescue.json"
        assert json.loads(script_file.read_text())["stage_name"] == "camp_r_34"
        assert (
            result["annihilation_results"][0]["rescue"]["script_sha256"]
            == hashlib.sha256(script_file.read_bytes()).hexdigest()
        )
    assert core.appended[-1][0] == "Award"
    assert [p["index"] for p in result["task_checkpoints"]] == [0, 1]


def test_verify_graph_cannot_reach_a_battle_start():
    nodes = rescue_nodes(context())
    assert nodes["Planner@Rescue@Ready"]["next"] == []
    assert not any("StartButton" in node.get("baseTask", "") for node in nodes.values())
    for node in nodes.values():
        for key in ("next", "onErrorNext", "exceededNext"):
            assert all(name in nodes for name in node.get(key, []))


def test_core_chain_error_does_not_start_rescue_even_after_no_proxy_evidence(tmp_path):
    class FailedCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            common = {"taskid": identifier, "taskchain": kind}
            self.emit(20002, {**common, "subtask": "ProcessTask", "details": {"task": "NoProxy"}})
            return 10000

    core = FailedCore()
    document = spec(tmp_path)
    document["tasks"] = [{"type": "Fight", "params": {"stage": "Annihilation", "times": 2}}]
    document["annihilation_copilot"] = context()
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == 1
    assert len(core.appended) == 1
    result = json.loads((tmp_path / "core-result.json").read_text())
    assert result["task_checkpoints"][0]["status"] == "error"


@pytest.mark.parametrize("node", ["Ready", "OrdinaryProxy", "Unavailable", "Uncertain", "Limit"])
def test_without_no_proxy_evidence_worker_never_searches(tmp_path, monkeypatch, node):
    monkeypatch.setattr(
        "maa_planner.daemon.execution.annihilation_rescue.search_scripts",
        lambda *a: pytest.fail("no live NoProxy"),
    )

    class ProxyCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            common = {"taskid": identifier, "taskchain": kind}
            self.emit(20002, {**common, "subtask": "ProcessTask", "details": {"task": node}})
            return 10002

    doc = spec(tmp_path)
    doc["tasks"] = [{"type": "Fight", "params": {"stage": "Annihilation", "times": 2}}]
    doc["annihilation_copilot"] = context()
    core = ProxyCore()
    assert run(doc, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert len(core.appended) == 1


def test_fixed_permanent_stage_never_searches_or_starts_rescue(tmp_path):
    import threading

    from maa_planner.daemon.execution.annihilation_rescue import run_rescue

    result = run_rescue(
        context(),
        "Chernobog@Annihilation",
        lambda *a, **k: pytest.fail("permanent rescue forbidden"),
        None,
        threading.Event(),
        directory=tmp_path,
    )
    assert result["reason"] == "not_current_rotation"


def test_delegation_graph_verifies_current_rotation_before_any_proxy_action():
    from maa_planner.daemon.execution.annihilation_tasks import delegation_task

    _, nodes = delegation_task(
        {"type": "Fight", "params": {"stage": "Annihilation"}},
        0,
        current=context()["candidates"][0],
    )
    prefix = "Planner@Annihilation0@"
    assert nodes[prefix + "Open"]["next"] == [prefix + "Open", prefix + "Current"]
    assert nodes[prefix + "Current"]["text"] == ["灰石剧场"]
    assert prefix + "Ready" in nodes[prefix + "Current"]["next"]
    assert prefix + "NoProxy" in nodes[prefix + "Current"]["next"]
    assert nodes[prefix + "Current"]["onErrorNext"] == [prefix + "Uncertain"]

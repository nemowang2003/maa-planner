from dataclasses import asdict

import pytest

from maa_planner.daemon.execution.annihilation import AnnihilationResult
from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.execution.core import CallbackResults


def reducer():
    result = CallbackResults()
    result.annihilation[1] = AnnihilationResult(1, "Annihilation")
    return result


@pytest.mark.parametrize("prefix", ["", "Planner@Annihilation0@"])
@pytest.mark.parametrize(
    "node,outcome,reason",
    [
        ("NoProxy", "copilot_required", "no_auto_deploy"),
        ("OrdinaryProxy", "skipped", "delegation_unavailable"),
        ("Unavailable", "skipped", "delegation_unavailable"),
        ("InsufficientSanity", "skipped", "insufficient_sanity"),
        ("Limit", "incomplete", "attempt_limit"),
        ("Uncertain", "unknown", "recognition_failed"),
    ],
)
def test_custom_terminal_nodes_use_registered_task_context(prefix, node, outcome, reason):
    result = reducer()
    result.tasks[1] = {"type": "Custom"}
    result.consume(
        20002,
        {
            "taskid": 1,
            "taskchain": "Custom",
            "subtask": "ProcessTask",
            "details": {"task": prefix + node},
        },
        "callbacks.jsonl:5",
        123,
    )
    assert result.annihilation[1] == AnnihilationResult(
        1, "Annihilation", outcome, reason, observed_at=123, evidence=("callbacks.jsonl:5",)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"taskid": 2},
        {"taskid": True},
        {"taskchain": "Fight"},
        {"subtask": "OtherTask"},
        {"details": {"task": "Ready"}},
        {"details": {"task": None}},
        {"details": []},
    ],
)
def test_unrelated_or_malformed_callbacks_cannot_supply_terminal_evidence(change):
    result = reducer()
    result.tasks[1] = {"type": "Custom"}
    result.tasks[2] = {"type": "Custom"}
    result.consume(
        20002,
        {
            "taskid": 1,
            "taskchain": "Custom",
            "subtask": "ProcessTask",
            "details": {"task": "NoProxy"},
            **change,
        },
        "callbacks.jsonl:5",
        123,
    )
    assert result.annihilation == {1: AnnihilationResult(1, "Annihilation")}


@pytest.mark.parametrize(
    "registered_type,message",
    [
        (None, 20002),
        ("Fight", 20002),
        ("Copilot", 20002),
        ("Custom", 20001),
        ("Custom", 20000),
        ("Custom", 10002),
    ],
)
def test_terminal_leaf_requires_registered_custom_task_and_completed_node(registered_type, message):
    result = reducer()
    if registered_type is not None:
        result.tasks[1] = {"type": registered_type, "subtask_errors": 0}
    result.consume(
        message,
        {
            "taskid": 1,
            "taskchain": "Custom",
            "subtask": "ProcessTask",
            "details": {"task": "NoProxy"},
        },
        "callbacks.jsonl:5",
        123,
    )
    assert result.annihilation[1] == AnnihilationResult(1, "Annihilation")


def test_terminal_evidence_stays_with_its_own_annihilation_task():
    result = reducer()
    result.annihilation[2] = AnnihilationResult(2, "LungmenDowntown@Annihilation")
    result.tasks = {1: {"type": "Custom"}, 2: {"type": "Custom"}}
    for task_id, node in ((1, "NoProxy"), (2, "Limit")):
        result.consume(
            20002,
            {
                "taskid": task_id,
                "taskchain": "Custom",
                "subtask": "ProcessTask",
                "details": {"task": node},
            },
            f"callbacks.jsonl:{task_id}",
            123 + task_id,
        )
    assert result.annihilation[1] == AnnihilationResult(
        1,
        "Annihilation",
        "copilot_required",
        "no_auto_deploy",
        observed_at=124,
        evidence=("callbacks.jsonl:1",),
    )
    assert result.annihilation[2] == AnnihilationResult(
        2,
        "LungmenDowntown@Annihilation",
        "incomplete",
        "attempt_limit",
        observed_at=125,
        evidence=("callbacks.jsonl:2",),
    )


@pytest.mark.parametrize(
    "quota,outcome",
    [
        ([1800, 1800], "completed"),
        ([350, 1800], "incomplete"),
        ([True, 1800], "unknown"),
        ([18000, 1800], "unknown"),
        ([0, 0], "unknown"),
        ([1800], "unknown"),
    ],
)
def test_only_explicit_valid_weekly_quota_can_prove_completion(quota, outcome):
    result = reducer()
    result.consume(
        20003,
        {
            "taskid": 1,
            "taskchain": "Fight",
            "what": "StageDrops",
            "details": {"annihilation_weekly_process": quota},
        },
        "callbacks.jsonl:1",
        123,
    )
    result.consume(10002, {"taskid": 1, "taskchain": "Fight"}, "callbacks.jsonl:2", 124)
    assert result.annihilation[1].outcome == outcome


def test_missing_proxy_is_copilot_required_but_task_success_alone_is_unknown():
    result = reducer()
    result.consume(
        20002,
        {
            "taskid": 1,
            "taskchain": "Fight",
            "subtask": "ProcessTask",
            "details": {"task": "Annihilation@UnableToAgent2"},
        },
        "callbacks.jsonl:5",
        123,
    )
    assert result.annihilation[1].outcome == "copilot_required"
    roundtrip = RunResult.from_dict(
        asdict(RunResult("a", "succeeded", annihilation_results=(result.annihilation[1],)))
    )
    assert roundtrip.annihilation_results[0].evidence == ("callbacks.jsonl:5",)
    untouched = reducer()
    untouched.consume(10002, {"taskid": 1, "taskchain": "Fight"}, "callbacks.jsonl:2")
    assert untouched.annihilation[1].outcome == "unknown"

from maa_planner.daemon.execution.copilot_results import CopilotResult, CopilotResults


def observe(reducer, message, **payload):
    reducer.consume(
        message, {"taskid": 7, "taskchain": "Copilot", **payload}, "callbacks.jsonl:3", 1234
    )


def reducer():
    result = CopilotResults()
    result.register(
        7,
        {
            "copilot_list": [
                {"id": 0, "filename": "a.json", "nav_name_override": "YW-EX-1", "is_raid": False},
                {"id": 1, "filename": "a.json", "nav_name_override": "YW-EX-1", "is_raid": True},
            ]
        },
    )
    return result


def load(result, identifier=0, **details):
    observe(
        result,
        20003,
        what="CopilotListLoadTaskFileSuccess",
        details={
            "id": identifier,
            "file_name": "a.json",
            "stage_name": "act52side_ex01",
            **details,
        },
    )


def settle(result, task="StageDrops-Stars-3", **payload):
    observe(
        result,
        20002,
        subtask="ProcessTask",
        first=["Copilot@WaitUntilEndOfAction"],
        details={"task": task},
        **payload,
    )


def test_completed_entry_is_bound_to_loaded_file_and_mode():
    result = reducer()
    load(result, 1)
    settle(result)
    reports = result.to_list()
    assert reports[0]["outcome"] == "unknown"
    report = CopilotResult.from_dict(reports[1])
    assert report.outcome == "completed" and report.is_raid is True
    assert report.stage == "act52side_ex01" and report.observed_at == 1234
    assert report.evidence == ("callbacks.jsonl:3", "callbacks.jsonl:3")


def test_task_success_without_settlement_never_proves_victory():
    result = reducer()
    load(result)
    observe(result, 10002)
    assert result.to_list()[0]["outcome"] == "unknown"


def test_unrelated_star_or_mismatched_load_never_proves_victory():
    result = reducer()
    settle(result)
    load(result, file_name="unexpected.json")
    settle(result)
    assert all(r["outcome"] == "unknown" for r in result.to_list())
    load(result)
    observe(
        result,
        20002,
        subtask="ProcessTask",
        first=["Another@Begin"],
        details={"task": "StageDrops-Stars-3"},
    )
    assert result.to_list()[0]["outcome"] == "unknown"


def test_explicit_defeat_is_separate_from_generic_task_error():
    result = reducer()
    load(result)
    observe(result, 20000, subtask="ProcessTask")
    assert result.to_list()[0]["outcome"] == "unknown"
    settle(result, "Copilot@FightMissionFailed")
    assert result.to_list()[0]["outcome"] == "failed"
    assert result.to_list()[0]["reason"] == "battle_failed"


def test_later_generic_error_does_not_erase_positive_evidence():
    result = reducer()
    load(result)
    settle(result)
    observe(result, 10000)
    assert result.to_list()[0]["outcome"] == "completed"

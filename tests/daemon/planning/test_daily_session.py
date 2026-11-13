import json

import pytest

from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.planning.schedule import parse_schedules


def test_daemon_arranges_session_independently_of_business_order_and_retry(tmp_path):
    schedule = parse_schedules(
        [
            {
                "id": "daily",
                "profiles": ["nemo"],
                "times": ["12:00"],
                "session": {"startup": {"account_name": "nemo"}, "closedown": {}},
                "tasks": [{"type": "Award"}, {"type": "Recruit"}],
            }
        ],
        {"nemo"},
        tmp_path / "config.toml",
    )[0]
    for pending, expected in (
        (None, ["task:0", "task:1"]),
        (frozenset({"task:1"}), ["task:1"]),
        (frozenset({"lifecycle:closedown"}), []),
    ):
        plan = build_daily_plan(schedule, now=2000, pending=pending)
        assert plan.tasks is not None
        doc = json.loads(plan.tasks.document)
        assert doc["business_ids"] == ["lifecycle:startup", *expected, "lifecycle:closedown"]
        assert doc["tasks"][0]["params"]["account_name"] == "nemo"
        assert doc["tasks"][-1]["type"] == "CloseDown"


@pytest.mark.parametrize("kind", ["StartUp", "CloseDown", "Unknown"])
def test_business_task_list_validates_its_own_task_types(tmp_path, kind):
    with pytest.raises(ValueError, match="未知日程任务类型"):
        parse_schedules(
            [
                {
                    "id": "daily",
                    "profiles": ["nemo"],
                    "times": ["12:00"],
                    "tasks": [{"type": kind}, {"type": "Recruit"}],
                }
            ],
            {"nemo"},
            tmp_path / "config.toml",
        )

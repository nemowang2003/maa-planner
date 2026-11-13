import json
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.core import run
from maa_planner.daemon.execution.tasks import CoreTasks
from tests.helpers.core import FakeCore, spec


@pytest.mark.parametrize("stage", [None, "1-7", "PR-A-1", "TL-9"])
@pytest.mark.parametrize("legacy", [{}, {"times": 30, "series": 6}, {"times": 0, "series": 1}])
def test_ordinary_fight_plan_and_worker_use_auto_series_without_times(tmp_path, stage, legacy):
    params = {"medicine": 2, "stone": 0, "medicine_expire_days": 2, **legacy}
    if stage is not None:
        params["stage"] = stage
    source = {"tasks": [{"type": "Fight", "params": params}]}
    plan = CoreTasks(json.dumps(source))
    expected = {key: value for key, value in params.items() if key not in {"times", "series"}}
    expected["series"] = 0
    assert json.loads(plan.document)["tasks"][0]["params"] == expected
    assert CoreTasks(plan.document) == plan
    assert source["tasks"][0]["params"] == params

    core = FakeCore()
    document = {**spec(tmp_path), **source}
    assert run(document, library_factory=lambda _: SimpleNamespace(lib=core)) == 0
    assert core.appended == [("Fight", expected)]


def test_annihilation_attempt_cap_and_other_task_times_remain_independent():
    tasks = [
        {"type": "Fight", "params": {"stage": "Annihilation", "times": 5}},
        {"type": "Recruit", "params": {"times": 2}},
        {"type": "Mall", "params": {"credit_fight": True}},
    ]
    assert json.loads(CoreTasks(json.dumps({"tasks": tasks})).document)["tasks"] == tasks

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from maa_planner.daemon import scheduling
from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.planning.activity import RecommendedStage
from maa_planner.daemon.preparation import Preparation
from maa_planner.schema import StageConfirmation
from tests.helpers.activity_shop import activity, shop_catalog, shop_plan
from tests.helpers.clearance import no_clearance_needed
from tests.helpers.scheduling import Clock, eventually, running, wake


def test_shop_proof_streams_before_daily_end_survives_failure_restart_and_changes_next_fight(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "maa_planner.daemon.planning.auto_copilot.prepare_automatic", no_clearance_needed
    )

    def snapshot():
        value = activity()
        story = replace(
            value.data.side_stories[0],
            stages=tuple(
                RecommendedStage(display=code, value=code, drop=drop)
                for code, drop in (
                    ("EV-9", "31113"),
                    ("EV-8", "31083"),
                    ("EV-7", "31043"),
                    ("EV-4", "搓玉效率0.69"),
                )
            ),
        )
        return replace(value, data=replace(value.data, side_stories=(story,)))

    async def catalog_loader(self):
        return shop_catalog()

    material_counts = {"31113": 2, "31083": 30, "31043": 40}

    async def info(settings, profile):
        return SimpleNamespace(
            cultivate={
                "items": [{"id": item, "count": count} for item, count in material_counts.items()]
            },
            player_info={},
        ), profile.name

    content = """[[profiles]]
name="a"
[[schedules]]
id="daily"
profiles=["a"]
times=["08:33", "08:40", "08:45", "08:49"]
shop_blacklist=["家具"]
session={startup={}}
[[schedules.tasks]]
type="Fight"
strategy="expert"
params={times=30,medicine=0,stone=0}
[[schedules.tasks]]
type="Award"
"""

    async def scenario():
        clock = Clock("1970-01-01T08:30:00")
        monkeypatch.setattr("tests.helpers.scheduling.empty_activity", snapshot)
        monkeypatch.setattr(Preparation, "stage_catalog", catalog_loader)
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            monkeypatch.setattr(scheduling, "collect_character_info", info)
            for stage, raid in shop_plan()["required"]:
                state.daemon.store.confirm_stage(
                    StageConfirmation("a", "event", stage, raid), now=clock.now()
                )
            wake(state, clock, "1970-01-01T08:33:00")
            await eventually(lambda: len(state.workers) == 1)
            worker = state.workers[0]
            document = json.loads(worker.request.core.tasks.document)
            assert document["tasks"][1]["params"]["stage"] == "EV-9"
            plan = document["activity_shops"][0]
            assert plan["after_task"] == 1
            report = {
                "activity_id": "event",
                "fingerprint": plan["fingerprint"],
                "after_task": 1,
                "outcome": "completed",
                "reason": "unlimited_commodity",
                "evidence": ["callbacks.jsonl:40"],
                "observed_at": clock.now(),
            }
            state.daemon.daily.shop_report(worker.request, report)
            assert state.daemon.store.shop_state("a", "event", plan["fingerprint"])["completed"]
            worker.result = RunResult(
                worker.request.attempt_id, "failed", "领奖失败", activity_shop_results=(report,)
            )
            worker.finish.set()
            await eventually(lambda: not state.daemon.daily.attempts)
            assert (
                len(
                    [e for e in state.daemon.store.events(0) if e["kind"] == "activity_shop_result"]
                )
                == 1
            )
        clock.set("1970-01-01T08:34:00")
        async with running(tmp_path, monkeypatch, clock) as state:
            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "1970-01-01T08:40:00")
            await eventually(lambda: len(state.workers) == 1)
            document = json.loads(state.workers[0].request.core.tasks.document)
            assert document["tasks"][1]["params"]["stage"] == "EV-9"
            assert "times" not in document["tasks"][1]["params"]
            assert document["tasks"][1]["params"]["series"] == 0
            assert "activity_shops" not in document
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.daily.attempts)
            material_counts.update({"31113": 100, "31083": 100, "31043": 100})
            wake(state, clock, "1970-01-01T08:45:00")
            await eventually(lambda: len(state.workers) == 2)
            document = json.loads(state.workers[1].request.core.tasks.document)
            assert document["tasks"][1]["params"]["stage"] == "PR-A-1"
            assert "activity_shops" not in document
            state.workers[1].finish.set()
            await eventually(lambda: not state.daemon.daily.attempts)
            material_counts["31113"] = 99
            wake(state, clock, "1970-01-01T08:49:00")
            await eventually(lambda: len(state.workers) == 3)
            document = json.loads(state.workers[2].request.core.tasks.document)
            assert document["tasks"][1]["params"]["stage"] == "EV-9"
            assert "activity_shops" not in document
            state.workers[2].finish.set()

    asyncio.run(scenario())

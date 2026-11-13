import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from maa_planner.daemon import observations as api_module
from maa_planner.daemon.config import settings_from_bytes
from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.planning import auto_copilot
from maa_planner.daemon.preparation import Preparation
from maa_planner.daemon.state import StateStore
from tests.helpers.async_tools import eventually
from tests.helpers.clearance import NOW, activity, catalog, clearance
from tests.helpers.planning import farming_activity
from tests.helpers.scheduling import Clock, document, running, wake


@pytest.mark.parametrize("failed,disabled", [(False, False), (True, False), (False, True)])
def test_timer_uses_automatic_search_or_skips_only_farming_on_prepare_failure(
    tmp_path, monkeypatch, failed, disabled
):
    calls = []

    async def prepare(
        settings, profile, info, activity, store, load_catalog, load_sets, cancel, *, now
    ):
        calls.append((profile.name, info))
        if failed:
            raise ValueError("Skland 干员拥有数据缺失")
        return clearance()

    monkeypatch.setattr(auto_copilot, "prepare_automatic", prepare)
    monkeypatch.setattr("tests.helpers.scheduling.empty_activity", farming_activity)

    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        content = document(names=("a",))
        if disabled:
            content = content.replace('type="Fight"', 'type="Fight"\nenabled=false')
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            if disabled:
                await eventually(
                    lambda: any(
                        e["kind"] == "schedule" and e["data"]["status"] == "skipped"
                        for e in state.daemon.store.events(0)
                    )
                )
                assert not state.workers and not calls
                return
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            worker = state.workers[0]
            doc = json.loads(worker.request.core.tasks.document)
            assert [t["type"] for t in doc["tasks"]] == (
                ["StartUp", "CloseDown"] if failed else ["StartUp", "Fight", "CloseDown"]
            )
            assert ("activity_copilot" in doc) == (not failed and not disabled)
            assert bool(state.daemon.store.alerts("a")) == failed
            worker.finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())
    assert calls == ([] if disabled else [("a", None)])


def test_preparation_shares_public_query_once_but_does_not_share_account_rosters(
    tmp_path, monkeypatch
):
    calls = []

    def query(settings, cancel):
        calls.append(settings)
        return ({"id": 1},)

    monkeypatch.setattr(auto_copilot, "query_sets", query)

    async def scenario():
        preparation = Preparation(
            settings_from_bytes(b'[[profiles]]\nname="a"\n', tmp_path / "config.toml")
        )
        a, b = await asyncio.gather(preparation.copilot_sets(), preparation.copilot_sets())
        assert a is b

    asyncio.run(scenario())
    assert len(calls) == 1


def test_prepare_collects_roster_per_account_and_reuses_an_existing_observation(
    tmp_path, monkeypatch
):
    calls = []
    info = SimpleNamespace(
        cultivate={"characters": [{"id": "a"}]},
        player_info={"charInfoMap": {"a": {"id": "a", "name": "甲"}}},
    )

    async def collect(settings, profile):
        calls.append(profile.name)
        return info, profile.name

    monkeypatch.setattr(auto_copilot, "collect_character_info", collect)

    async def load_catalog():
        return catalog()[1]

    async def load_sets():
        return ()

    async def scenario():
        store = StateStore(tmp_path / "state.sqlite3")
        settings = SimpleNamespace(cache_directory=tmp_path)
        for name, observation in (("a", None), ("b", None), ("c", info)):
            plan, _, _ = await auto_copilot.prepare_automatic(
                settings,
                SimpleNamespace(name=name),
                observation,
                activity(),
                store,
                load_catalog,
                load_sets,
                threading.Event(),
                now=NOW,
            )
            assert plan["targets"] == []
            assert plan["selection"][0]["status"] == "no_usable_set"
        store.close()

    asyncio.run(scenario())
    assert calls == ["a", "b"]


@pytest.mark.parametrize("failed", [False, True])
def test_plan_preview_uses_same_automatic_preparation_and_reports_errors(
    tmp_path, monkeypatch, failed
):
    config = b"""[[profiles]]
name="a"
[[schedules]]
id="preview"
profiles=["a"]
times=["12:00"]
[[schedules.tasks]]
type="Fight"
[[schedules.tasks]]
type="Award"
"""
    settings = settings_from_bytes(config, tmp_path / "config.toml")

    async def snapshot(self, settings):
        return activity()

    async def prepare(*args, **kwargs):
        if failed:
            raise OSError("query offline")
        return clearance()

    monkeypatch.setattr(Daemon, "activity_snapshot", snapshot)
    monkeypatch.setattr(api_module.time, "time", lambda: NOW)
    monkeypatch.setattr(auto_copilot, "prepare_automatic", prepare)
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control")
    daemon.store = StateStore(tmp_path / "state.sqlite3")
    api = daemon.observations
    result = asyncio.run(
        api.execute("plan", settings, threading.Event(), {"schedule_id": "preview"})
    )
    assert [t["type"] for t in result["tasks"]] == (["Award"] if failed else ["Fight", "Award"])
    assert bool(result["activity_copilot_error"]) == failed
    assert bool(result["activity_copilot"]) != failed
    daemon.store.close()


@pytest.mark.parametrize(
    "task",
    [
        {"type": "Award"},
        {"type": "Infrast"},
        {"type": "Fight", "params": {"stage": "Annihilation", "times": 1}},
    ],
)
def test_preview_without_regular_fight_never_fetches_clearance_dependencies(
    tmp_path, monkeypatch, task
):
    from dataclasses import replace

    from maa_planner.daemon.planning.schedule import parse_schedules

    settings = settings_from_bytes(b'[[profiles]]\nname="a"\n', tmp_path / "config.toml")
    schedule = parse_schedules(
        [{"id": "preview", "profiles": ["a"], "times": ["12:00"], "tasks": [task]}],
        {"a"},
        tmp_path / "config.toml",
    )[0]
    settings = replace(settings, schedules=(schedule,))

    async def unexpected(*args, **kwargs):
        pytest.fail("没有普通 Fight 时不应获取活动快照或自动通关依赖")

    monkeypatch.setattr(Daemon, "activity_snapshot", unexpected)
    monkeypatch.setattr(auto_copilot, "prepare_automatic", unexpected)
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control")
    daemon.store = StateStore(tmp_path / "preview.sqlite3")
    api = daemon.observations
    result = asyncio.run(
        api.execute("plan", settings, threading.Event(), {"schedule_id": "preview"})
    )
    assert [t["type"] for t in result["tasks"]] == [task["type"]]
    assert result["activity_copilot"] is None
    assert result["activity_copilot_error"] is None

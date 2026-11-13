import asyncio
import json
from types import SimpleNamespace

from maa_planner.daemon.planning.annihilation_rescue import prepare_rescue
from maa_planner.daemon.preparation import Preparation
from tests.helpers.scheduling import Clock, document, eventually, running, wake


def test_timer_prepares_frozen_rescue_and_operator_roster_before_worker(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        content = (
            document(names=("a",))
            .replace('type="Fight"', 'type="Fight"\nstrategy="expert"')
            .replace(
                'stage="1-7", medicine=0, stone=0, expiring_medicine=999, medicine_expire_days=2',
                'stage="Annihilation", times=2, medicine=0, stone=0',
            )
        )
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            monkeypatch.setattr(
                "maa_planner.daemon.planning.annihilation_rescue.prepare_rescue", prepare_rescue
            )

            async def info(settings, profile):
                return SimpleNamespace(
                    cultivate={"characters": [{"id": "owned"}]},
                    player_info={
                        "charInfoMap": {"one": {"id": "owned", "name": "拥有"}},
                        "status": {"storeTs": clock.now()},
                        "campaign": {"reward": {"current": 0, "total": 1800}},
                    },
                ), profile.name

            async def candidates(self, cancel, *, now):
                return "a" * 40, (
                    {
                        "stage_id": "camp_r_34",
                        "name": "灰石剧场",
                        "available_from": 1,
                        "available_until": 9999999999,
                    },
                )

            monkeypatch.setattr("maa_planner.daemon.scheduling.collect_character_info", info)
            monkeypatch.setattr(Preparation, "annihilation_candidates", candidates)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            tasks = state.workers[0].request.core.tasks
            doc = json.loads(tasks.document)
            assert "annihilation_copilot" in doc, [
                a["details"] for a in state.daemon.store.alerts("a")
            ]
            assert doc["annihilation_copilot"]["owned_operators"] == ["拥有"]
            assert "script_id" not in doc["annihilation_copilot"]["candidates"][0]
            assert "Planner@Rescue@Stage0" in doc["resource_tasks"]
            assert "annihilation-7.json" not in json.loads(tasks.attachments)
            assert not state.daemon.store.alerts("a")
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())


def test_plan_preview_contains_same_rescue_without_installing_core(tmp_path, monkeypatch):
    import threading

    from maa_planner.daemon.config import settings_from_bytes
    from maa_planner.daemon.coordinator import Daemon
    from maa_planner.daemon.state import StateStore
    from tests.helpers.activity import empty_activity

    content = (
        document(names=("a",))
        .replace('type="Fight"', 'type="Fight"\nstrategy="expert"')
        .replace(
            'stage="1-7", medicine=0, stone=0, expiring_medicine=999, medicine_expire_days=2',
            'stage="Annihilation", times=2, medicine=0, stone=0',
        )
    )
    settings = settings_from_bytes(content.encode(), tmp_path / "config.toml")
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control")
    daemon.store = StateStore(tmp_path / "state.sqlite3")
    clock = Clock("2026-11-02T12:10:00")

    async def info(settings, profile):
        return SimpleNamespace(
            cultivate={"characters": []},
            player_info={
                "charInfoMap": {},
                "status": {"storeTs": clock.now()},
                "campaign": {"reward": {"current": 0, "total": 1800}},
            },
        ), profile.name

    async def candidates(self, cancel, *, now):
        return "a" * 40, (
            {
                "stage_id": "camp_r_34",
                "name": "剿灭",
                "available_from": 1,
                "available_until": 9999999999,
            },
        )

    async def activity(self, settings):
        return empty_activity()

    monkeypatch.setattr(Daemon, "activity_snapshot", activity)
    monkeypatch.setattr("maa_planner.daemon.planning.skland.collect_character_info", info)
    monkeypatch.setattr(Preparation, "annihilation_candidates", candidates)
    monkeypatch.setattr("maa_planner.daemon.observations.time.time", clock.now)
    monkeypatch.setattr(
        "maa_planner.daemon.tools.stage_catalog.load_stage_catalog",
        lambda cache: {
            "commit": "a" * 40,
            "stage_table": {},
            "zone_table": {},
            "activity_table": {"basicInfo": {}},
        },
    )
    try:
        result = asyncio.run(
            daemon.observations.execute(
                "plan", settings, threading.Event(), {"schedule_id": "daily"}
            )
        )
        assert result["annihilation_copilot_error"] is None
        assert result["annihilation_copilot"]["candidates"][0]["stage_id"] == "camp_r_34"
        assert "annihilation-7.json" not in result["attachments"]
        assert not daemon.running
    finally:
        daemon.store.close()

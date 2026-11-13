import asyncio
import json
import threading
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from maa_planner.daemon import scheduling
from maa_planner.daemon.config import settings_from_bytes
from tests.helpers.async_tools import eventually
from tests.helpers.clearance import clearance
from tests.helpers.planning import farming_activity, skland_base, warehouse
from tests.helpers.scheduling import Clock, running, wake


@pytest.mark.parametrize("failed_stage", [None, "clearance", "shop", "rescue", "processing"])
def test_preview_and_daily_share_plan_and_isolate_optional_preparation_failures(
    tmp_path, monkeypatch, failed_stage
):
    for mode in ("normal", "three_gold", "orundum", "sell_only"):
        (tmp_path / f"{mode}.json").write_text(json.dumps({"plans": [{"name": mode}]}))
    content = """[[profiles]]
name="a"
[[schedules]]
id="daily"
profiles=["a"]
times=["12:10"]
[[schedules.tasks]]
name="base"
type="Infrast"
strategy="expert"
files={normal="normal.json",three_gold="three_gold.json",orundum="orundum.json",sell_only="sell_only.json"}
processing={operators=["芳汀"],targets={T3=20}}
[[schedules.tasks]]
name="farm"
type="Fight"
strategy="expert"
params={stage="1-7",times=2}
[[schedules.tasks]]
name="weekly"
type="Fight"
strategy="expert"
params={stage="Annihilation",times=2}
[[schedules.tasks]]
name="award"
type="Award"
"""
    monkeypatch.setattr("tests.helpers.scheduling.empty_activity", farming_activity)

    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        async with running(tmp_path, monkeypatch, clock, content=content) as state:

            async def info(settings, profile):
                player = skland_base()
                player["campaign"] = {"reward": {"current": 0, "total": 1800}}
                return SimpleNamespace(cultivate=warehouse(), player_info=player), profile.name

            async def automatic(*args, **kwargs):
                if failed_stage == "clearance":
                    raise OSError("clearance unavailable")
                return clearance()

            async def shops(*args, **kwargs):
                if failed_stage == "shop":
                    raise OSError("shop unavailable")
                return ()

            async def rescue(*args, **kwargs):
                if failed_stage == "rescue":
                    raise OSError("rescue unavailable")
                return None

            async def processing(*args, **kwargs):
                if failed_stage == "processing":
                    raise OSError("processing unavailable")
                return {
                    "catalog_commit": "a" * 40,
                    "stock": {"p": 0, "base": 20},
                    "catalog": {
                        "item_table": {
                            "items": {
                                "p": {"name": "材料", "rarity": "TIER_4", "itemType": "MATERIAL"},
                            }
                        },
                        "building_data": {
                            "workshopFormulas": {
                                "p": {
                                    "formulaId": "p",
                                    "itemId": "p",
                                    "count": 1,
                                    "goldCost": 0,
                                    "formulaType": "F_EVOLVE",
                                    "costs": [{"id": "base", "count": 2}],
                                }
                            }
                        },
                    },
                }

            async def snapshot(settings):
                return farming_activity()

            monkeypatch.setattr(scheduling, "collect_character_info", info)
            monkeypatch.setattr("maa_planner.daemon.planning.skland.collect_character_info", info)
            monkeypatch.setattr(
                "maa_planner.daemon.planning.auto_copilot.prepare_automatic", automatic
            )
            monkeypatch.setattr(
                "maa_planner.daemon.planning.activity_shop.prepare_shop_context", shops
            )
            monkeypatch.setattr(
                "maa_planner.daemon.planning.annihilation_rescue.prepare_rescue", rescue
            )
            monkeypatch.setattr(
                "maa_planner.daemon.planning.processing.prepare_processing", processing
            )
            monkeypatch.setattr(state.daemon.observations, "activity_snapshot", snapshot)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            request = state.workers[0].request
            frozen = json.loads(request.core.tasks.document)
            events_before = [e for e in state.daemon.store.events(0) if e["kind"] != "preparation"]
            pending_before = dict(state.daemon.daily.pending)
            preview = await state.daemon.observations.execute(
                "plan",
                settings_from_bytes(content.encode(), state.config),
                threading.Event(),
                {"schedule_id": "daily"},
            )
            assert preview["tasks"] == frozen["tasks"]
            assert preview["attachments"] == json.loads(request.core.tasks.attachments)
            for key in ("resource_tasks", "resource_images"):
                assert preview[key] == frozen.get(key, {})
            assert preview["infrast_processing"] == frozen.get("infrast_processing", [])
            assert preview["annihilation"] == asdict(
                state.daemon.daily.annihilation_plans[request.attempt_id]
            )
            names = [t["name"] for t in preview["tasks"]]
            assert "base" in names and "award" in names and "weekly" in names
            assert ("farm" in names) == (failed_stage != "clearance")
            assert bool(preview["infrast_processing"]) == (failed_stage != "processing")
            error_field = {
                "clearance": "activity_copilot_error",
                "shop": "activity_shop_error",
                "rescue": "annihilation_copilot_error",
                "processing": "infrast_processing_error",
            }
            for stage, field in error_field.items():
                assert bool(preview[field]) == (failed_stage == stage)
            assert [
                e for e in state.daemon.store.events(0) if e["kind"] != "preparation"
            ] == events_before
            assert state.daemon.daily.pending == pending_before
            assert len(state.workers) == 1
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())


def test_cancelled_evidence_collection_holds_resources_until_its_thread_finishes(
    tmp_path, monkeypatch
):
    from tests.helpers.control import ApiPeer
    from tests.helpers.scheduling import document

    entered, release = threading.Event(), threading.Event()

    def fetch():
        entered.set()
        assert release.wait(5)
        raise ValueError("采集结束")

    async def info(*args):
        return await asyncio.to_thread(fetch)

    async def scenario():
        clock = Clock()
        content = document(("a",)).replace('type="Fight"', 'type="Fight"\nstrategy="expert"')
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(entered.is_set)
            state.config.write_text(content.replace("enabled=true", "enabled=false"))
            await eventually(lambda: not state.daemon.daily.pending)
            client = ApiPeer(state.daemon.server.path)
            try:
                with pytest.raises(RuntimeError, match="准备"):
                    await client.start("resource.update", idempotency_key="collect-still-running")
                assert not state.workers
            finally:
                release.set()
            await eventually(lambda: not state.daemon.daily.preparing)
            assert not state.workers

    try:
        asyncio.run(scenario())
    finally:
        release.set()

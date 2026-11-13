import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

from maa_planner.daemon.config import settings_from_bytes
from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.state import StateStore
from maa_planner.schema import StageConfirmation
from tests.helpers.activity_shop import NOW, activity, configured, shop_catalog, shop_plan


def test_preview_joins_persisted_shop_proof_and_reuses_clearance_catalog_without_game(
    tmp_path, monkeypatch
):
    schedule = configured(tmp_path)
    settings = replace(
        settings_from_bytes(b'[[profiles]]\nname="a"', tmp_path / "config.toml"),
        schedules=(schedule,),
    )
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control")
    daemon.store = StateStore(tmp_path / "state.sqlite3")
    plan = shop_plan()
    for stage, raid in plan["required"]:
        daemon.store.confirm_stage(StageConfirmation("a", "event", stage, raid), now=NOW)
    daemon.store.record_shop(
        "a",
        plan,
        {
            "activity_id": "event",
            "fingerprint": plan["fingerprint"],
            "outcome": "completed",
            "reason": "unlimited_commodity",
            "evidence": ["proof"],
            "observed_at": NOW,
        },
        attempt_id="one",
    )
    calls = []

    async def snapshot(self, settings):
        return activity()

    async def info(settings, profile):
        return SimpleNamespace(
            cultivate={
                "items": [{"id": item, "count": 100} for item in ("31113", "31083", "31043")],
                "characters": [],
            },
            player_info={"charInfoMap": {}},
        ), profile.name

    def load(cache):
        calls.append(cache)
        return shop_catalog()

    from tests.helpers.activity_shop import navigation_assets

    monkeypatch.setattr(
        "maa_planner.daemon.tools.activity_sprite_navigation.prepare_navigation_assets",
        lambda *args: navigation_assets(),
    )
    monkeypatch.setattr(
        daemon.observations, "activity_snapshot", lambda settings: snapshot(daemon, settings)
    )
    monkeypatch.setattr("maa_planner.daemon.planning.skland.collect_character_info", info)
    monkeypatch.setattr("maa_planner.daemon.tools.stage_catalog.load_stage_catalog", load)
    monkeypatch.setattr("maa_planner.daemon.planning.auto_copilot.collect_character_info", info)
    monkeypatch.setattr("maa_planner.daemon.observations.time.time", lambda: NOW)
    result = asyncio.run(
        daemon.observations.execute("plan", settings, threading.Event(), {"schedule_id": "daily"})
    )
    assert result["activity_shop_error"] is None
    assert result["activity_copilot_error"] is None
    assert result["activity_shops"][0]["garbage"]
    assert result["activity_shops"][0]["eligibility"] == "ready"
    assert result["activity_shops"][0]["missing"] == []
    assert result["farming"][0]["stage"] == "PR-A-1"
    assert len(calls) == 1
    assert not daemon.running
    daemon.store.close()

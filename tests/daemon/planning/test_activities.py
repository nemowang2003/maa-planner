import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from maa_planner.daemon.config import Settings
from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.observations import ObservationService
from maa_planner.daemon.operations import json_value
from maa_planner.daemon.planning.activity import (
    ActivityData,
    ActivitySnapshot,
    RecommendedStage,
    SideStory,
)


def test_public_activity_command_needs_no_account_config(capsys, monkeypatch):
    now = datetime.now(UTC)
    story = SideStory(
        key="TEST",
        name="测试活动",
        tip="活动",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=1),
        timezone_offset=8,
        minimum_required="v6.17.0",
        stages=(RecommendedStage(display="TEST-4", value="TEST-4", drop="搓玉效率0.69"),),
    )
    snapshot = ActivitySnapshot(
        data=ActivityData(side_stories=(story,), resource_collection=None, mini_games=()),
        source_url="https://api.maa.plus/example",
        fetched_at=now,
        expires_at=now + timedelta(minutes=5),
        checked_at=now,
    )
    coordinator = Daemon(Path("unused"), Path("unused.sock"))
    from tests.helpers.clearance import catalog

    monkeypatch.setattr(
        "maa_planner.daemon.tools.stage_catalog.load_stage_catalog", lambda cache: catalog()[1]
    )
    with patch.object(coordinator, "activity_snapshot", return_value=snapshot):
        api = ObservationService(
            store=lambda: None,
            activity_snapshot=coordinator.activity_snapshot,
            check_game_updates=None,
            observe_preparation=coordinator.observe_preparation,
            recovery_root=Path("unused"),
        )
        result = json_value(
            asyncio.run(api.execute("activities", Settings(Path("unused"), ()), threading.Event()))
        )
    assert result["side_stories"][0]["status"] == "active"
    assert result["side_stories"][0]["stages"][0]["drop"] == "搓玉效率0.69"
    assert result["side_stories"][0]["starts_at"].endswith("+08:00")
    assert result["resource_collection"] is None
    assert result["mini_games"] == []
    assert result["activity_regions_error"] is None


def test_regional_windows_report_closed_future_and_unknown_without_guessing():
    from maa_planner.daemon.planning.regions import activity_regions
    from tests.helpers.clearance import activity, catalog

    snapshot = activity()
    data = catalog()[1]
    data["activity_table"]["zoneToActivity"].update(ex="event", future="event", unknown="event")
    data["zone_table"]["zoneValidInfo"].update(
        ex={"startTs": 1900, "endTs": 2100}, future={"startTs": 2200, "endTs": 2300}
    )
    result = activity_regions(snapshot, data, now=2101)[0]
    assert [r["status"] for r in result["regions"]] == ["active", "ended", "upcoming", "unknown"]
    assert result["regions_known"] is False
    assert result["regions"][1]["ends_at"].timestamp() == 2101
    assert result["catalog_commit"] == data["commit"]

import json
from datetime import datetime, timedelta

import pytest

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.execution.runtime import NAV_FILES, navigation_files
from maa_planner.daemon.planning.activity import ActivityParseError, parse_activity_data
from maa_planner.daemon.tools import hot_update


def payload():
    window = {
        "UtcStartTime": "2026/11/02 11:00:00",
        "UtcExpireTime": "2026/11/03 03:59:59",
        "TimeZone": 8,
    }
    return {
        "Official": {
            "sideStoryStage": {
                "test": {
                    "Activity": dict(window, StageName="测试", Tip="活动"),
                    "Stages": [{"Display": "TEST-1", "Value": "TEST-1", "Drop": "搓玉效率0.69"}],
                }
            },
            "resourceCollection": None,
            "miniGame": [],
        }
    }


def test_one_hot_update_document_set_serves_planner_and_cli(tmp_path, monkeypatch):
    calls = []
    source = payload()

    def fetch(destination, *, api_url):
        calls.append(api_url)
        for name in NAV_FILES:
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(source if name == "gui/StageActivityV2.json" else {}))

    monkeypatch.setattr(hot_update, "fetch_navigation", fetch)
    first = hot_update.prepare_hot_update(tmp_path)
    assert len(calls) == 1
    story = first.activity.data.side_stories[0]
    assert story.starts_at.isoformat() == "2026-11-02T11:00:00+08:00"
    assert story.stages[0].drop == "搓玉效率0.69"
    assert json.loads(navigation_files(first.directory)["gui/StageActivityV2.json"]) == source
    second = hot_update.prepare_hot_update(tmp_path)
    assert len(calls) == 2  # A separate batch refreshes; no time-based freshness shortcut.
    assert second.directory == first.directory  # Equal contents reuse immutable files.
    assert second.activity.checked_at >= first.activity.checked_at
    (first.directory / "resource/tasks.json").write_text('{"corrupt":true}')
    with pytest.raises(ValueError, match="损坏"):
        hot_update.prepare_hot_update(tmp_path)


def test_activity_intervals_use_source_offset_and_shanghai_presentation():
    source = payload()
    source["Official"]["sideStoryStage"]["test"]["Activity"]["TimeZone"] = 9
    story = parse_activity_data(source).side_stories[0]
    assert story.starts_at.isoformat() == "2026-11-02T10:00:00+08:00"
    assert story.status(datetime.fromisoformat("2026-11-02T10:00:00+08:00")) == "active"
    assert story.status(story.ends_at) == "ended"
    with pytest.raises(ActivityParseError):
        parse_activity_data({"Official": {}})


def test_active_recommendations_do_not_require_exactly_three_materials(tmp_path, monkeypatch):
    now = datetime.now(BUSINESS_TIMEZONE)
    source = payload()
    stages = source["Official"]["sideStoryStage"]["test"]["Stages"]
    stages.append({"Display": "TEST-2", "Value": "TEST-2", "Drop": "30012"})
    source["Official"]["sideStoryStage"]["test"]["Activity"].update(
        UtcStartTime=(now - timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S"),
        UtcExpireTime=(now + timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S"),
    )

    def fetch(destination, *, api_url):
        for name in NAV_FILES:
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(source if name == "gui/StageActivityV2.json" else {}))

    monkeypatch.setattr(hot_update, "fetch_navigation", fetch)
    snapshot = hot_update.prepare_hot_update(tmp_path)
    assert len(snapshot.activity.data.side_stories[0].stages) == 2


def test_active_activity_source_error_is_classified_before_planning(tmp_path, monkeypatch):
    now = datetime.now(BUSINESS_TIMEZONE)
    source = payload()
    source["Official"]["sideStoryStage"]["test"]["Activity"].update(
        UtcStartTime=(now - timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S"),
        UtcExpireTime=(now + timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S"),
    )

    def fetch(destination, *, api_url):
        for name in NAV_FILES:
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(source if name == "gui/StageActivityV2.json" else {}))

    monkeypatch.setattr(hot_update, "fetch_navigation", fetch)
    with pytest.raises(hot_update.ActivitySourceError, match="推荐"):
        hot_update.prepare_hot_update(tmp_path)


def test_transient_hot_update_connection_error_retries_only_that_document(tmp_path, monkeypatch):
    import io
    import urllib.error

    from maa_planner.daemon.execution import runtime

    calls = []

    def fetch(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.URLError("TLS EOF")
        return io.BytesIO(b"{}")

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fetch)
    runtime.fetch_navigation(tmp_path / "snapshot", api_url="https://example.test")
    assert len(calls) == len(NAV_FILES) + 1
    assert calls[0] == calls[1]
    assert len(navigation_files(tmp_path / "snapshot")) == len(NAV_FILES)

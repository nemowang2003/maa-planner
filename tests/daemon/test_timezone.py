"""Business dates must not depend on the host timezone."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from maa_planner.daemon.planning.skland import json_default
from maa_planner.daemon.tools import game_update
from maa_planner.daemon.tools.apk import ApkInfo
from maa_planner.daemon.tools.resources import _resource_time


@pytest.mark.parametrize("host_timezone", ["UTC", "America/Los_Angeles", "Asia/Tokyo"])
def test_business_dates_ignore_host_timezone(monkeypatch, host_timezone):
    instant = datetime(2026, 11, 1, 17, tzinfo=UTC)

    class HostClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz or ZoneInfo(host_timezone))

    monkeypatch.setattr(game_update, "datetime", HostClock)
    monkeypatch.setattr(game_update, "resolve_latest", lambda *a, **kw: "https://test/app.apk")
    monkeypatch.setattr(
        game_update,
        "read_remote_apk",
        lambda *a, **kw: ApkInfo("com.hypergryph.arknights", 1, "1.0"),
    )
    latest = game_update.latest_apk(
        "https://test/latest", "com.hypergryph.arknights", allowed_host="test"
    )
    assert datetime.fromisoformat(latest.checked_at).utcoffset() == timedelta(hours=8)
    assert latest.checked_at == "2026-11-02T01:00:00+08:00"
    version = _resource_time({"last_updated": "2026-11-02 11:00:00.000"})
    assert version == datetime(2026, 11, 2, 3, tzinfo=UTC)
    assert json_default(instant) == "2026-11-02T01:00:00+08:00"


def test_api_dates_reject_unspecified_timezone():
    with pytest.raises(ValueError, match="无时区"):
        json_default(datetime(2026, 11, 2, 11))

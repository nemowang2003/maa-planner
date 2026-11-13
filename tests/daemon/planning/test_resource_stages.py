from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ResourceCollection
from maa_planner.daemon.planning.resource_stages import chip_calendar
from tests.helpers.activity import empty_activity


def stages(snapshot, when):
    return {s.stage: s for s in chip_calendar(snapshot, when)}


def test_game_weekday_changes_at_four_and_is_independent_of_host_timezone():
    snapshot = empty_activity()
    monday = datetime(2026, 11, 2, 4, tzinfo=BUSINESS_TIMEZONE)
    before = stages(snapshot, monday - timedelta(seconds=1))
    after = stages(snapshot, monday)
    assert before["PR-C-1"].open_by_calendar
    assert not after["PR-C-1"].open_by_calendar
    assert not before["PR-B-2"].open_by_calendar
    assert after["PR-B-2"].open_by_calendar
    assert chip_calendar(snapshot, monday) == chip_calendar(snapshot, monday.astimezone(UTC))
    assert after["PR-A-1"].item_ids == ("3261", "3231")
    assert after["PR-A-2"].item_ids == ("3262", "3232")


@pytest.mark.parametrize("delta,open_expected", [(-1, False), (0, True), (59, True), (60, False)])
def test_resource_event_overrides_only_its_active_window(delta, open_expected):
    snapshot = empty_activity()
    now = datetime(2026, 11, 4, 12, tzinfo=BUSINESS_TIMEZONE)
    event = ResourceCollection(
        starts_at=now,
        ends_at=now + timedelta(seconds=60),
        timezone_offset=8,
        tip="all resources",
        is_resource_collection=True,
    )
    snapshot = replace(snapshot, data=replace(snapshot.data, resource_collection=event))
    result = stages(snapshot, now + timedelta(seconds=delta))["PR-A-1"]
    assert result.open_by_calendar is open_expected
    assert result.reason == ("resource_collection" if open_expected else "weekly_calendar")


def test_naive_time_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        chip_calendar(empty_activity(), datetime(2026, 11, 4))

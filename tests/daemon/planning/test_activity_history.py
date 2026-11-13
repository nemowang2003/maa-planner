import copy
from dataclasses import replace
from datetime import datetime

import pytest

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import SideStory
from maa_planner.daemon.planning.activity_history import original_story, with_activity_history
from maa_planner.daemon.planning.annihilation import decide_annihilation
from tests.helpers.activity import empty_activity
from tests.helpers.activity_history import history


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=BUSINESS_TIMEZONE).timestamp()


@pytest.mark.parametrize(
    "start,end,days",
    [
        ("2025-08-02T12:00:00", "2025-08-23T03:59:59", 21),
        ("2025-05-01T12:00:00", "2025-05-22T03:59:59", 21),
        ("2025-06-05T12:00:00", "2025-06-19T03:59:59", 14),
        ("2025-08-02T16:00:00", "2025-08-23T04:00:00", 21),
    ],
)
def test_original_game_days_are_not_elapsed_hours_or_shop_duration(start, end, days):
    original = original_story(history(start=start, end=end), "墟")
    assert original is not None
    assert original.game_days == days
    assert original.activity_id == "act44side" and original.replica_id == "act44sre"
    assert original.ends_at.timestamp() == ts(end)


@pytest.mark.parametrize(
    "case",
    [
        "missing_map",
        "missing_records",
        "name_mismatch",
        "duplicate_name",
        "wrong_identity",
        "no_link",
        "multiple_links",
        "wrong_end",
        "bool_time",
        "wrong_type",
        "replicate_record",
    ],
)
def test_ambiguous_missing_and_malformed_history_is_not_guessed(case):
    player = history()
    if case == "missing_map":
        player.pop("activityInfoMap")
    elif case == "missing_records":
        player.pop("activity")
    elif case == "name_mismatch":
        player["activityInfoMap"]["act44side"]["name"] = "other"
    elif case == "duplicate_name":
        player["activityInfoMap"]["other"] = {
            **player["activityInfoMap"]["act44side"],
            "id": "other",
        }
    elif case == "wrong_identity":
        player["activityInfoMap"]["act44side"]["id"] = "other"
    elif case == "no_link":
        player["activity"][0]["actId"] = "other"
    elif case == "multiple_links":
        player["activity"].append({"actId": "act44side", "actReplicaId": "second"})
    elif case == "wrong_end":
        player["activityInfoMap"]["act44side"]["endTime"] = ts("2025-08-23T16:00:00")
    elif case == "bool_time":
        player["activityInfoMap"]["act44side"]["startTime"] = True
    elif case == "wrong_type":
        player["activityInfoMap"]["act44side"]["type"] = "MINI_ACTIVITY"
    elif case == "replicate_record":
        player["activityInfoMap"]["act44side"]["isReplicate"] = True
    assert original_story(player, "墟") is None


def test_current_reopen_duration_is_not_used_and_shared_snapshot_is_immutable():
    snapshot = empty_activity()
    reopen = SideStory(
        key="SSReopen",
        name="墟",
        tip="",
        stages=(),
        minimum_required=None,
        starts_at=datetime.fromtimestamp(ts("2026-11-01T04:00:00"), BUSINESS_TIMEZONE),
        ends_at=datetime.fromtimestamp(ts("2026-11-08T03:59:59"), BUSINESS_TIMEZONE),
        timezone_offset=8,
    )
    snapshot = replace(snapshot, data=replace(snapshot.data, side_stories=(reopen,)))
    player = history()
    before = copy.deepcopy(player)
    enriched = with_activity_history(snapshot, player)
    assert snapshot.data.side_stories[0].original is None
    assert player == before
    decision = decide_annihilation(now=ts("2026-11-02T12:10:00"), activity=enriched)
    assert not decision.run and decision.reopen_original_ids == ("act44side",)
    # Identical current rerun schedule, but an originally two-week event.
    other = with_activity_history(
        snapshot, history(start="2025-08-02T12:00:00", end="2025-08-16T03:59:59")
    )
    assert decide_annihilation(now=ts("2026-11-02T12:10:00"), activity=other).run

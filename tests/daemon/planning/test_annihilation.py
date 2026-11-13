from dataclasses import replace
from datetime import datetime

import pytest

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import OriginalSideStory, SideStory
from maa_planner.daemon.planning.annihilation import (
    WeeklyProgress,
    decide_annihilation,
    game_day,
    observe_weekly_progress,
    week_bounds,
)
from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.planning.schedule import parse_schedules
from tests.helpers.activity import empty_activity
from tests.helpers.planning import MONDAY


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=BUSINESS_TIMEZONE).timestamp()


def snapshot(now=MONDAY, *, key="SSReopen", start=None, end=None, original_days=21):
    data = empty_activity()
    story = SideStory(
        key=key,
        name="test",
        tip="",
        minimum_required=None,
        stages=(),
        starts_at=datetime.fromtimestamp(start or now - 1000, BUSINESS_TIMEZONE),
        ends_at=datetime.fromtimestamp(end or now + 86400, BUSINESS_TIMEZONE),
        timezone_offset=8,
        original=OriginalSideStory(
            activity_id="original",
            replica_id="replica",
            name="test",
            starts_at=datetime.fromtimestamp(now - 400 * 86400, BUSINESS_TIMEZONE),
            ends_at=datetime.fromtimestamp(now - 379 * 86400, BUSINESS_TIMEZONE),
            game_days=original_days,
        )
        if original_days is not None
        else None,
    )
    return replace(data, data=replace(data.data, side_stories=(story,)))


def test_week_and_retry_day_use_shanghai_four_hour_boundary():
    assert week_bounds(MONDAY) == (MONDAY, MONDAY + 7 * 86400)
    assert week_bounds(MONDAY - 1) == (MONDAY - 7 * 86400, MONDAY)
    assert game_day(ts("2026-11-03T03:59:59")) == MONDAY
    assert game_day(ts("2026-11-03T04:00:00")) == MONDAY + 86400


def test_current_week_completion_and_previous_week_unknown_are_distinct():
    player = {
        "status": {"storeTs": MONDAY - 1},
        "campaign": {"reward": {"current": 1800, "total": 1800}},
    }
    assert observe_weekly_progress(player, now=MONDAY + 100) is None
    player["status"]["storeTs"] = MONDAY
    observed = observe_weekly_progress(player, now=MONDAY + 5 * 86400)
    assert (
        observed is not None and observed.complete
    )  # No arbitrary age limit inside the same cycle.
    decision = decide_annihilation(now=MONDAY + 100, activity=empty_activity(), progress=observed)
    assert not decision.run
    assert decide_annihilation(now=MONDAY + 100, activity=empty_activity()).run


@pytest.mark.parametrize(
    "player",
    [
        {},
        {"status": None},
        {"status": {}, "campaign": []},
        {"status": {"storeTs": True}, "campaign": {"reward": {"current": 0, "total": 1800}}},
        {"status": {"storeTs": MONDAY}, "campaign": {"reward": {"current": True, "total": 1800}}},
        {"status": {"storeTs": MONDAY}, "campaign": {"reward": {"current": 0, "total": 0}}},
    ],
)
def test_missing_or_bad_progress_remains_unknown(player):
    assert observe_weekly_progress(player, now=MONDAY + 10) is None


def test_incomplete_progress_retries_each_batch_and_only_skland_completion_skips():
    for now in (MONDAY + 100, MONDAY + 8000, MONDAY + 86400):
        assert decide_annihilation(
            now=now, activity=empty_activity(), progress=WeeklyProgress(350, 1800, MONDAY)
        ).run
    assert not decide_annihilation(
        now=MONDAY + 86400, activity=empty_activity(), progress=WeeklyProgress(1800, 1800, MONDAY)
    ).run


def test_only_active_reopen_ending_this_week_defers_until_configured_opportunity():
    activity = snapshot(end=MONDAY + 3 * 86400)
    decision = decide_annihilation(now=MONDAY, activity=activity)
    assert not decision.run and decision.deferred_until == MONDAY + 3 * 86400
    assert decide_annihilation(now=MONDAY + 3 * 86400, activity=activity).run
    assert decide_annihilation(now=MONDAY, activity=snapshot(key="SS")).run
    assert decide_annihilation(now=MONDAY, activity=snapshot(end=MONDAY + 8 * 86400)).run
    assert decide_annihilation(now=MONDAY, activity=snapshot(start=MONDAY + 100)).run
    assert decide_annihilation(now=MONDAY, activity=snapshot(end=MONDAY + 7 * 86400)).run


def test_two_week_original_does_not_defer_and_unknown_history_is_explicit():
    assert decide_annihilation(now=MONDAY, activity=snapshot(original_days=14)).run
    unknown = decide_annihilation(now=MONDAY, activity=snapshot(original_days=None))
    assert unknown.run and unknown.history_unknown
    assert "缺少原活动历史" in unknown.reason


def test_no_early_fallback_and_inclusive_expiry_normalizes_to_four():
    activity = snapshot(end=ts("2026-11-08T03:59:59"))
    decision = decide_annihilation(now=ts("2026-11-07T23:40:00"), activity=activity)
    assert not decision.run
    assert decision.deferred_until == ts("2026-11-08T04:00:00")
    assert decision.deferred_until is not None
    assert decide_annihilation(now=decision.deferred_until, activity=activity).run
    # An inclusive expiry just before the next reset actually ends AT the reset.
    assert decide_annihilation(now=MONDAY, activity=snapshot(end=ts("2026-11-09T03:59:59"))).run


def test_preview_and_execution_share_weekly_selection_and_preserve_parameters(tmp_path):
    text = """[[schedules]]
id="daily"
profiles=["a"]
times=["08:00","20:00"]
[[schedules.tasks]]
name="weekly"
type="Fight"
strategy="expert"
params={stage="Annihilation",times=5,medicine=0,stone=0}
[[schedules.tasks]]
name="normal"
type="Fight"
params={stage="1-7",times=1}
"""
    import json
    import tomllib

    schedules = parse_schedules(tomllib.loads(text)["schedules"], {"a"}, tmp_path / "config.toml")
    schedule = schedules[0]
    plan = build_daily_plan(schedule, now=MONDAY, activity=empty_activity())
    assert plan.tasks is not None
    assert json.loads(plan.tasks.document)["tasks"][0]["params"] == {
        "stage": "Annihilation",
        "times": 5,
        "medicine": 0,
        "stone": 0,
    }
    skipped = build_daily_plan(
        schedule,
        now=MONDAY,
        activity=empty_activity(),
        weekly_progress=WeeklyProgress(1800, 1800, MONDAY),
    )
    assert skipped.tasks is not None
    assert [t["name"] for t in json.loads(skipped.tasks.document)["tasks"]] == ["normal"]

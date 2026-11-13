import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from maa_planner.daemon.planning.skland import summarize_activities
from maa_planner.daemon.state import StateStore
from maa_planner.schema import StageConfirmation
from tests.helpers.activity import empty_activity
from tests.helpers.clearance import activity
from tests.helpers.scheduling import Clock, eventually, running, wake


def snapshot(*, checked=0, end=3000, name="测试活动", start=1000, stale=False):
    value = activity()
    story = replace(
        value.data.side_stories[0],
        name=name,
        starts_at=value.data.side_stories[0].starts_at + timedelta(seconds=start - 1000),
        ends_at=value.data.side_stories[0].ends_at + timedelta(seconds=end - 3000),
    )
    return replace(
        value,
        checked_at=story.starts_at + timedelta(seconds=checked),
        stale=stale,
        data=replace(value.data, side_stories=(story,)),
    )


def events(store, kind):
    return [e["data"] for e in store.events(0) if e["kind"] == kind]


def test_deadline_survives_restart_and_missing_upstream_entry(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    store.observe_activities(snapshot(), now=2000)
    store.observe_activities(empty_activity(), now=2500)
    assert store.expire_activity_state(now=2999) == 3000
    assert not events(store, "activity_ended")
    store.close()
    store = StateStore(path)
    assert store.expire_activity_state(now=3000) is None
    assert events(store, "activity_ended")[0]["ends_at"] == 3000
    store.expire_activity_state(now=4000)
    assert len(events(store, "activity_ended")) == 1
    store.close()


def test_new_deadline_wins_over_older_or_stale_snapshot(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.observe_activities(snapshot(), now=2000)
    store.observe_activities(snapshot(checked=100, end=4000), now=2100)
    store.observe_activities(snapshot(checked=200, end=2200, stale=True), now=2150)
    store.observe_activities(snapshot(), now=2200)
    assert store.expire_activity_state(now=3000) == 4000
    assert not events(store, "activity_ended")
    store.expire_activity_state(now=4000)
    assert len(events(store, "activity_ended")) == 1
    store.close()


def test_newly_discovered_history_is_not_a_just_ended_activity(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.observe_activities(snapshot(), now=4000)
    store.expire_activity_state(now=4000)
    assert not events(store, "activity_ended")
    # The reusable MAA key SS can refer to a later event without hiding the old one.
    store.observe_activities(snapshot(name="下期活动", start=5000, end=7000), now=4500)
    assert store.expire_activity_state(now=6000) == 7000
    store.expire_activity_state(now=7000)
    assert events(store, "activity_ended")[0]["name"] == "下期活动"
    store.close()


@pytest.mark.parametrize("outcome", ["completed", "incomplete", "unknown"])
def test_shop_decision_state_expires_at_combat_end_for_all_accounts_but_keeps_evidence(
    tmp_path, outcome
):
    from tests.helpers.activity_shop import shop_plan

    store = StateStore(tmp_path / "state.sqlite3")
    plan = shop_plan()
    for profile in ("a", "b"):
        for stage, raid in plan["required"]:
            store.confirm_stage(StageConfirmation(profile, "event", stage, raid), now=2000)
        store.record_shop(
            profile,
            plan,
            {
                "activity_id": "event",
                "fingerprint": plan["fingerprint"],
                "observed_at": 2000,
                "outcome": outcome,
                "reason": "unlimited_commodity",
                "evidence": ["callbacks.jsonl:1"],
            },
            attempt_id=profile,
        )
    assert store.expire_activity_state(now=2999) == 3000
    assert store.has_shop_state("a")
    store.expire_activity_state(now=3000)
    assert not store.has_shop_state("a") and not store.has_shop_state("b")
    assert store.completed_stages("a", "event")
    assert len(events(store, "activity_shop_result")) == 2
    assert len(events(store, "activity_state_cleaned")) == 2
    store.close()


def test_unknown_legacy_expiry_is_not_deleted(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    with store.db:
        store.db.execute("INSERT INTO activity_shops VALUES ('a','old','fp','{}')")
    assert store.expire_activity_state(now=100000) is None
    assert store.has_shop_state("a")
    store.close()


def test_deadline_transition_and_event_are_atomic(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite3")
    store.observe_activities(snapshot(), now=2000)

    def reject(*args, **kwargs):
        raise OSError("rejected")

    monkeypatch.setattr(store.history, "event", reject)
    with pytest.raises(OSError, match="rejected"):
        store.expire_activity_state(now=3000)
    assert store.db.execute("SELECT ended_at FROM activity_windows").fetchone() == (None,)
    store.close()


def test_daemon_rechecks_remembered_deadline_on_next_start(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock("1970-01-01T08:30:00")
        async with running(tmp_path, monkeypatch, clock, content='[[profiles]]\nname="a"') as state:
            state.daemon.remember_activity(snapshot())
        clock.set("1970-01-01T09:00:00")
        async with running(tmp_path, monkeypatch, clock) as state:
            await eventually(lambda: bool(events(state.daemon.store, "activity_ended")))
            wake(state, clock, "1970-01-01T09:01:00")
            assert len(events(state.daemon.store, "activity_ended")) == 1

    asyncio.run(scenario())


def test_daemon_wakes_at_combat_deadline_without_another_game_run(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock("1970-01-01T08:30:00", realtime=True)
        async with running(tmp_path, monkeypatch, clock, content='[[profiles]]\nname="a"') as state:
            state.daemon.remember_activity(snapshot(end=clock.now() + 0.2))
            await eventually(lambda: bool(events(state.daemon.store, "activity_ended")))
            assert not state.workers

    asyncio.run(scenario())


def test_activity_summary_ends_at_combat_deadline(monkeypatch):
    from datetime import datetime

    from maa_planner.daemon.planning import skland

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(3000, tz)

    monkeypatch.setattr(skland, "datetime", ClockDateTime)
    result = summarize_activities(snapshot())
    assert not result["in_activity_period"]
    assert result["active_activity_keys"] == []
    assert result["side_stories"][0]["status"] == "ended"

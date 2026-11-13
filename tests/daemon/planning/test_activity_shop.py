import asyncio
import json
from dataclasses import replace

import pytest

from maa_planner.daemon.planning.activity_shop import (
    prepare_shop_context,
    prepare_shops,
    shop_eligibility,
)
from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.planning.schedule import parse_schedules
from maa_planner.daemon.state import StateStore
from maa_planner.schema import StageConfirmation
from tests.helpers.activity_shop import NOW, activity, configured, shop_catalog, shop_plan


def test_default_purchase_blacklist_and_material_threshold_match_confirmed_rules(tmp_path):
    schedule = parse_schedules(
        [
            {
                "id": "daily",
                "profiles": ["a"],
                "times": ["12:00"],
                "tasks": [{"type": "Fight", "strategy": "expert"}],
            }
        ],
        {"a"},
        tmp_path / "config.toml",
    )[0]
    assert schedule.shop_blacklist == ("模组增补仪", "模组增补条", "家具零件")
    assert schedule.garbage_material_threshold == 100


def test_manual_garbage_marker_cannot_bypass_material_and_shop_conditions(tmp_path):
    with pytest.raises(ValueError, match="自动双条件"):
        parse_schedules(
            [
                {
                    "id": "daily",
                    "profiles": ["a"],
                    "times": ["12:00"],
                    "tasks": [
                        {"type": "Fight", "strategy": "expert", "garbage_activities": ["SS"]}
                    ],
                }
            ],
            {"a"},
            tmp_path / "config.toml",
        )


def confirmed(plan):
    return {tuple(v) for v in plan["required"]}


def test_project_navigation_is_bound_to_game_activity_id_and_frozen_revision():
    from maa_planner.daemon.planning.navigation import NAVIGATION_REVISION
    from tests.helpers.activity_shop import navigation_assets

    assets = navigation_assets()
    (plan,) = prepare_shops(
        shop_catalog(), activity(), now=NOW, navigation={"event": assets}, blacklist=()
    )
    assets["entries"]["shop"].clear()
    assert plan["entry"][0]["template"] == "Planner@TestShop.png"
    assert plan["navigation_revision"] == NAVIGATION_REVISION


def test_story_ledger_is_not_required_but_battles_and_raids_remain_independent():
    plan = shop_plan()
    assert set(map(tuple, plan["required"])) == {
        ("act_01", False),
        ("act_01", True),
        ("act_02", False),
        ("act_02", True),
    }
    assert shop_eligibility(plan, confirmed(plan), NOW) == "ready"
    assert shop_eligibility(plan, confirmed(plan) - {("act_02", True)}, NOW) == "stages_incomplete"
    from maa_planner.daemon.planning.clearance import prepare_clearance
    from tests.helpers.clearance import catalog

    scripts, _ = catalog()
    clearance, _, _ = prepare_clearance(scripts, shop_catalog(), activity(), now=NOW)
    assert set(map(tuple, clearance["required"])) == set(map(tuple, plan["required"]))


def test_future_and_empty_regions_prevent_early_shop_even_with_complete_script_coverage():
    data = shop_catalog()
    data["activity_table"]["zoneToActivity"]["future"] = "event"
    data["zone_table"]["zoneValidInfo"]["future"] = {"startTs": 2500, "endTs": 2999}
    (plan,) = prepare_shops(data, activity(), now=NOW, navigation={}, blacklist=())
    assert not plan["catalog_complete"]
    assert shop_eligibility(plan, confirmed(plan), NOW) != "ready"
    assert shop_eligibility(plan, confirmed(plan), 2500) == "catalog_incomplete"
    data["stage_table"]["stages"]["future_01"] = {
        "zoneId": "future",
        "difficulty": "NORMAL",
        "hardStagedId": None,
    }
    (plan,) = prepare_shops(data, activity(), now=NOW, navigation={}, blacklist=())
    assert shop_eligibility(plan, confirmed(plan), NOW) == "regions_not_open"
    assert shop_eligibility(plan, confirmed(plan), 2500) == "ready"


def test_scope_and_blacklist_changes_invalidate_proof_but_unrelated_commit_does_not():
    data = shop_catalog()

    def scope(blacklist=("家具",)):
        return prepare_shops(data, activity(), now=NOW, navigation={}, blacklist=blacklist)[0][
            "fingerprint"
        ]

    first = scope()
    data["commit"] = "b" * 40
    assert scope() == first
    assert scope(()) != first
    data["stage_table"]["stages"]["new"] = {
        "zoneId": "zone",
        "difficulty": "NORMAL",
        "hardStagedId": None,
    }
    assert scope() != first


def test_unknown_zone_time_and_raid_metadata_are_not_completion():
    for field in ("zoneValidInfo",):
        data = shop_catalog()
        del data["zone_table"][field]
        (plan,) = prepare_shops(data, activity(), now=NOW, navigation={}, blacklist=())
        assert not plan["catalog_complete"]
    data = shop_catalog()
    del data["stage_table"]["stages"]["act_02#f#"]
    (plan,) = prepare_shops(data, activity(), now=NOW, navigation={}, blacklist=())
    assert not plan["catalog_complete"]


def test_stage_time_and_activity_expiry_are_checked():
    data = shop_catalog()
    data["stage_table"]["stageValidInfo"] = {"act_02": {"startTs": 2100, "endTs": -1}}
    (plan,) = prepare_shops(data, activity(), now=NOW, navigation={}, blacklist=())
    assert shop_eligibility(plan, confirmed(plan), NOW) == "regions_not_open"
    assert shop_eligibility(plan, confirmed(plan), 2100) == "ready"
    assert shop_eligibility(plan, confirmed(plan), 3000) == "activity_closed"


def test_shop_is_after_normal_fight_and_garbage_changes_only_future_stage_selection(tmp_path):
    plan = shop_plan()
    schedule = configured(tmp_path)
    daily = build_daily_plan(
        schedule,
        now=NOW,
        activity=activity(),
        depot={"31113": 1, "31083": 2, "31043": 3},
        shops=(plan,),
    )
    assert daily.tasks is not None
    doc = json.loads(daily.tasks.document)
    assert doc["activity_shops"][0]["after_task"] == 1
    assert "times" not in doc["tasks"][1]["params"]
    assert doc["tasks"][1]["params"]["series"] == 0
    assert all(k.startswith("Planner@") for k in doc["resource_tasks"])
    later = build_daily_plan(
        schedule, now=NOW, activity=activity(), depot={}, shops=({**plan, "garbage": True},)
    )
    assert later.tasks is not None
    assert "activity_shops" not in json.loads(later.tasks.document)


def test_persisted_completion_is_account_scoped_and_rechecked_against_current_scope(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    plan = shop_plan()
    for stage, raid in plan["required"]:
        store.confirm_stage(StageConfirmation("a", "event", stage, raid), now=NOW)
    report = {
        "activity_id": "event",
        "fingerprint": plan["fingerprint"],
        "outcome": "completed",
        "reason": "unlimited_commodity",
        "evidence": ["callbacks:1"],
        "observed_at": NOW,
    }
    store.record_shop("a", plan, report, attempt_id="one")
    store.close()
    store = StateStore(tmp_path / "state.sqlite3")

    async def load():
        return shop_catalog()

    async def context(profile):
        return await prepare_shop_context(
            configured(tmp_path),
            activity(),
            now=NOW,
            store=store,
            profile=profile,
            load_catalog=load,
            depot={"31113": 100, "31083": 100, "31043": 100},
        )

    assert asyncio.run(context("a"))[0]["garbage"]
    assert not asyncio.run(context("b"))[0]["garbage"]
    assert not store.shop_state("a", "rerun", plan["fingerprint"])
    assert not store.shop_state("a", "event", "different")
    store.close()


@pytest.mark.parametrize(
    "outcome,reason,evidence",
    [
        ("completed", "missing_evidence", ["x"]),
        ("completed", "unlimited_commodity", []),
        ("insufficient_currency", "insufficient_currency", ["x"]),
        ("unknown", "navigation_failed", []),
    ],
)
def test_native_unknown_and_insufficient_results_never_prove_garbage(
    tmp_path, outcome, reason, evidence
):
    store = StateStore(tmp_path / "state.sqlite3")
    plan = shop_plan()
    for stage, raid in plan["required"]:
        store.confirm_stage(StageConfirmation("a", "event", stage, raid), now=NOW)
    store.record_shop(
        "a",
        plan,
        {
            "activity_id": "event",
            "fingerprint": plan["fingerprint"],
            "outcome": outcome,
            "reason": reason,
            "evidence": evidence,
            "observed_at": NOW,
        },
        attempt_id="one",
    )
    assert not store.shop_state("a", "event", plan["fingerprint"])["completed"]
    store.close()


def test_positive_shop_report_without_full_local_progress_is_not_garbage(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    plan = shop_plan()
    store.record_shop(
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
    assert not store.shop_state("a", "event", plan["fingerprint"])["completed"]
    assert store.alerts("a")
    store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"shop_entries": []},
        {"shop_entries": {"SS": ""}},
        {"shop_blacklist": "家具"},
        {"shop_blacklist": ["家具", "家具"]},
        {"shop_blacklist": [""]},
        {"client_type": "YoStarEN"},
    ],
)
def test_invalid_shop_configuration_is_rejected(tmp_path, change):
    row = {
        "id": "daily",
        "profiles": ["a"],
        "times": ["12:00"],
        "tasks": [{"type": "Fight", "params": {"stage": "1-7"}}],
        **change,
    }
    with pytest.raises(ValueError):
        parse_schedules([row], {"a"}, tmp_path / "config.toml")


@pytest.mark.parametrize(
    "drops,depot,shop_done,materials_complete,garbage",
    [
        (
            ("31113", "31083", "31043"),
            {"31113": 99, "31083": 100, "31043": 100},
            True,
            False,
            False,
        ),
        (("31113", "31083", "31043"), {"31113": 100, "31083": 100, "31043": 100}, True, True, True),
        (
            ("31113", "31083", "31043"),
            {"31113": 100, "31083": 100, "31043": 100},
            False,
            True,
            False,
        ),
        (("30073", "31083", "31043"), {"31083": 100, "31043": 100}, True, True, True),
        (("30073", "31033", "31053"), {}, True, True, True),
        (("31113", "31083", "31043"), None, True, False, False),
    ],
)
def test_garbage_requires_materials_and_shop_with_blacklist_exemptions(
    tmp_path, drops, depot, shop_done, materials_complete, garbage
):
    store = StateStore(tmp_path / "state.sqlite3")
    plan = shop_plan()
    for stage, raid in plan["required"]:
        store.confirm_stage(StageConfirmation("a", "event", stage, raid), now=NOW)
    if shop_done:
        store.record_shop(
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
    snapshot = activity()
    story = snapshot.data.side_stories[0]
    recommendations = (
        tuple(replace(stage, drop=drop) for stage, drop in zip(story.stages[:3], drops))
        + story.stages[3:]
    )
    snapshot = replace(
        snapshot,
        data=replace(snapshot.data, side_stories=(replace(story, stages=recommendations),)),
    )

    async def load():
        return shop_catalog()

    contexts = asyncio.run(
        prepare_shop_context(
            configured(tmp_path),
            snapshot,
            now=NOW,
            store=store,
            profile="a",
            load_catalog=load,
            depot=depot,
        )
    )
    selected = contexts[0]
    assert selected["shop_completed"] is shop_done
    assert selected["materials_complete"] is materials_complete
    assert selected["garbage"] is garbage
    if drops[0] == "30073":
        assert selected["materials"][0]["target"] == 0
        assert selected["materials"][0]["exempt"]
    store.close()


def test_garbage_uses_all_recommended_materials_without_fixed_count(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    plan = shop_plan()
    for stage, raid in plan["required"]:
        store.confirm_stage(StageConfirmation("a", "event", stage, raid), now=NOW)
    store.record_shop(
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
    snapshot = activity()
    story = snapshot.data.side_stories[0]
    extra = replace(story.stages[2], value="EV-6", drop="31063")
    snapshot = replace(
        snapshot,
        data=replace(snapshot.data, side_stories=(replace(story, stages=story.stages + (extra,)),)),
    )

    async def load():
        return shop_catalog()

    (selected,) = asyncio.run(
        prepare_shop_context(
            configured(tmp_path),
            snapshot,
            now=NOW,
            store=store,
            profile="a",
            load_catalog=load,
            depot={"31113": 100, "31083": 100, "31043": 100, "31063": 0},
        )
    )
    assert [v["item_id"] for v in selected["materials"]].count("31063") == 1
    assert selected["materials_complete"] is False
    assert selected["garbage"] is False
    store.close()

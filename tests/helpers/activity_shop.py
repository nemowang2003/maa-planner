from dataclasses import replace

from maa_planner.daemon.planning.activity import RecommendedStage
from maa_planner.daemon.planning.activity_shop import prepare_shops
from maa_planner.daemon.planning.schedule import parse_schedules
from tests.helpers.clearance import NOW, catalog
from tests.helpers.clearance import activity as clearance_activity


def activity():
    snapshot = clearance_activity()
    story = replace(
        snapshot.data.side_stories[0],
        stages=tuple(
            RecommendedStage(display=code, value=code, drop=drop)
            for code, drop in (
                ("EV-9", "31113"),
                ("EV-8", "31083"),
                ("EV-7", "31043"),
                ("EV-4", "搓玉效率0.69"),
            )
        ),
    )
    return replace(snapshot, data=replace(snapshot.data, side_stories=(story,)))


def shop_catalog():
    _, data = catalog()
    data["zone_table"]["zoneValidInfo"] = {"zone": {"startTs": 1000, "endTs": 2999}}
    data["activity_table"]["missionData"] = [
        {"id": "mission_1", "missionGroup": "event", "description": "通关EV-EX-1"},
    ]
    stages = data["stage_table"]["stages"]
    stages["act_st"] = {
        "stageId": "act_st",
        "isStoryOnly": True,
        "unlockCondition": [{"stageId": "act_01", "completeState": "PASS"}],
        "zoneId": "zone",
        "difficulty": "NORMAL",
        "hardStagedId": None,
        "levelId": None,
        "code": "EV-ST-1",
    }
    stages["act_02"]["unlockCondition"] = [{"stageId": "act_st", "completeState": "PASS"}]
    return data


def navigation_assets(*, missions=False):
    from maa_planner.daemon.execution.png import encode_png
    from tests.helpers.png import PNG

    def entry(name):
        return {"template": name, "rectMove": [0, 0, 1, 1], "size": [1, 1], "source": name}

    return {
        "entries": {
            "shop": [entry("Planner@TestShop.png")],
            "missions": [entry("Planner@TestMissions.png")] if missions else [],
        },
        "shop_ready": ["Planner@TestShopReady.png"],
        "images": {
            name: encode_png(PNG)
            for name in (
                "Planner@TestShop.png",
                "Planner@TestMissions.png",
                "Planner@TestShopReady.png",
            )
        },
        "source": {"revision": "test"},
    }


def shop_plan():
    (plan,) = prepare_shops(
        shop_catalog(),
        activity(),
        now=NOW,
        navigation={"event": navigation_assets()},
        blacklist=("家具",),
    )
    return {**plan, "after_task": 0, "navigation": "Planner@Shop0"}


def configured(tmp_path):
    return parse_schedules(
        [
            {
                "id": "daily",
                "profiles": ["a"],
                "times": ["12:00"],
                "shop_blacklist": ["家具"],
                "tasks": [
                    {"type": "Fight", "params": {"stage": "Annihilation", "times": 1}},
                    {"type": "Fight", "strategy": "expert", "params": {"times": 30}},
                    {"type": "Award"},
                ],
            }
        ],
        {"a"},
        tmp_path / "config.toml",
    )[0]

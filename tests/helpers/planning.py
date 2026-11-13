from dataclasses import replace
from datetime import datetime, timedelta

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import RecommendedStage, SideStory
from tests.helpers.activity import empty_activity

NOW = datetime(2026, 11, 2, 12, 10, tzinfo=BUSINESS_TIMEZONE).timestamp()


def skland_base(mode="orundum", ingredient="30012"):
    products = {
        "normal": ["gold", "gold", "exp", "exp"],
        "three_gold": ["gold", "gold", "gold", "exp"],
        "sell_only": ["gold", "gold", "exp", "exp"],
        "orundum": ["gold", "gold", "exp", "shard"],
    }
    return {
        "status": {"storeTs": NOW - 60},
        "building": {
            "manufactures": [{"formulaId": formula} for formula in products[mode]],
            "tradings": [
                {"strategy": "O_GOLD"},
                {"strategy": "O_DIAMOND" if mode in ("orundum", "sell_only") else "O_GOLD"},
            ],
        },
        "manufactureFormulaInfoMap": {
            "gold": {"itemId": "3003", "costs": []},
            "exp": {"itemId": "2003", "costs": []},
            "shard": {"itemId": "3141", "costs": [{"id": ingredient}, {"id": "4001"}]},
        },
    }


def warehouse():
    return {
        "items": [
            {"id": key, "count": str(value)}
            for key, value in {
                "30012": 150,
                "30062": 1000,
                "3003": 499,
                "3141": 101,
                "4001": 100000,
            }.items()
        ]
    }


def farming_activity(drops=("31113", "31083", "31043")):
    start = datetime.fromtimestamp(NOW, BUSINESS_TIMEZONE)
    story = SideStory(
        key="TL",
        name="时间啊，向前！",
        tip="SideStory",
        starts_at=start,
        ends_at=start + timedelta(days=21),
        timezone_offset=8,
        minimum_required="v6.21.1",
        stages=tuple(
            RecommendedStage(display=code, value=code, drop=drop)
            for code, drop in zip(("TL-9", "TL-8", "TL-7", "TL-4"), (*drops, "搓玉效率0.69"))
        ),
    )
    snapshot = empty_activity()
    return replace(snapshot, data=replace(snapshot.data, side_stories=(story,)))


MONDAY = datetime(2026, 11, 2, 4, tzinfo=BUSINESS_TIMEZONE).timestamp()

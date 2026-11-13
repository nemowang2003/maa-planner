from dataclasses import replace
from datetime import datetime

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ActivityData, SideStory
from maa_planner.daemon.planning.clearance import prepare_clearance
from tests.helpers.activity import empty_activity

NOW = 2000.0


def catalog():
    stages = {}
    scripts = []
    for index in (1, 2):
        stage = f"act_0{index}"
        code = f"EV-EX-{index}"
        base = {
            "stageId": stage,
            "levelId": f"activities/act/level_{stage}",
            "zoneId": "zone",
            "code": code,
            "difficulty": "NORMAL",
            "hardStagedId": stage + "#f#",
            "unlockCondition": (
                [] if index == 1 else [{"stageId": "act_01", "completeState": "PASS"}]
            ),
        }
        stages[stage] = base
        stages[stage + "#f#"] = {
            **base,
            "stageId": stage + "#f#",
            "difficulty": "FOUR_STAR",
            "hardStagedId": None,
            "unlockCondition": [{"stageId": stage, "completeState": "COMPLETE"}],
        }
        scripts.append((index, {"stage_name": stage, "difficulty": 3, "actions": [], "opers": []}))
    return tuple(scripts), {
        "commit": "a" * 40,
        "stage_table": {"stages": stages},
        "zone_table": {
            "zones": {"zone": {"zoneNameSecond": "区域"}},
            "zoneValidInfo": {"zone": {"startTs": 1000, "endTs": 2999}},
        },
        "activity_table": {
            "zoneToActivity": {"zone": "event"},
            "basicInfo": {
                "event": {"id": "event", "name": "测试活动", "startTime": 1000, "endTime": 3000}
            },
        },
    }


def activity():
    return replace(
        empty_activity(),
        data=ActivityData(
            side_stories=(
                SideStory(
                    key="SS",
                    name="测试活动",
                    tip="",
                    stages=(),
                    minimum_required=None,
                    starts_at=datetime.fromtimestamp(1000, BUSINESS_TIMEZONE),
                    ends_at=datetime.fromtimestamp(3000, BUSINESS_TIMEZONE),
                    timezone_offset=8,
                ),
            ),
            mini_games=(),
            resource_collection=None,
        ),
    )


def clearance():
    scripts, data = catalog()
    return prepare_clearance(scripts, data, activity(), now=NOW)


async def no_clearance_needed(*args, **kwargs):
    """Isolate other daily policies from the automatic clearance collaborator."""
    return None

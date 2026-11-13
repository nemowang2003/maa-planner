from tests.helpers.clearance import catalog


def story_catalog():
    scripts, data = catalog()
    stages = data["stage_table"]["stages"]
    for name, parent in (("middle", "act_01"), ("tail", "act_02"), ("tail2", "tail")):
        stages[name] = {
            "stageId": name,
            "code": "EV-ST-" + {"middle": "1", "tail": "2", "tail2": "3"}[name],
            "zoneId": "zone",
            "difficulty": "NORMAL",
            "hardStagedId": None,
            "levelId": None,
            "isStoryOnly": True,
            "unlockCondition": [{"stageId": parent, "completeState": "PASS"}],
        }
    stages["act_02"]["unlockCondition"] = [{"stageId": "middle", "completeState": "PASS"}]
    stages["later"] = {
        **stages["act_01"],
        "stageId": "later",
        "code": "EV-S-1",
        "zoneId": "later_zone",
        "hardStagedId": None,
        "unlockCondition": [{"stageId": "tail2", "completeState": "PASS"}],
    }
    data["activity_table"]["zoneToActivity"]["later_zone"] = "event"
    data["zone_table"]["zones"]["later_zone"] = {"zoneNameSecond": "后续区域"}
    data["zone_table"]["zoneValidInfo"]["later_zone"] = {"startTs": 1000, "endTs": 2999}
    return scripts, data

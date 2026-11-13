import pytest

from maa_planner.daemon.planning.copilot import script_modes, stage_definitions, targets


@pytest.mark.parametrize(
    "value,expected", [(0, (False,)), (1, (False,)), (2, (True,)), (3, (False, True))]
)
def test_script_modes_follow_documented_difficulty(value, expected):
    assert script_modes({"difficulty": value}) == expected
    assert script_modes({}) == (False,)


@pytest.mark.parametrize("value", [None, True, False, "3", -1, 4])
def test_invalid_script_difficulty_is_not_guessed(value):
    with pytest.raises(ValueError, match="difficulty"):
        script_modes({"difficulty": value})


def definitions():
    stages = {}
    for identifier, code, raid, prerequisites in [
        ("act52side_ex01", "YW-EX-1", "act52side_ex01#f#", []),
        ("act52side_ex01#f#", "YW-EX-1", None, ["act52side_ex01"]),
        ("act52side_ex02", "YW-EX-2", None, ["act52side_ex01"]),
        ("act55side_01", "TL-1", None, []),
    ]:
        stages[identifier] = {
            "stageId": identifier,
            "code": code,
            "zoneId": "zone",
            "difficulty": "FOUR_STAR" if "#f#" in identifier else "NORMAL",
            "hardStagedId": raid,
            "unlockCondition": [{"stageId": p, "completeState": "COMPLETE"} for p in prerequisites],
        }
    return {"stages": stages}


def test_real_106881_metadata_expands_both_modes_and_orders_prerequisites():
    scripts = (
        (2, {"stage_name": "act52side_ex02", "difficulty": 1}),
        (106881, {"stage_name": "act52side_ex01", "difficulty": 3}),
        (3, {"stage_name": "act52side_ex01", "difficulty": 1}),
    )
    output = targets(scripts, stage_definitions(definitions()))
    assert [(s.script_id, s.stage_id, s.is_raid) for s in output] == [
        (106881, "act52side_ex01", False),
        (106881, "act52side_ex01", True),
        (2, "act52side_ex02", False),
    ]


def test_script_suitability_does_not_create_nonexistent_raid():
    catalog = stage_definitions(definitions())
    for script in ({"stage_name": "act55side_01"}, {"stage_name": "act55side_01", "difficulty": 3}):
        output = targets(((10, script),), catalog)
        assert len(output) == 1 and output[0].is_raid is False


def test_missing_or_contradictory_raid_relationship_is_unknown():
    document = definitions()
    document["stages"].pop("act52side_ex01#f#")
    with pytest.raises(ValueError, match="突袭关系"):
        stage_definitions(document)


def test_unlock_cycle_and_unknown_script_stage_are_rejected():
    document = definitions()
    document["stages"]["act52side_ex01"]["unlockCondition"] = [
        {"stageId": "act52side_ex02", "completeState": "PASS"}
    ]
    catalog = stage_definitions(document)
    with pytest.raises(ValueError, match="依赖存在环"):
        targets(
            ((1, {"stage_name": "act52side_ex01"}), (2, {"stage_name": "act52side_ex02"})), catalog
        )
    with pytest.raises(ValueError, match="没有"):
        targets(((1, {"stage_name": "unknown"}),), catalog)

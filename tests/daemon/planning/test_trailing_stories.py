from maa_planner.daemon.planning.activity_scope import activity_scope
from tests.helpers.stories import story_catalog


def test_only_regional_trailing_stories_are_explicit_completion_targets():
    _, data = story_catalog()
    required = {tuple(v) for v in activity_scope(data, "event")["required"]}
    assert ("middle", False) not in required
    assert ("tail", False) in required
    assert ("tail2", False) in required  # Cross-region successor must not hide this tail.


def test_story_targets_follow_their_region_and_precede_dependent_region():
    from maa_planner.daemon.planning.clearance import prepare_clearance
    from maa_planner.daemon.planning.tutorials import add_tutorial_barriers
    from tests.helpers.clearance import NOW, activity

    scripts, data = story_catalog()
    scripts = (*scripts, (3, {"stage_name": "later", "actions": [], "opers": []}))
    prepared = prepare_clearance(scripts, data, activity(), now=NOW)
    plan, _, nodes = add_tutorial_barriers(prepared, data, now=NOW)
    assert [e["stage_id"] for e in plan["targets"]] == [
        "act_01",
        "act_01",
        "act_02",
        "act_02",
        "tail",
        "tail2",
        "later",
    ]
    stories = [e for e in plan["targets"] if e.get("kind") == "tail_story"]
    assert all("filename" not in e and e["script_id"] == 0 for e in stories)
    assert stories[0]["prerequisites"] == [["act_02", False]]
    assert stories[1]["prerequisites"] == [["tail", False]]
    assert any(key.endswith("@PlotSkipped") for key in nodes)


def test_story_panel_uses_centered_plot_title_not_battle_sidebar():
    from maa_planner.daemon.planning.stories import story_nodes

    panel = story_nodes("Planner@Story", "TL-ST-4")["Planner@Story@PlotPanel"]
    x, y, width, height = panel["roi"]
    # Native TL-ST-4 plot dialog title, normalized from 1920x1080 to Core's 1280x720.
    assert x <= 293 and x + width >= 440
    assert y <= 323 and y + height >= 350
    assert panel["text"] == ["TL-ST-4"] and panel["action"] == "DoNothing"

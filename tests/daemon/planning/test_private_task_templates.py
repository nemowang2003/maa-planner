import pytest

from maa_planner.daemon.execution.annihilation_tasks import delegation_task
from maa_planner.daemon.planning.activity_shop import shop_navigation
from maa_planner.daemon.planning.clearance import navigation_nodes
from tests.helpers.activity_shop import shop_plan


@pytest.mark.parametrize(
    "kind",
    [
        "shop",
        "clearance",
        "Annihilation",
        "Chernobog@Annihilation",
        "LungmenOutskirts@Annihilation",
        "LungmenDowntown@Annihilation",
    ],
)
def test_generated_derived_match_nodes_never_fall_back_to_private_named_pngs(kind):
    if kind == "shop":
        nodes = shop_navigation(shop_plan(), "Planner@Shop0")
    elif kind == "clearance":
        nodes = navigation_nodes("Planner@Clear0", "测试活动", "普通", ["EV-1"])
    else:
        _, nodes = delegation_task({"type": "Fight", "params": {"stage": kind}}, 0)
    non_matching_bases = {
        "SS-OpenOcr",
        "ClickStageName",
        "StartButton1",
        "CloseStonePage",
        "ClickCorner",
        "FightMissionFailed",
        "AnnihilationSwitchButton",
    }
    for name, node in nodes.items():
        base = node.get("baseTask")
        if base is None or base in non_matching_bases or base.endswith("@AnnihilationStage"):
            continue
        # Core's documented baseTask rule uses <new task name>.png if absent.
        assert "template" in node, f"{name} would require an unshipped private PNG"
        templates = node["template"] if isinstance(node["template"], list) else [node["template"]]
        assert templates and all(not value.startswith("Planner@") for value in templates)


def test_shop_uses_theme_back_terminal_and_page_identity_independent_of_stock():
    nodes = shop_navigation(shop_plan(), "Planner@Shop0")
    assert nodes["Planner@Shop0@Home"]["template"] == [
        "SwitchTheme@ToggleSettingsMenu.png",
        "SwitchTheme@ToggleSettingsMenu-v2.png",
    ]
    assert nodes["Planner@Shop0@Back"]["template"] == ["Return.png", "Return-White.png"]
    assert nodes["Planner@Shop0@TerminalReady"]["template"] == "StageResource.png"
    ready = nodes["Planner@Shop0@ShopReady"]
    # The real TL shop's first two columns are sold out. A purchasable-price
    # template in that ROI cannot establish whether the shop page is open.
    assert ready["algorithm"] == "MatchTemplate"
    assert ready["template"] == shop_plan()["shop_ready"]
    assert ready["roi"] == [0, 0, 1280, 720]
    assert "baseTask" not in ready
    assert nodes["Planner@Shop0@ShopReady"]["action"] == "DoNothing"
    assert nodes["Planner@Shop0@ShopReady"]["next"] == []


def test_activity_and_region_navigation_use_data_labels_without_tl_specific_core_tasks():
    labels = ["时间啊，向前！", "残照终尽", "长冬初青", "世纪之春"]
    nodes = navigation_nodes("Planner@TL", labels[0], labels[1], ["TL-9"], activity_labels=labels)
    entry = nodes["Planner@TL@Activity"]
    assert entry["baseTask"] == "SS-OpenOcr"
    assert entry["text"] == labels
    assert entry["roi"] == [0, 0, 1280, 720]
    normal = nodes["Planner@TL@Region"]
    assert normal["algorithm"] == "OcrDetect" and normal["text"] == [labels[1]]
    assert "baseTask" not in normal and "template" not in normal
    shop = shop_navigation(
        {**shop_plan(), "name": labels[0], "activity_labels": labels}, "Planner@Shop0"
    )
    assert shop["Planner@Shop0@Open"]["baseTask"] == "SS-OpenOcr"
    assert shop["Planner@Shop0@Open"]["text"] == labels


@pytest.mark.parametrize("kind", ["shop", "clearance"])
def test_terminal_checkpoint_covers_resource_tab_shifted_left_of_core_default_roi(kind):
    nodes = (
        shop_navigation(shop_plan(), "Planner@Test")
        if kind == "shop"
        else navigation_nodes("Planner@Test", "时间啊，向前！", "长冬初青", ["TL-EX-1"])
    )
    checkpoint = nodes["Planner@Test@TerminalReady"]
    x, y, width, height = checkpoint["roi"]
    # The 2026-11-09 terminal has its resource tab at x=439..470;
    # Core's default x=462 clips most of the template.
    assert x <= 439 and x + width >= 470
    assert y <= 653 and y + height >= 688
    assert checkpoint["template"] == "StageResource.png"
    assert checkpoint["action"] == "DoNothing" and checkpoint["next"] == []


@pytest.mark.parametrize("kind", ["shop", "clearance", "annihilation"])
def test_home_recovery_can_dismiss_battle_results_without_starting_another_battle(kind):
    if kind == "shop":
        nodes = shop_navigation(shop_plan(), "Planner@Test")
        prefix = "Planner@Test@"
    elif kind == "clearance":
        nodes = navigation_nodes("Planner@Test", "活动", "区域", ["EV-1"])
        prefix = "Planner@Test@"
    else:
        _, nodes = delegation_task({"type": "Fight", "params": {"stage": "Annihilation"}}, 0)
        prefix = "Planner@Annihilation0@"
    # Limit this walk to the return segment; terminal/delegation has its own
    # guarded entry. Recovering a result can only dismiss it or find Home.
    visited, todo = set(), [prefix + "ToHome"]
    while todo:
        name = todo.pop()
        if name in visited:
            continue
        visited.add(name)
        node = nodes[name]
        assert node.get("baseTask") not in {"StartButton1", "StartButton2", "UsePrts", "FightBegin"}
        for key in ("next", "onErrorNext", "exceededNext"):
            todo.extend(target for target in node.get(key, []) if not target.endswith("@Terminal"))
    assert prefix + "RecoverDefeat" in visited and prefix + "DismissSettlement" in visited
    assert nodes[prefix + "RecoverDefeat"]["baseTask"] == "FightMissionFailed"
    assert nodes[prefix + "Back"]["template"] == ["Return.png", "Return-White.png"]


def test_entry_uses_masked_template_and_relative_click_instead_of_title_or_absolute_point():
    plan = shop_plan()
    nodes = shop_navigation(plan, "Planner@Shop0")
    entry = nodes["Planner@Shop0@Entry"]
    assert entry["algorithm"] == "MatchTemplate"
    assert entry["maskRange"] == [1, 255]
    assert entry["template"] == plan["entry"][0]["template"]
    assert entry["rectMove"] == plan["entry"][0]["rectMove"]
    assert "specificRect" not in entry and "text" not in entry

import pytest

from maa_planner.daemon.planning.processing import material_recipes, next_recipe


def catalog():
    return {
        "commit": "a" * 40,
        "item_table": {
            "items": {
                "p": {"name": "白马醇", "rarity": "TIER_4", "itemType": "MATERIAL"},
                "o": {"name": "聚合剂", "rarity": "TIER_5", "itemType": "MATERIAL"},
                "book": {"name": "技巧概要·卷3", "rarity": "TIER_4", "itemType": "MATERIAL"},
            }
        },
        "building_data": {
            "workshopFormulas": {
                "p": {
                    "formulaId": "p",
                    "sortId": 2,
                    "itemId": "p",
                    "count": 1,
                    "goldCost": 300,
                    "formulaType": "F_EVOLVE",
                    "costs": [{"id": "base", "count": 2}],
                },
                "o": {
                    "formulaId": "o",
                    "sortId": 1,
                    "itemId": "o",
                    "count": 1,
                    "goldCost": 400,
                    "formulaType": "F_EVOLVE",
                    "costs": [{"id": "p", "count": 2}],
                },
                "book": {"formulaType": "F_SKILL", "itemId": "book"},
            }
        },
    }


def test_targets_cover_evolution_materials_only_with_explicit_rarity_goals():
    recipes = material_recipes(catalog(), {"T3": 20, "T4": 10})
    assert [(r["item_id"], r["target"]) for r in recipes] == [("o", 10), ("p", 20)]
    assert recipes[0]["costs"] == {"p": 2, "4001": 400}


def test_recipe_selection_never_guesses_unknown_counts_or_recurses_for_ingredients():
    recipes = material_recipes(catalog(), {"T3": 20, "T4": 10})
    assert next_recipe(recipes, {"p": 20, "o": 10, "base": 100, "4001": 10000}) is None
    assert next_recipe(recipes, {"p": 20, "base": 100, "4001": 10000}) is None
    assert next_recipe(recipes, {"p": 0, "o": 0, "base": 0, "4001": 10000}) is None
    assert next_recipe(recipes, {"p": 0, "o": 0, "base": 2, "4001": 10000})["item_id"] == "p"


def test_invalid_recipe_costs_are_rejected_before_game_control():
    data = catalog()
    data["building_data"]["workshopFormulas"]["o"]["costs"][0]["count"] = -1
    with pytest.raises(ValueError, match="配方"):
        material_recipes(data, {"T3": 20, "T4": 10})


def test_named_goal_overrides_group_even_for_zero_and_omitted_groups_do_not_apply():
    recipes = material_recipes(catalog(), {"T3": 20, "白马醇": 0, "聚合剂": 7})
    assert {r["name"]: r["target"] for r in recipes} == {"白马醇": 0, "聚合剂": 7}
    assert [r["name"] for r in material_recipes(catalog(), {"白马醇": 6})] == ["白马醇"]
    with pytest.raises(ValueError, match="未知"):
        material_recipes(catalog(), {"T3": 20, "白马淳": 30})


def test_operator_strategy_expands_groups_then_applies_blacklist_regardless_of_order():
    from maa_planner.daemon.planning.processing import allowed_items

    recipes = [
        {"item_id": "a", "name": "聚合剂", "tier": "T4"},
        {"item_id": "b", "name": "白马醇", "tier": "T3"},
        {"item_id": "c", "name": "炽合金块", "tier": "T3"},
    ]
    assert allowed_items(recipes, ["聚合剂", "T3", "-炽合金块"]) == ["a", "b"]
    assert allowed_items(recipes, ["-炽合金块", "T3", "聚合剂"]) == ["a", "b"]
    assert allowed_items(recipes, []) == []
    assert allowed_items(recipes, None) == ["a", "b", "c"]
    with pytest.raises(ValueError, match="未知"):
        allowed_items(recipes, ["不存在的材料"])


def test_cyclic_recipe_data_cannot_start_an_unbounded_maintenance_loop():
    data = catalog()
    data["building_data"]["workshopFormulas"]["p"]["costs"] = [{"id": "o", "count": 1}]
    with pytest.raises(ValueError, match="循环"):
        material_recipes(data, {"T3": 20, "T4": 10})

"""Explicit operators and game-data recipes for stock maintenance before shifts."""


def material_recipes(catalog, targets):
    items = catalog["item_table"]["items"]
    formulas = catalog["building_data"]["workshopFormulas"]
    recipes, seen = [], set()
    for formula in sorted(formulas.values(), key=lambda f: f.get("sortId", 0)):
        if formula.get("formulaType") != "F_EVOLVE":
            continue
        item_id = formula["itemId"]
        item = items[item_id]
        group = {"TIER_4": "T3", "TIER_5": "T4"}.get(item["rarity"])
        target = targets.get(item["name"], targets.get(group))
        if group is None or target is None or item.get("itemType") != "MATERIAL":
            continue
        if item_id in seen:
            raise ValueError("加工配方产物重复")
        seen.add(item_id)
        costs = {}
        for cost in formula["costs"]:
            identifier, count = cost["id"], cost["count"]
            if not isinstance(identifier, str) or type(count) is not int or count <= 0:
                raise ValueError("加工配方原料数量无效")
            costs[identifier] = costs.get(identifier, 0) + count
        gold, count = formula["goldCost"], formula["count"]
        if type(gold) is not int or gold < 0 or type(count) is not int or count != 1 or not costs:
            raise ValueError("加工配方产出或龙门币数量无效")
        if gold:
            costs["4001"] = costs.get("4001", 0) + gold
        recipes.append(
            {
                "item_id": item_id,
                "name": item["name"],
                "target": target,
                "tier": group,
                "formula_id": formula["formulaId"],
                "costs": costs,
            }
        )
    if not recipes:
        raise ValueError("加工配方表没有紫色／橙色精英化材料")
    # Consume available purple ingredients for orange deficits first, then refill
    # purple goals. No recursive sub-material crafting or chip conversion.
    recipes.sort(key=lambda r: items[r["item_id"]]["rarity"], reverse=True)
    names = {
        items[f["itemId"]]["name"]
        for f in formulas.values()
        if f.get("formulaType") == "F_EVOLVE"
        and items[f["itemId"]]["rarity"] in ("TIER_4", "TIER_5")
    }
    unknown = set(targets) - {"T3", "T4"} - names
    if unknown:
        raise ValueError("未知精英化加工材料: " + "、".join(sorted(unknown)))
    validate_recipe_dependencies(recipes)
    return tuple(recipes)


def next_recipe(recipes, stock):
    for recipe in recipes:
        count = stock.get(recipe["item_id"])
        if count is None or count >= recipe["target"]:
            continue
        if all(
            stock.get(item) is not None and stock[item] >= n for item, n in recipe["costs"].items()
        ):
            return recipe
    return None


def needs_processing(task, weekday):
    return (
        task.enabled
        and weekday in task.weekdays
        and task.task_type == "Infrast"
        and bool(task.processing_operators)
    )


async def prepare_processing(settings, profile, info, load_catalog):
    import asyncio

    from maa_planner.daemon.planning.daily import observe_depot
    from maa_planner.daemon.planning.skland import collect_character_info
    from maa_planner.daemon.tools.stage_catalog import load_processing_catalog

    if info is None:
        info, _ = await collect_character_info(settings, profile)
    depot = observe_depot(info.cultivate)
    catalog = await load_catalog()
    data = await asyncio.to_thread(
        load_processing_catalog, settings.cache_directory, catalog["commit"]
    )
    return {"catalog_commit": data["commit"], "catalog": data, "stock": depot}


def processing_phase(operators, targets, strategies, context, index):
    import copy

    prefix = f"Planner@Processing{index}"
    selected, files = [], {}
    all_recipes = material_recipes(context["catalog"], {"T3": 0, "T4": 0})
    recipes = material_recipes(context["catalog"], dict(targets))
    for position, operator in enumerate(operators):
        if operator == "九色鹿":
            continue
        allowed = allowed_items(all_recipes, dict(strategies).get(operator))
        filename = f"processing-{index}-{position}.json"
        files[filename] = {
            "plans": [
                {
                    "name": operator,
                    "rooms": {
                        "processing": [
                            {
                                "operators": [operator],
                                "autofill": False,
                                "sort": False,
                            }
                        ]
                    },
                }
            ]
        }
        selected.append(
            {"name": operator, "filename": "@file:" + filename, "allowed_items": allowed}
        )
    phase = {
        "catalog_commit": context["catalog_commit"],
        "recipes": [
            r
            for r in recipes
            if r["target"] > 0 and any(r["item_id"] in o["allowed_items"] for o in selected)
        ],
        "stock": copy.deepcopy(context["stock"]),
        "before_task": index,
        "navigation": prefix,
        "operators": selected,
    }
    return phase, files, processing_nodes(phase)


def processing_nodes(phase):
    from maa_planner.daemon.planning.private_tasks import home_navigation, inherited_task

    prefix = phase["navigation"]

    def n(name):
        return prefix + "@" + name

    def inherited(base, template, **fields):
        return inherited_task(
            base,
            template=template,
            action="DoNothing",
            next=[],
            sub=[],
            onErrorNext=[],
            exceededNext=[],
            **fields,
        )

    nodes = home_navigation(prefix)
    nodes.update(
        {
            "EnterBase": {"algorithm": "JustReturn", "next": ["InfrastBegin"]},
            "Open": {"algorithm": "JustReturn", "next": [n("Facility"), n("FacilityMini")]},
            "Facility": {
                "template": "InfrastProcessing.png",
                "roi": [0, 90, 1280, 600],
                "action": "ClickSelf",
                "postDelay": 1000,
            },
            "FacilityMini": {
                "template": "InfrastProcessingMini.png",
                "roi": [0, 90, 1280, 600],
                "action": "ClickSelf",
                "postDelay": 1000,
            },
            "Workshop": inherited("MiniGame@MaterialSynthesis@Workshop", "ProcessingFlag.png"),
            "LowMood": {
                "algorithm": "OcrDetect",
                "text": ["超负荷加工"],
                "roi": [649, 308, 362, 155],
            },
            "Locked": {
                "algorithm": "OcrDetect",
                "text": ["尚未解锁", "解锁条件", "配方未解锁"],
                "roi": [230, 100, 1050, 600],
                "action": "DoNothing",
            },
            "Unavailable": {
                "algorithm": "OcrDetect",
                "text": ["材料不足", "原料不足", "龙门币不足"],
                "roi": [600, 250, 680, 470],
                "action": "DoNothing",
            },
            "NoOperator": inherited(
                "MiniGame@MaterialSynthesis@NoOperator", "ProcessingNoOperator.png"
            ),
            "Increase": {
                "baseTask": "MiniGame@MaterialSynthesis@Increase",
                "template": "ProcessingIncrease.png",
                "next": [],
                "onErrorNext": [],
            },
            "Decrease": {
                "baseTask": "MiniGame@MaterialSynthesis@Decrease",
                "template": "ProcessingDecrease.png",
                "next": [],
                "onErrorNext": [],
            },
            "Quantity": {
                "algorithm": "OcrDetect",
                "text": [],
                "roi": [1020, 350, 140, 65],
                "action": "DoNothing",
            },
            "Start": {
                "baseTask": "MiniGame@MaterialSynthesis@Start",
                "template": "ProcessingStart.png",
                "next": [n("Obtain")],
                "onErrorNext": [],
            },
            "Obtain": {
                "baseTask": "MiniGame@MaterialSynthesis@Obtain",
                "template": "ItemObtainedIcon.png",
                "next": [n("Workshop")],
                "onErrorNext": [],
            },
            "Probe": {
                "algorithm": "JustReturn",
                "next": [n("LowMood"), n("NoOperator"), n("Unavailable"), n("Workshop")],
            },
            "RecipeList": {
                "algorithm": "OcrDetect",
                "text": ["配方", "方案"],
                "roi": [900, 80, 380, 200],
                "action": "ClickSelf",
                "postDelay": 500,
            },
            "Scroll": {
                "algorithm": "JustReturn",
                "action": "Swipe",
                "specificRect": [1140, 590, 1, 1],
                "rectMove": [1140, 220, 1, 1],
                "postDelay": 500,
            },
            "Top": {
                "algorithm": "JustReturn",
                "action": "Swipe",
                "specificRect": [1140, 220, 1, 1],
                "rectMove": [1140, 590, 1, 1],
                "postDelay": 500,
            },
        }
    )
    for position, recipe in enumerate(phase["recipes"]):
        nodes[f"Material{position}"] = {
            "template": "Planner@ProcessingMaterial-" + recipe["item_id"] + ".png",
            "roi": [500, 130, 400, 300],
            "action": "DoNothing",
            "templThreshold": 0.85,
            "maskRange": [1, 255],
        }
        nodes[f"Recipe{position}"] = {
            "algorithm": "OcrDetect",
            "text": [recipe["name"]],
            "fullMatch": True,
            "roi": [230, 100, 1050, 600],
            "action": "ClickSelf",
            "postDelay": 600,
        }
    return {n(name): node for name, node in nodes.items()}


def validate_phases(phases, tasks):
    import re

    if not isinstance(phases, list):
        raise ValueError("加工阶段必须为列表")
    seen = set()
    for phase in phases:
        if not isinstance(phase, dict) or set(phase) != {
            "catalog_commit",
            "recipes",
            "stock",
            "before_task",
            "navigation",
            "operators",
        }:
            raise ValueError("加工阶段字段无效")
        index = phase["before_task"]
        if (
            type(index) is not int
            or not 0 <= index < len(tasks)
            or tasks[index]["type"] != "Infrast"
            or index in seen
        ):
            raise ValueError("加工阶段必须关联唯一的基建任务")
        seen.add(index)
        if not re.fullmatch("[a-f0-9]{40}", phase["catalog_commit"]) or not re.fullmatch(
            "Planner@Processing[0-9]+", phase["navigation"]
        ):
            raise ValueError("加工阶段来源或导航无效")
        if not isinstance(phase["stock"], dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 0 for k, v in phase["stock"].items()
        ):
            raise ValueError("加工库存无效")
        if not isinstance(phase["operators"], list) or any(
            not isinstance(o, dict)
            or set(o) != {"name", "filename", "allowed_items"}
            or not isinstance(o["name"], str)
            or not o["name"]
            or o["name"] == "九色鹿"
            or not isinstance(o["filename"], str)
            or not o["filename"]
            or not isinstance(o["allowed_items"], list)
            or any(
                not isinstance(v, str) or not re.fullmatch("[A-Za-z0-9_-]+", v)
                for v in o["allowed_items"]
            )
            for o in phase["operators"]
        ):
            raise ValueError("加工干员无效")
        if not isinstance(phase["recipes"], list):
            raise ValueError("加工配方列表无效")
        for recipe in phase["recipes"]:
            if (
                not isinstance(recipe, dict)
                or not re.fullmatch("[A-Za-z0-9_-]+", recipe.get("item_id", ""))
                or set(recipe) != {"item_id", "name", "target", "formula_id", "costs", "tier"}
                or any(
                    not isinstance(recipe[k], str) or not recipe[k]
                    for k in ("item_id", "name", "formula_id")
                )
                or type(recipe["target"]) is not int
                or recipe["target"] < 0
                or recipe["tier"] not in {"T3", "T4"}
                or not isinstance(recipe["costs"], dict)
                or not recipe["costs"]
                or any(
                    not isinstance(k, str) or type(v) is not int or v <= 0
                    for k, v in recipe["costs"].items()
                )
            ):
                raise ValueError("加工配方无效")
        validate_recipe_dependencies(phase["recipes"])


def allowed_items(recipes, tokens):
    if tokens is None:
        return [r["item_id"] for r in recipes]
    names = {r["name"] for r in recipes}
    unknown = {t.removeprefix("-") for t in tokens} - names - {"T3", "T4"}
    if unknown:
        raise ValueError("未知加工策略材料: " + "、".join(sorted(unknown)))
    positive = [t for t in tokens if not t.startswith("-")]
    negative = {t[1:] for t in tokens if t.startswith("-")}
    return [
        r["item_id"]
        for r in recipes
        if (
            (r["name"] in positive or r["tier"] in positive or (tokens and not positive))
            and r["name"] not in negative
            and r["tier"] not in negative
        )
    ]


def validate_recipe_dependencies(recipes):
    graph = {r["item_id"]: set(r["costs"]) for r in recipes}
    if len(graph) != len(recipes):
        raise ValueError("加工配方产物重复")
    visited, visiting = set(), set()

    def visit(item):
        if item in visiting:
            raise ValueError("加工配方存在循环依赖")
        if item in visited or item not in graph:
            return
        visiting.add(item)
        for ingredient in graph[item]:
            visit(ingredient)
        visiting.remove(item)
        visited.add(item)

    for item in graph:
        visit(item)

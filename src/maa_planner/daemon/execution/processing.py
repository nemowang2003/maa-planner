"""One operator at a time; verified single crafts before the ordinary shift."""

from pathlib import Path

from maa_planner.daemon.planning.processing import next_recipe
from maa_planner.daemon.tools.storage import write_json


def run_processing(phase, driver):
    if not phase["recipes"] or not phase["operators"]:
        return {"outcome": "completed", "results": [], "stock": dict(phase["stock"])}
    stock = driver.read_stock()
    if stock is None:
        return {"outcome": "error", "reason": "inventory_unknown", "results": []}
    results = []
    blocked = set()
    for operator in phase["operators"]:
        if driver.cancelled():
            raise InterruptedError("加工取消")
        recipes = [
            r
            for r in phase["recipes"]
            if r["item_id"] not in blocked and r["item_id"] in operator["allowed_items"]
        ]
        if next_recipe(recipes, stock) is None:
            results.append(
                {"operator": operator["name"], "outcome": "skipped", "reason": "no_allowed_deficit"}
            )
            continue
        selection = driver.select_operator(operator)
        if selection == "error":
            return {
                "outcome": "error",
                "reason": "operator_selection_unverified",
                "results": results,
            }
        if selection == "unavailable":
            results.append(
                {
                    "operator": operator["name"],
                    "outcome": "skipped",
                    "reason": "operator_unavailable",
                }
            )
            continue
        while (recipe := next_recipe(recipes, stock)) is not None:
            if driver.cancelled():
                raise InterruptedError("加工取消")
            outcome = driver.craft(recipe)
            results.append(
                {"operator": operator["name"], "item_id": recipe["item_id"], "outcome": outcome}
            )
            if outcome == "low_mood":
                break
            if outcome in {"unavailable", "locked"}:
                blocked.add(recipe["item_id"])
                recipes = [r for r in recipes if r["item_id"] not in blocked]
                continue
            if outcome != "completed":
                return {"outcome": "error", "reason": "craft_unverified", "results": results}
            stock[recipe["item_id"]] += 1
            for item, count in recipe["costs"].items():
                stock[item] -= count
    return {
        "outcome": "completed",
        "results": results,
        "stock": stock,
        "remaining": [
            {"item_id": r["item_id"], "count": stock.get(r["item_id"]), "target": r["target"]}
            for r in phase["recipes"]
            if stock.get(r["item_id"]) is None or stock[r["item_id"]] < r["target"]
        ],
    }


class CoreProcessingDriver:
    def __init__(self, phase, execute, results, stop):
        self.phase, self.execute, self.results, self.stop = phase, execute, results, stop
        self.prefix = phase["navigation"]
        self.current_recipe = None

    def cancelled(self):
        return self.stop.is_set()

    def step(self, name):
        ids = self.execute(
            [
                {
                    "type": "Custom",
                    "name": "加工:" + name,
                    "params": {"task_names": [self.prefix + "@" + name]},
                }
            ],
            handled=True,
        )
        if self.cancelled():
            raise InterruptedError("加工取消")
        if not ids or self.results.tasks[ids[0]]["status"] != "completed":
            return None
        return ids[0]

    def read_stock(self):
        import json

        ids = self.execute(
            [{"type": "Depot", "name": "加工前复核库存", "params": {}}], handled=True
        )
        if self.cancelled():
            raise InterruptedError("加工库存识别取消")
        if not ids or self.results.tasks[ids[0]]["status"] != "completed":
            return None
        samples = [
            details
            for what, details in self.results.extras.get(ids[0], [])
            if what == "DepotInfo" and details.get("done") is True
        ]
        if len(samples) != 1:
            return None
        try:
            stock = json.loads(samples[0]["data"])
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(stock, dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 0 for k, v in stock.items()
        ):
            return None
        # LMD is not an ordinary depot card; its explicit API amount is a
        # conservative planning budget. The game rechecks actual affordability.
        if "4001" not in stock and "4001" in self.phase["stock"]:
            stock["4001"] = self.phase["stock"]["4001"]
        return stock

    def select_operator(self, operator):
        self.current_recipe = None
        ids = self.execute(
            [
                {
                    "type": "Infrast",
                    "name": "加工选人:" + operator["name"],
                    "params": {
                        "mode": 10000,
                        "facility": ["Processing"],
                        "filename": operator["filename"],
                        "plan_index": 0,
                        "drones": "_NotUse",
                        "replenish": False,
                        "dorm_trust_enabled": False,
                    },
                }
            ],
            handled=True,
        )
        if self.cancelled():
            raise InterruptedError("加工选人取消")
        if not ids or self.results.tasks[ids[0]]["status"] != "completed":
            return "error"
        extras = self.results.extras.get(ids[0], [])
        if any(
            what in {"CustomInfrastRoomKeptUnchanged", "CustomInfrastRoomGroupsMatchFailed"}
            for what, _ in extras
        ):
            return "unavailable"
        if not any(what == "InfrastConfirmButton" for what, _ in extras):
            return "error"
        if (
            self.step("EnterBase") is None
            or self.step("Open") is None
            or self.step("Workshop") is None
        ):
            return "error"
        return "ready"

    def craft(self, recipe):
        # Navigation never consumes anything; the selected output must match.
        position = self.phase["recipes"].index(recipe)
        if self.current_recipe != recipe["item_id"]:
            if self.step("RecipeList") is None:
                return "unknown"
            for _ in range(10):
                if self.step("Top") is None:
                    return "unknown"
            for _ in range(12):
                if self.step(f"Recipe{position}") is not None:
                    break
                identifier = self.step("Locked")
                if identifier is not None:
                    return "locked"
                if self.step("Scroll") is None:
                    return "unknown"
            else:
                return "unknown"
        if self.step(f"Material{position}") is None:
            return "unknown"
        self.current_recipe = recipe["item_id"]
        quantity = None
        for _ in range(100):
            identifier = self.step("Quantity")
            if identifier is None:
                return "unknown"
            values = self.results.matches.get(identifier, [])
            texts = [v.get("text") for node, v in values if node == self.prefix + "@Quantity"]
            if (
                len(texts) != 1
                or not isinstance(texts[0], str)
                or not texts[0].isascii()
                or not texts[0].isdigit()
            ):
                return "unknown"
            quantity = int(texts[0])
            if quantity == 1:
                break
            if not 1 < quantity <= 99 or self.step("Decrease") is None:
                return "unknown"
        if quantity != 1:
            return "unknown"
        identifier = self.step("Probe")
        if identifier is None:
            return "unknown"
        nodes = {node for node, _ in self.results.nodes.get(identifier, [])}
        if "LowMood" in nodes:
            return "low_mood"
        if nodes & {"NoOperator", "Unavailable"}:
            return "unavailable"
        identifier = self.step("Start")
        if identifier is None:
            return "unknown"
        nodes = {node for node, _ in self.results.nodes.get(identifier, [])}
        return "completed" if {"Start", "Obtain", "Workshop"} <= nodes else "unknown"


def execute_processing(phase, execute, results, stop, directory):
    report = run_processing(phase, CoreProcessingDriver(phase, execute, results, stop))
    write_json(Path(directory) / (phase["navigation"].replace("@", "-") + "-result.json"), report)
    return report


def material_templates(phases, resource_directory, destination):
    """Derive private templates from the pinned Core resources under its read lock."""
    import io

    from PIL import Image

    images = {}
    for phase in phases:
        for recipe in phase["recipes"]:
            name = "Planner@ProcessingMaterial-" + recipe["item_id"] + ".png"
            if name in images:
                continue
            source = Path(resource_directory) / "template/items" / (recipe["item_id"] + ".png")
            with Image.open(source) as image:
                # Same material icon scale as upstream MaterialSynthesisImageAnalyzer.
                scaled = image.convert("RGB").resize(
                    tuple(round(v * 1.2) for v in image.size), Image.Resampling.BILINEAR
                )
                output = io.BytesIO()
                scaled.save(output, format="PNG")
            path = Path(destination) / name
            path.write_bytes(output.getvalue())
            images[name] = str(path)
    return images

"""Order battles, tutorial barriers, and explicit regional trailing stories."""

import re


def is_tutorial(stage):
    return bool(re.fullmatch(r"[A-Za-z0-9]+-TR-[0-9]+", stage.get("code", "")))


def add_tutorial_barriers(prepared, catalog, *, now):
    from maa_planner.daemon.planning.stories import add_story_targets

    prepared = add_story_targets(prepared, catalog, now=now)
    plan, _, _ = prepared
    stages = catalog["stage_table"]["stages"]
    completed = {tuple(v) for v in plan["completed"]}
    entries = list(plan["targets"])
    present = {(entry["stage_id"], entry["is_raid"]) for entry in entries}
    for identifier, raid in plan["required"]:
        stage = stages[identifier]
        if raid or not is_tutorial(stage) or (identifier, False) in completed | present:
            continue
        zone = catalog["zone_table"].get("zoneValidInfo", {}).get(stage["zoneId"], {})
        window = catalog["stage_table"].get("stageValidInfo", {}).get(identifier, {})
        if (
            not zone.get("startTs", float("inf")) <= now <= zone.get("endTs", -1)
            or window.get("startTs", 0) > now
        ):
            continue
        entries.append(
            {
                "kind": "missing_tutorial",
                "script_id": 0,
                "stage_id": identifier,
                "code": stage["code"],
                "is_raid": False,
                "game_stage_id": identifier,
                "script_stage_id": identifier,
                "navigation": "Planner@TutorialStop",
                "zone_id": stage["zoneId"],
            }
        )
    # Traverse intermediate plots without requiring their ledger entries; retain
    # explicit trailing plots as prerequisites before the dependent region.
    explicit_stories = {e["stage_id"] for e in entries if e.get("kind") == "tail_story"}
    memo = {}

    def dependencies(identifier, visiting):
        if identifier in visiting:
            raise ValueError("活动前置路径存在循环")
        if identifier in memo:
            return memo[identifier]
        found = set()
        for condition in stages[identifier].get("unlockCondition", []):
            parent = condition["stageId"]
            if parent not in stages:
                continue
            if stages[parent].get("isStoryOnly") is True and parent not in explicit_stories:
                found.update(dependencies(parent, visiting | {identifier}))
            else:
                found.add(parent)
        memo[identifier] = found
        return found

    ordered = []
    while entries:
        pending = {entry["game_stage_id"] for entry in entries}
        for index, entry in enumerate(entries):
            if dependencies(entry["game_stage_id"], set()) & pending:
                continue
            ordered.append(entries.pop(index))
            break
        else:
            raise ValueError("活动前置路径存在循环")
    if len(ordered) > 200:
        raise ValueError("自动通关超过 200 个目标")
    plan["targets"] = ordered
    return prepared

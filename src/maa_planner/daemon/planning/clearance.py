"""Freeze URL contents, current activity mapping and project-owned navigation."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ActivitySnapshot
from maa_planner.daemon.planning.activity_scope import activity_scope
from maa_planner.daemon.planning.copilot import stage_definitions, targets
from maa_planner.daemon.planning.navigation import activity_labels as page_labels
from maa_planner.daemon.planning.private_tasks import (
    activity_entry,
    activity_page_navigation,
    home_navigation,
    inherited_task,
)


def navigation_nodes(
    prefix: str,
    activity: str,
    region: str,
    codes: list[str],
    *,
    activity_labels: list[str] | None = None,
) -> dict:
    def node(name):
        return prefix + "@" + name

    def inherited(base, **fields):
        return inherited_task(base, **fields)

    nodes = {
        **home_navigation(prefix),
        "Terminal": inherited("Fight", next=[node("TerminalReady")]),
        # Terminal tabs shift when the set of visible modes changes.
        "TerminalReady": inherited(
            "GoTerminalStopflag", roi=[0, 0, 1280, 720], action="DoNothing", next=[]
        ),
        "Activity": {**activity_entry(activity, activity_labels or [activity]), "next": []},
        "Area": {"algorithm": "JustReturn", "next": [node("Map"), node("Region"), node("Locked")]},
        "Map": inherited("ClickStageName", text=codes, action="DoNothing", next=[]),
        **activity_page_navigation(prefix, "Map", "Region", root="SwitchArea"),
        "Region": {
            "algorithm": "OcrDetect",
            "text": [region],
            "roi": [0, 0, 1280, 720],
            "action": "ClickSelf",
            "postDelay": 2000,
            "next": [node("Map"), node("Locked")],
        },
        "Locked": {
            "algorithm": "OcrDetect",
            "text": ["尚未解锁", "解锁条件", "尚未开放"],
            "roi": [0, 0, 1280, 720],
            "action": "Stop",
        },
        "NonPerfect": inherited("StageDrops-Stars-2", action="DoNothing", next=[]),
    }
    return {node(name): value for name, value in nodes.items()}


def prepare_clearance(
    scripts: tuple[tuple[int, dict], ...],
    catalog: dict,
    activity: ActivitySnapshot,
    *,
    now: float,
    activity_id: str | None = None,
) -> tuple[dict, dict, dict]:
    stages = catalog["stage_table"]["stages"]
    activity_table = catalog["activity_table"]
    mapping, basic = activity_table["zoneToActivity"], activity_table["basicInfo"]
    original_activities = {mapping[stages[script["stage_name"]]["zoneId"]] for _, script in scripts}
    names = {basic[identifier]["name"] for identifier in original_activities}
    if activity_id is not None:
        names.add(basic[activity_id]["name"])
    local = datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
    active = [s for s in activity.data.active_side_stories(local) if s.name in names]
    if len(active) != 1 or len(names) != 1:
        raise ValueError("作业集未唯一对应 MAA 当前开放活动")
    candidates = [
        v
        for v in basic.values()
        if v["name"] == active[0].name and v["startTime"] <= now <= v["endTime"]
    ]
    if len(candidates) != 1:
        raise ValueError("无法唯一关联当前活动 ID")
    activity_id = candidates[0]["id"]
    rewritten = []
    originals = dict(scripts)
    for identifier, script in scripts:
        source = stages[script["stage_name"]]
        matches = [
            k
            for k, v in stages.items()
            if v.get("difficulty") == "NORMAL"
            and mapping.get(v.get("zoneId")) == activity_id
            and v.get("code") == source["code"]
            and isinstance(v.get("levelId"), str)
            and v["levelId"].lower() == source["levelId"].lower()
        ]
        if len(matches) != 1:
            raise ValueError(f"作业关卡无法关联当前活动: {source['code']}")
        rewritten.append((identifier, {**script, "stage_name": matches[0]}))
    definitions = stage_definitions(
        catalog["stage_table"], tuple(s["stage_name"] for _, s in rewritten)
    )
    selected = targets(tuple(rewritten), definitions)
    resources, entries = {}, []
    zones = catalog["zone_table"]["zones"]
    prefixes = {}
    for target in selected:
        definition = definitions[target.game_stage_id]
        region = zones[definition.zone_id].get("zoneNameSecond")
        if not isinstance(region, str) or not region:
            raise ValueError("活动区域缺少可导航名称")
        region = region.strip('“”"')
        if definition.zone_id not in prefixes:
            prefix = f"Planner@Clearance{len(prefixes)}"
            prefixes[definition.zone_id] = prefix
            codes = list(
                dict.fromkeys(
                    v["code"]
                    for v in stages.values()
                    if v.get("zoneId") == definition.zone_id and v.get("code")
                )
            )
            resources.update(
                navigation_nodes(
                    prefix,
                    active[0].name,
                    region,
                    codes,
                    activity_labels=page_labels(catalog, activity_id),
                )
            )
        prefix = prefixes[definition.zone_id]
        entries.append(
            {
                **asdict(target),
                "filename": f"@file:clearance-{target.script_id}.json",
                "script_stage_id": originals[target.script_id]["stage_name"],
                "navigation": prefix,
                "zone_id": definition.zone_id,
            }
        )
    scope = activity_scope(catalog, activity_id, now=now)
    plan = {
        "activity_id": activity_id,
        "activity_name": active[0].name,
        "targets": entries,
        "completed": [],
        "required": scope["required"],
        "catalog_complete": scope["catalog_complete"],
        "catalog_commit": catalog["commit"],
        "available_from": active[0].starts_at.timestamp(),
        "available_until": active[0].ends_at.timestamp(),
    }
    attachments = {f"clearance-{identifier}.json": script for identifier, script in scripts}
    return plan, attachments, resources

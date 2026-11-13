"""Freeze the current rotation and roster; search scripts only after live NoProxy."""

import json
import math
import re
from urllib.parse import urlencode

from maa_planner.daemon.constants import COPILOT_API_URL, COPILOT_CREATOR_ID
from maa_planner.daemon.execution.copilot import MAX_SCRIPTS, _Source
from maa_planner.daemon.planning.auto_copilot import observe_operators


def current_candidates(catalog, campaign, *, now):
    """Freeze only the uniquely active rotation; permanent/open maps are irrelevant."""
    available = {
        row["stageId"]: (row["startTs"], row["endTs"] + 1)
        for row in campaign["campaignRotateStageOpenTimes"]
        if row["startTs"] <= now <= row["endTs"]
    }
    if len(available) != 1:
        raise ValueError("当期轮换剿灭无法唯一确定")
    identifier, window = next(iter(available.items()))
    if not re.fullmatch(r"camp_r_[0-9]+", identifier):
        raise ValueError("当期剿灭不是轮换关卡")
    stage = catalog["stage_table"]["stages"][identifier]
    if not isinstance(stage.get("name"), str) or not stage["name"]:
        raise ValueError("当期剿灭缺少可核对名称")
    return (
        {
            "stage_id": identifier,
            "name": stage["name"],
            "available_from": window[0],
            "available_until": window[1],
        },
    )


def search_scripts(cache, entry, cancel):
    """Called after live NoProxy/map evidence, never during schedule preparation."""
    identifier = entry["stage_id"]
    source = _Source(cancel, cache)
    count = 0
    scripts, seen = [], set()
    for page in range(1, 21):
        query = urlencode(
            {
                "page": page,
                "limit": 50,
                "levelKeyword": identifier,
                "uploaderId": COPILOT_CREATOR_ID,
                "desc": "true",
            }
        )
        data = source.get(COPILOT_API_URL + "/copilot/query?" + query)
        rows, more = data.get("data"), data.get("has_next")
        if not isinstance(rows, list) or type(more) is not bool or (more and not rows):
            raise ValueError("剿灭作业分页格式无效")
        for row in rows:
            script_id = row.get("id") if isinstance(row, dict) else None
            if (
                type(script_id) is not int
                or script_id <= 0
                or script_id in seen
                or str(row.get("uploader_id")) != COPILOT_CREATOR_ID
            ):
                raise ValueError("剿灭作业作者或分页身份无效")
            seen.add(script_id)
            preview = json.loads(row["content"])
            if preview.get("stage_name") != identifier or row.get("available") is not True:
                continue
            count += 1
            if count > MAX_SCRIPTS:
                raise ValueError("剿灭作业候选超过预算，不使用截断结果")
            body = source.get(f"{COPILOT_API_URL}/copilot/get/{script_id}")
            script = json.loads(body["content"])
            if script.get("stage_name") != identifier or not isinstance(
                script.get("actions"), list
            ):
                raise ValueError("剿灭作业正文关卡不匹配或缺少动作")
            scripts.append((script_id, script))
        if not more:
            break
    else:
        raise ValueError("剿灭作业超过分页预算")
    return tuple(scripts)


async def prepare_rescue(settings, profile, info, load_candidates, cancel, *, now):
    if info is None:
        from maa_planner.daemon.planning.skland import collect_character_info

        info, _ = await collect_character_info(settings, profile)
    owned = observe_operators(info.cultivate, info.player_info)
    catalog_commit, public = await load_candidates()
    entries = [dict(entry) for entry in public]
    context = {
        "candidates": entries,
        "catalog_commit": catalog_commit,
        "prepared_at": now,
        "owned_operators": sorted(owned),
    }
    validate_context(context)
    return context, {}, rescue_nodes(context)


def rescue_nodes(context):
    from maa_planner.daemon.execution.annihilation_tasks import delegation_task

    _, existing = delegation_task(
        {"type": "Fight", "params": {"stage": "Annihilation", "times": 0}}, 0
    )
    old, prefix = "Planner@Annihilation0@", "Planner@Rescue@"
    names = {
        "Ready",
        "Activate",
        "NoProxy",
        "OrdinaryProxy",
        "Unavailable",
        "Uncertain",
        "Record",
        "LevelUp",
        "Wait",
        "Settled",
        "SettledGeneric",
        "Dismiss",
    }

    def rewrite(value):
        if isinstance(value, dict):
            return {k: rewrite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        return value.replace(old, prefix) if isinstance(value, str) else value

    nodes = {prefix + name: rewrite(existing[old + name]) for name in names}
    nodes[prefix + "Ready"]["next"] = []
    nodes[prefix + "Wait"]["maxTimes"] = 2**31 - 1
    nodes[prefix + "Wait"]["next"].insert(0, prefix + "Defeat")
    nodes[prefix + "Defeat"] = {
        "baseTask": "FightMissionFailed",
        "action": "DoNothing",
        "next": [],
        "onErrorNext": [],
        "exceededNext": [],
    }
    for index, entry in enumerate(context["candidates"]):
        nodes[prefix + f"Stage{index}"] = {
            "algorithm": "OcrDetect",
            "text": [entry["name"]],
            "fullMatch": True,
            "roi": [0, 0, 1280, 720],
            "action": "DoNothing",
            "next": [],
        }
    return nodes


def validate_context(context):
    if (
        not isinstance(context, dict)
        or set(context) != {"candidates", "catalog_commit", "prepared_at", "owned_operators"}
        or not isinstance(context["catalog_commit"], str)
        or not re.fullmatch("[a-f0-9]{40}", context["catalog_commit"])
    ):
        raise ValueError("剿灭补救目录版本无效")
    if type(context["prepared_at"]) not in (int, float) or not math.isfinite(
        context["prepared_at"]
    ):
        raise ValueError("剿灭补救准备时间无效")
    entries = context["candidates"]
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("剿灭补救候选清单无效")
    owned = context["owned_operators"]
    if (
        not isinstance(owned, list)
        or len(owned) > 2000
        or any(not isinstance(name, str) or not name for name in owned)
        or len(set(owned)) != len(owned)
    ):
        raise ValueError("剿灭拥有干员快照无效")
    seen = set()
    stage_ids = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not {"stage_id", "name", "available_from", "available_until"} <= entry.keys()
            or entry.keys()
            - {
                "stage_id",
                "name",
                "available_from",
                "available_until",
            }
        ):
            raise ValueError("剿灭补救关卡字段无效")
        if (
            any(not isinstance(entry[k], str) or not entry[k] for k in ("stage_id", "name"))
            or entry["name"] in seen
            or entry["stage_id"] in stage_ids
        ):
            raise ValueError("剿灭补救名称无效或不唯一")
        seen.add(entry["name"])
        stage_ids.add(entry["stage_id"])
        if (
            any(
                type(entry[k]) not in (int, float) or not math.isfinite(entry[k])
                for k in ("available_from", "available_until")
            )
            or not entry["available_from"] < entry["available_until"]
        ):
            raise ValueError("剿灭补救有效期无效")
        if not re.fullmatch(r"camp_r_[0-9]+", entry["stage_id"]):
            raise ValueError("剿灭补救只允许当期轮换关卡")

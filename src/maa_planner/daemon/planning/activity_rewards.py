"""Private activity mission collection; independent of full-clear/garbage gates."""

import math
import re

from maa_planner.daemon.planning.activity_shop import shop_navigation
from maa_planner.daemon.planning.private_tasks import activity_page_navigation
from maa_planner.daemon.tools.reward_roi import CLAIM_TEXT, reward_roi_key, valid_roi


def reward_task(plan, index):
    texts = plan["mission_texts"]
    if not texts:
        raise ValueError("活动任务页缺少游戏数据中的任务文本，不能使用人工截图锚点")
    prefix = f"Planner@Rewards{index}"

    def node(name):
        return prefix + "@" + name

    nodes = shop_navigation({**plan, "entry": plan["mission_entry"]}, prefix)
    # One task follows each explicit page checkpoint. A recognized title alone
    # never certifies a reward, and no missing button is interpreted as complete.
    nodes[node("Home")].update(action="DoNothing", next=[node("Terminal")])
    nodes[node("TerminalReady")]["next"] = [node("Open")]
    entries = [name for name in nodes if name.startswith(node("Entry"))]
    for name in entries:
        nodes[name]["next"] = [node("MissionReady")]
    nodes.pop(node("ShopReady"))
    nodes.update(
        {
            node(k): v
            for k, v in activity_page_navigation(
                prefix, "MissionReady", [name[len(prefix) + 1 :] for name in entries]
            ).items()
        }
    )
    # Use serialized mission descriptions, never a title/ROI selected from a frame.
    nodes[node("MissionReady")] = {
        "algorithm": "OcrDetect",
        "text": texts,
        "fullMatch": True,
        "roi": [0, 0, 1280, 720],
        "action": "DoNothing",
        "next": [node("MissionControls"), node("Locked"), node("Incomplete"), node("NoClaim")],
    }
    nodes[node("MissionControls")] = {
        "algorithm": "OcrDetect",
        "text": list(CLAIM_TEXT),
        "fullMatch": True,
        "roi": [0, 0, 1280, 720],
        "action": "DoNothing",
        "next": [node("Claim"), node("Locked"), node("Incomplete"), node("NoClaim")],
    }
    nodes[node("Claim")] = {
        "algorithm": "OcrDetect",
        "text": list(CLAIM_TEXT),
        "fullMatch": True,
        "roi": [0, 0, 1280, 720],
        "action": "ClickSelf",
        "postDelay": 1500,
        "next": [node("Received"), node("Locked"), node("Incomplete"), node("NoReceipt")],
    }
    nodes[node("Received")] = {
        "baseTask": "SignInEventEarnRewards",
        "next": [],
        "exceededNext": [],
        "maxTimes": 1,
    }
    nodes[node("NoReceipt")] = {"algorithm": "JustReturn", "next": []}
    # This fallback is reachable only after positive task-page recognition.
    # An absent claim button is normal and never proves all missions complete.
    nodes[node("NoClaim")] = {"algorithm": "JustReturn", "next": []}
    for name, text in (
        ("Locked", ["尚未开放", "尚未解锁"]),
        ("Incomplete", ["未完成", "尚未达成"]),
    ):
        nodes[node(name)] = {
            "algorithm": "OcrDetect",
            "text": text,
            "roi": [0, 0, 1280, 720],
            "action": "DoNothing",
            "next": [],
        }
    cached = plan.get("claim_roi")
    if cached is not None:
        if not valid_roi(cached):
            raise ValueError("领奖按钮缓存 ROI 无效")
        nodes[node("CachedControls")] = {
            **nodes[node("MissionControls")],
            "roi": list(cached),
            "next": [
                node("CachedClaim"),
                node("MissionControls"),
                node("Locked"),
                node("Incomplete"),
                node("NoClaim"),
            ],
        }
        nodes[node("CachedClaim")] = {**nodes[node("Claim")], "roi": list(cached)}
        nodes[node("MissionReady")]["next"].insert(0, node("CachedControls"))
    marker = {
        "activity_id": plan["activity_id"],
        "navigation": prefix,
        "available_from": plan["available_from"],
        "available_until": plan["available_until"],
        "navigation_revision": plan["navigation_revision"],
        "page_source": plan["mission_page_source"],
        "claim_roi_key": reward_roi_key(plan),
    }
    return {
        "name": plan["key"] + ":missions",
        "type": "Custom",
        "params": {"task_names": [node("ToHome")], "activity_reward": marker},
    }, nodes


def validate_reward_marker(task):
    marker = task.get("params", {}).get("activity_reward")
    if "activity_reward" not in task.get("params", {}):
        return
    if (
        task.get("type") != "Custom"
        or not isinstance(marker, dict)
        or set(marker)
        != {
            "activity_id",
            "navigation",
            "available_from",
            "available_until",
            "navigation_revision",
            "page_source",
            "claim_roi_key",
        }
    ):
        raise ValueError("活动奖励标记无效")
    if (
        any(
            not isinstance(marker[k], str) or not marker[k]
            for k in ("activity_id", "navigation_revision")
        )
        or not isinstance(marker["navigation"], str)
        or not re.fullmatch(r"Planner@Rewards[0-9]+", marker["navigation"])
    ):
        raise ValueError("活动奖励身份无效")
    if (
        any(
            type(marker[k]) not in (int, float) or not math.isfinite(marker[k])
            for k in ("available_from", "available_until")
        )
        or marker["available_from"] >= marker["available_until"]
    ):
        raise ValueError("活动奖励窗口无效")
    source = marker["page_source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"table", "catalog_commit", "activity_id", "mission_ids"}
        or source["table"] != "activity_table.missionData"
        or source["activity_id"] != marker["activity_id"]
        or not isinstance(source["catalog_commit"], str)
        or not re.fullmatch(r"[a-f0-9]{40}", source["catalog_commit"])
        or not isinstance(source["mission_ids"], list)
        or not source["mission_ids"]
        or any(not isinstance(value, str) or not value for value in source["mission_ids"])
    ):
        raise ValueError("活动奖励任务页的游戏数据来源无效")
    from maa_planner.daemon.tools.reward_roi import valid_key

    if not valid_key(marker["claim_roi_key"]):
        raise ValueError("领奖 ROI 缓存身份无效")
    if task["params"].get("task_names") != [marker["navigation"] + "@ToHome"]:
        raise ValueError("活动奖励入口与私有标记不匹配")

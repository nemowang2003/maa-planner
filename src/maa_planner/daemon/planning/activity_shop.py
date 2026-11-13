"""Full activity scope and shop eligibility; script coverage is never full scope."""

import hashlib
import json
import math
import re
from datetime import datetime

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ActivitySnapshot
from maa_planner.daemon.planning.activity_scope import activity_scope
from maa_planner.daemon.planning.annihilation import is_annihilation_stage
from maa_planner.daemon.planning.farming import MATERIAL_BLACKLIST
from maa_planner.daemon.planning.navigation import (
    NAVIGATION_REVISION,
    activity_page_data,
)
from maa_planner.daemon.planning.private_tasks import (
    activity_entry,
    activity_page_navigation,
    home_navigation,
    inherited_task,
)

DEFAULT_SHOP_BLACKLIST = ("模组增补仪", "模组增补条", "家具零件")


class ActivityCatalogError(ValueError):
    """The immutable stage catalog cannot uniquely identify the activity."""


def shop_eligibility(plan: dict, completed: set | frozenset, now: float) -> str:
    if not plan["available_from"] <= now < plan["available_until"]:
        return "activity_closed"
    if not plan["catalog_complete"]:
        return "catalog_incomplete"
    if now < plan["all_regions_from"]:
        return "regions_not_open"
    if now >= plan["all_regions_until"]:
        return "region_closed"
    if not all((stage, raid) in completed for stage, raid in plan["required"]):
        return "stages_incomplete"
    return "ready"


def garbage_materials(story, *, threshold: int, targets: dict[str, int], depot) -> list[dict]:
    """Return every material recommendation; no fixed material-count assumption."""
    materials = []
    seen = set()
    for stage in story.stages:
        drop = stage.drop
        if not (drop.isascii() and drop.isdecimal()) or drop in seen:
            continue
        seen.add(drop)
        exempt = drop in MATERIAL_BLACKLIST
        materials.append(
            {
                "item_id": drop,
                "target": 0 if exempt else targets.get(drop, threshold),
                "stock": depot.get(drop, 0) if depot is not None else None,
                "exempt": exempt,
            }
        )
    return materials


def prepare_shops(
    catalog: dict,
    activity: ActivitySnapshot,
    *,
    now: float,
    navigation: dict | None = None,
    blacklist: tuple[str, ...],
) -> tuple[dict, ...]:
    table = catalog["activity_table"]
    output = []
    current = datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
    for story in activity.data.active_side_stories(current):
        candidates = [
            v
            for v in table["basicInfo"].values()
            if v.get("name") == story.name and v["startTime"] <= now < v["endTime"] + 1
        ]
        if len(candidates) != 1:
            raise ActivityCatalogError(f"活动 {story.key} 无法唯一关联商店所属活动 ID")
        basic = candidates[0]
        activity_id = basic["id"]
        assets = json.loads(json.dumps((navigation or {}).get(activity_id, {})))
        full_scope = activity_scope(catalog, activity_id)
        scope = {
            "activity_id": activity_id,
            "zones": full_scope["zones"],
            "required": full_scope["required"],
            "blacklist": list(blacklist),
        }
        fingerprint = hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        output.append(
            {
                **scope,
                **activity_page_data(catalog, activity_id),
                "key": story.key,
                "name": story.name,
                "fingerprint": fingerprint,
                "catalog_commit": catalog["commit"],
                "catalog_complete": full_scope["catalog_complete"],
                "completed": [],
                "garbage": False,
                "shop_completed": False,
                "all_regions_from": full_scope["all_regions_from"] or story.ends_at.timestamp(),
                "all_regions_until": full_scope["all_regions_until"] or story.starts_at.timestamp(),
                "available_from": story.starts_at.timestamp(),
                "available_until": story.ends_at.timestamp(),
                "entry": assets.get("entries", {}).get("shop", []),
                "mission_entry": assets.get("entries", {}).get("missions", []),
                "shop_ready": assets.get("shop_ready", []),
                "navigation_images": assets.get("images", {}),
                "navigation_source": assets.get("source", {}),
                "navigation_revision": NAVIGATION_REVISION,
            }
        )
    return tuple(output)


def shop_navigation(plan: dict, prefix: str) -> dict:
    def node(name):
        return prefix + "@" + name

    entries = plan["entry"]
    entry_keys = ["Entry" if i == 0 else f"Entry{i}" for i in range(len(entries))]
    entry_names = [node(key) for key in entry_keys]
    if not entry_names or not plan["shop_ready"]:
        raise ValueError("活动导航缺少入口或商店横幅资源")
    nodes = {
        **home_navigation(prefix),
        **activity_page_navigation(prefix, "ShopReady", entry_keys),
        "Terminal": inherited_task("Fight", next=[node("TerminalReady")]),
        "TerminalReady": inherited_task(
            "GoTerminalStopflag", roi=[0, 0, 1280, 720], action="DoNothing", next=[]
        ),
        "Open": {
            **activity_entry(plan["name"], plan["activity_labels"]),
            "next": entry_names,
        },
        "ShopReady": {
            "algorithm": "MatchTemplate",
            "template": plan["shop_ready"],
            "templThreshold": 0.8,
            "roi": [0, 0, 1280, 720],
            "action": "DoNothing",
            "next": [],
        },
    }
    for i, entry in enumerate(entries):
        nodes["Entry" if i == 0 else f"Entry{i}"] = {
            "algorithm": "MatchTemplate",
            "template": entry["template"],
            "maskRange": [1, 255],
            "templThreshold": 0.8,
            "roi": [0, 0, 1280, 720],
            "action": "ClickSelf",
            "rectMove": entry["rectMove"],
            "postDelay": 2000,
            "next": [node("ShopReady")],
        }
    return {node(key): value for key, value in nodes.items()}


async def prepare_shop_context(
    schedule,
    activity,
    *,
    now,
    store,
    profile,
    load_catalog,
    load_navigation=None,
    roi_cache_directory=None,
    depot=None,
) -> tuple[dict, ...]:
    if not activity.data.active_side_stories(datetime.fromtimestamp(now, BUSINESS_TIMEZONE)):
        return ()
    catalog = await load_catalog()
    plans = prepare_shops(
        catalog,
        activity,
        now=now,
        blacklist=schedule.shop_blacklist,
    )
    if load_navigation is not None:
        navigation = {}
        for plan in plans:
            navigation[plan["activity_id"]] = await load_navigation(catalog, plan["activity_id"])
        plans = prepare_shops(
            catalog, activity, now=now, navigation=navigation, blacklist=schedule.shop_blacklist
        )
    for plan in plans:
        if roi_cache_directory is not None:
            from maa_planner.daemon.tools.reward_roi import load_claim_roi, reward_roi_key

            plan["claim_roi"] = load_claim_roi(roi_cache_directory, reward_roi_key(plan))
        completed = store.completed_stages(profile, plan["activity_id"])
        plan["completed"] = sorted([list(v) for v in completed])
        plan["missing"] = [v for v in plan["required"] if tuple(v) not in completed]
        plan["eligibility"] = shop_eligibility(plan, completed, now)
        state = store.shop_state(profile, plan["activity_id"], plan["fingerprint"])
        plan["navigation_status"] = store.shop_navigation_state(plan).get("status", "untried")
        story = next(s for s in activity.data.side_stories if s.key == plan["key"])
        thresholds = dict(schedule.garbage_material_targets)
        plan["materials"] = garbage_materials(
            story,
            threshold=schedule.garbage_material_threshold,
            targets=thresholds,
            depot=depot,
        )
        plan["materials_complete"] = bool(plan["materials"]) and all(
            v["target"] is not None and v["stock"] is not None and v["stock"] >= v["target"]
            for v in plan["materials"]
        )
        plan["shop_completed"] = state.get("completed", False)
        plan["garbage"] = (
            plan["shop_completed"] and plan["eligibility"] == "ready" and plan["materials_complete"]
        )
    return plans


def validate_shop_plans(plans: object, tasks: list) -> None:
    if not isinstance(plans, list):
        raise ValueError("activity_shops 必须为阶段列表")
    for plan in plans:
        if not isinstance(plan, dict):
            raise ValueError("活动商店计划必须为对象")
        index = plan.get("after_task")
        if (
            type(index) is not int
            or not 0 <= index < len(tasks)
            or tasks[index]["type"] != "Fight"
            or is_annihilation_stage(tasks[index].get("params", {}).get("stage"))
        ):
            raise ValueError("商店阶段必须紧接一个 Fight 任务")
        for key in ("activity_id", "key", "name"):
            if not isinstance(plan.get(key), str) or not plan[key]:
                raise ValueError("商店阶段缺少活动身份")
        if not isinstance(plan.get("fingerprint"), str) or not re.fullmatch(
            "[0-9a-f]{64}", plan["fingerprint"]
        ):
            raise ValueError("商店阶段范围指纹无效")
        if not isinstance(plan.get("navigation"), str) or not re.fullmatch(
            r"Planner@Shop[0-9]+", plan["navigation"]
        ):
            raise ValueError("商店阶段私有导航无效")
        for key in ("catalog_complete", "garbage"):
            if type(plan.get(key)) is not bool:
                raise ValueError("商店阶段资格标记无效")
        if plan.get("navigation_status", "untried") not in {"untried", "succeeded", "disabled"}:
            raise ValueError("商店导航首次结果无效")
        for key in ("available_from", "available_until", "all_regions_from", "all_regions_until"):
            if type(plan.get(key)) not in (int, float) or not math.isfinite(plan[key]):
                raise ValueError("商店阶段有效期无效")
        for key in ("required", "completed"):
            values = plan.get(key)
            if not isinstance(values, list) or any(
                not isinstance(v, list)
                or len(v) != 2
                or not isinstance(v[0], str)
                or not v[0]
                or type(v[1]) is not bool
                for v in values
            ):
                raise ValueError("商店阶段关卡清单无效")
        if plan["catalog_complete"] and not plan["required"]:
            raise ValueError("完整活动不能没有关卡目标")
        validate_navigation_assets(plan)
        if not isinstance(plan.get("blacklist"), list) or any(
            not isinstance(v, str) or not v.strip() for v in plan["blacklist"]
        ):
            raise ValueError("商店商品黑名单无效")


def validate_navigation_assets(plan):
    from maa_planner.daemon.execution.png import decode_png, image_name

    images = plan.get("navigation_images", {})
    if not isinstance(images, dict):
        raise ValueError("活动导航图片必须为对象")
    for name, value in images.items():
        image_name(name)
        decode_png(value)
    for role in ("entry", "mission_entry"):
        entries = plan.get(role, [])
        if not isinstance(entries, list):
            raise ValueError("活动入口必须为模板列表")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("template"), str)
                or entry["template"] not in images
            ):
                raise ValueError("活动入口缺少冻结模板")
            rect = entry.get("rectMove")
            size = entry.get("size")
            if (
                not isinstance(size, list)
                or len(size) != 2
                or any(type(v) is not int or v <= 0 for v in size)
                or not isinstance(rect, list)
                or len(rect) != 4
                or any(type(v) is not int for v in rect)
                or min(rect[:2]) < 0
                or rect[2:] != [1, 1]
                or rect[0] >= size[0]
                or rect[1] >= size[1]
            ):
                raise ValueError("活动入口相对点击位置无效")
    ready = plan.get("shop_ready", [])
    if not isinstance(ready, list) or any(
        not isinstance(name, str) or name not in images for name in ready
    ):
        raise ValueError("商店就绪条件缺少冻结模板")
    if plan.get("entry") and not ready:
        raise ValueError("商店入口必须配套就绪模板")

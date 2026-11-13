"""Select one creator's whole collections using stage identity and owned formations."""

from __future__ import annotations

import asyncio
import copy
import re
import threading
from datetime import datetime

from skland_api.models.constants import OPERATOR_NAME_MAPPING_FIX

from maa_planner.daemon.constants import BUSINESS_TIMEZONE, COPILOT_API_URL, COPILOT_CREATOR_ID
from maa_planner.daemon.execution.copilot import MAX_REQUESTS, MAX_SCRIPTS, MAX_TOTAL, _Source
from maa_planner.daemon.execution.tasks import json_object
from maa_planner.daemon.planning.activity_scope import activity_scope
from maa_planner.daemon.planning.clearance import prepare_clearance
from maa_planner.daemon.planning.skland import collect_character_info


def observe_operators(cultivate: dict, player: dict) -> frozenset[str]:
    """The global name map alone never establishes ownership."""
    characters, metadata = cultivate.get("characters"), player.get("charInfoMap")
    if not isinstance(characters, list) or not isinstance(metadata, dict):
        raise ValueError("Skland 干员拥有数据缺失")
    names = {}
    for value in metadata.values():
        if not isinstance(value, dict):
            raise ValueError("Skland 干员名称表无效")
        identifier, name = value.get("id"), value.get("name")
        if not isinstance(identifier, str) or not isinstance(name, str) or not name:
            raise ValueError("Skland 干员名称表无效")
        names[identifier] = OPERATOR_NAME_MAPPING_FIX.get(identifier, name)
    owned, seen = set(), set()
    for entry in characters:
        identifier = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(identifier, str) or identifier not in names or identifier in seen:
            raise ValueError("Skland 干员拥有清单无效或无法关联名称")
        seen.add(identifier)
        owned.add(names[identifier])
    return frozenset(owned)


def resolve_formation(
    script: dict, owned: frozenset[str], allowance: int
) -> tuple[dict, list[str]]:
    """Match distinct owned operators to slots, then at most one support slot."""
    opers, groups = script.get("opers", []), script.get("groups", [])
    if (
        not isinstance(opers, list)
        or not isinstance(groups, list)
        or not ("opers" in script or "groups" in script)
    ):
        raise ValueError("作业缺少合法干员／干员组")
    slots = [[oper] for oper in opers]
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("opers"), list):
            raise ValueError("作业干员组无效")
        slots.append(group["opers"])
    for slot in slots:
        if not slot or any(
            not isinstance(oper, dict) or not isinstance(oper.get("name"), str) or not oper["name"]
            for oper in slot
        ):
            raise ValueError("作业干员要求无效")

    def assign(available):
        assigned: dict[str, int] = {}

        def match(index, seen):
            for oper in slots[index]:
                name = oper["name"]
                if name not in available or name in seen:
                    continue
                seen.add(name)
                if name not in assigned or match(assigned[name], seen):
                    assigned[name] = index
                    return True
            return False

        for index in range(len(slots)):
            match(index, set())
        return {index: name for name, index in assigned.items()}

    chosen = assign(owned)
    if len(chosen) < len(slots) and allowance:
        # An owned-only maximum matching may leave a slot containing only an
        # already-used owned operator. Reassign it before choosing support.
        alternatives = dict.fromkeys(
            o["name"] for slot in slots for o in slot if o["name"] not in owned
        )
        for name in alternatives:
            candidate = assign(owned | {name})
            if len(candidate) == len(slots):
                chosen = candidate
                break
    if len(chosen) < len(slots):
        unavailable = [
            "/".join(o["name"] for o in slot)
            for index, slot in enumerate(slots)
            if index not in chosen
        ]
        raise ValueError(f"无法以至多 {allowance} 名助战组成不重复阵容：{'、'.join(unavailable)}")
    missing = [name for name in chosen.values() if name not in owned]
    fixed = copy.deepcopy(script)
    for index, group in enumerate(fixed.get("groups", []), len(opers)):
        group["opers"] = [next(o for o in group["opers"] if o["name"] == chosen[index])]
    return fixed, missing


def query_sets(settings, cancel: threading.Event) -> tuple[dict, ...]:
    source = _Source(cancel, settings.cache_directory / "copilot")
    output, seen = [], set()
    for page in range(1, 21):
        data = source.post(
            COPILOT_API_URL + "/set/query",
            {
                "page": page,
                "limit": 50,
                "creator_id": COPILOT_CREATOR_ID,
                "only_following": False,
                "desc": True,
            },
        )
        rows, more = data.get("data"), data.get("has_next")
        if not isinstance(rows, list) or type(more) is not bool or (more and not rows):
            raise ValueError("作业集查询分页格式无效")
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or row["id"] <= 0
                or row.get("creator_id") != COPILOT_CREATOR_ID
                or not isinstance(row.get("name"), str)
                or not row["name"]
                or row["id"] in seen
            ):
                raise ValueError("作业集查询身份无效或分页重复")
            seen.add(row["id"])
            output.append(row)
        # API's data.page is the page count, not the requested page number.
        if not more:
            return tuple(output)
    raise ValueError("作业集查询超过 20 页，不使用不完整搜索结果")


def shared_formation(
    scripts: tuple[tuple[int, dict], ...], owned: frozenset[str]
) -> tuple[dict[int, dict] | None, str | None]:
    """Union an entirely owned collection without changing per-battle skill use."""
    fixed = {}
    union = {}
    for identifier, script in scripts:
        try:
            document, _ = resolve_formation(script, owned, 0)
        except ValueError:
            return None, "operators_not_owned"
        fixed[identifier] = document
        for oper in document.get("opers", []) + [
            group["opers"][0] for group in document.get("groups", [])
        ]:
            name = oper["name"]
            if name not in union:
                union[name] = copy.deepcopy(oper)
                continue
            previous = union[name]
            for key in ("skill", "role"):
                a, b = previous.get(key), oper.get(key)
                if a and b and a != b:
                    return None, "formation_conflict"
                if b:
                    previous[key] = b
            requirements = previous.setdefault("requirements", {})
            incoming = oper.get("requirements", {})
            a, b = requirements.get("module", -1), incoming.get("module", -1)
            if a >= 0 and b >= 0 and a != b:
                return None, "formation_conflict"
            if b >= 0:
                requirements["module"] = b
            # Higher elite requirements dominate lower-elite level thresholds.
            if (incoming.get("elite", 0), incoming.get("level", 0)) > (
                requirements.get("elite", 0),
                requirements.get("level", 0),
            ):
                for key in ("elite", "level"):
                    requirements[key] = incoming.get(key, 0)
            for key in ("skill_level", "potentiality"):
                if key in incoming:
                    requirements[key] = max(requirements.get(key, 0), incoming[key])
    if len(union) > 12:
        return None, "formation_too_large"
    if any(
        group["name"] in union and group["name"] != group["opers"][0]["name"]
        for document in fixed.values()
        for group in document.get("groups", [])
    ):
        return None, "formation_conflict"
    for document in fixed.values():
        local = document.setdefault("opers", []) + [
            group["opers"][0] for group in document.get("groups", [])
        ]
        present = {oper["name"] for oper in local}
        for oper in local:
            common = union[oper["name"]]
            for key in ("skill", "role", "requirements"):
                if key in common:
                    oper[key] = copy.deepcopy(common[key])
        for name, oper in union.items():
            if name not in present:
                added = copy.deepcopy(oper)
                # An extra roster member must not inherit another stage's
                # automatic-skill policy. Existing entries keep their own.
                added.pop("skill_usage", None)
                added.pop("skill_times", None)
                document["opers"].append(added)
    return fixed, None


def current_activity(catalog, activity, now, completed_for=None):
    stories = sorted(
        activity.data.active_side_stories(datetime.fromtimestamp(now, BUSINESS_TIMEZONE)),
        key=lambda story: story.ends_at,
    )
    if not stories:
        raise ValueError("当前没有开放活动")
    first = None
    for story in stories:
        matches = [
            v
            for v in catalog["activity_table"]["basicInfo"].values()
            if v.get("name") == story.name and v["startTime"] <= now <= v["endTime"]
        ]
        if len(matches) != 1:
            raise ValueError("无法唯一关联当前活动")
        identifier = matches[0]["id"]
        first = first or (story, identifier)
        if completed_for is None:
            return story, identifier
        scope = activity_scope(catalog, identifier, now=now)
        known = completed_for(identifier)
        if not scope["catalog_complete"] or not all(tuple(v) in known for v in scope["required"]):
            return story, identifier
    assert first is not None
    return first


def select_sets(
    settings, activity, catalog, owned, completed, collections, cancel, *, now, activity_id=None
):
    if activity_id is None:
        story, activity_id = current_activity(catalog, activity, now)
    else:
        story = next(
            s
            for s in activity.data.active_side_stories(
                datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
            )
            if s.name == catalog["activity_table"]["basicInfo"][activity_id]["name"]
        )
    base = prepare_clearance((), catalog, activity, now=now, activity_id=activity_id)
    from maa_planner.daemon.planning.tutorials import add_tutorial_barriers, is_tutorial

    stages = catalog["stage_table"]["stages"]
    regions: dict[str, set[tuple[str, bool]]] = {}
    prefix = None
    for identifier, raid in base[0]["required"]:
        stage = stages[identifier]
        code = stage.get("code", "")
        match = re.fullmatch(r"([A-Za-z0-9]+)-(?:((?:EX|S|TR))-)?[0-9]+", code)
        if not match or not stage.get("levelId"):
            continue  # Core owns story navigation; story-only stages need no separate task.
        prefix = match[1] if prefix is None else prefix
        if match[1] != prefix:
            raise ValueError("活动关卡缩写不一致")
        zone = catalog["zone_table"].get("zoneValidInfo", {}).get(stage["zoneId"], {})
        window = catalog["stage_table"].get("stageValidInfo", {}).get(identifier, {})
        if not (zone.get("startTs", float("inf")) <= now <= zone.get("endTs", -1)):
            continue
        if window.get("startTs", 0) > now:
            continue
        if (identifier, raid) not in completed:
            regions.setdefault("normal" if match[2] in (None, "TR") else match[2], set()).add(
                (identifier, raid)
            )
    base[0]["completed"] = [list(v) for v in sorted(completed)]
    base[0]["selection"] = []
    if not regions:
        return add_tutorial_barriers(base, catalog, now=now)
    candidates = [
        row
        for row in collections
        if re.search(
            rf"(?<![A-Za-z0-9]){re.escape(prefix or '')}(?![A-Za-z0-9])", row["name"], re.I
        )
        or story.name in row["name"]
    ]
    candidates.sort(key=lambda row: ("一摆到底" not in row["name"], -row["id"]))
    source = _Source(cancel, settings.cache_directory / "copilot")
    downloaded = {}
    selected, support, decisions, formation_groups = {}, {}, [], {}
    for region in ("normal", "EX", "S"):
        needed = regions.get(region)
        if not needed:
            continue
        rejected = []
        for row in candidates:
            try:
                if row["id"] not in downloaded:
                    ids = source.get(f"{COPILOT_API_URL}/set/get?id={row['id']}").get("copilot_ids")
                    if (
                        not isinstance(ids, list)
                        or not ids
                        or len(ids) > MAX_SCRIPTS
                        or any(type(i) is not int or i <= 0 for i in ids)
                    ):
                        raise ValueError("作业集 ID 清单无效")
                    scripts = []
                    for identifier in dict.fromkeys(ids):
                        content = source.get(f"{COPILOT_API_URL}/copilot/get/{identifier}").get(
                            "content"
                        )
                        if not isinstance(content, str):
                            raise ValueError("作业缺少 JSON content")
                        script = json_object(content)
                        if not isinstance(script.get("actions"), list):
                            raise ValueError("不是普通 Copilot 作业")
                        scripts.append((identifier, script))
                    downloaded[row["id"]] = tuple(scripts)
                scripts = downloaded[row["id"]]
                prepare_clearance(
                    scripts, catalog, activity, now=now
                )  # Validate collection identity.
                usable, covered, borrowed, unavailable = {}, set(), {}, {}
                needed_codes = {stages[stage]["code"] for stage, _ in needed}
                for identifier, script in scripts:
                    if stages[script["stage_name"]]["code"] not in needed_codes:
                        continue
                    try:
                        fixed, missing = resolve_formation(
                            script, owned, 0 if region == "normal" else 1
                        )
                    except ValueError as error:
                        unavailable[identifier] = str(error)
                        continue
                    usable[identifier] = fixed
                    borrowed[identifier] = missing
                if usable:
                    mapped, _, _ = prepare_clearance(
                        tuple(usable.items()), catalog, activity, now=now
                    )
                    covered = {(v["stage_id"], v["is_raid"]) for v in mapped["targets"]}
                battle_needed = {target for target in needed if not is_tutorial(stages[target[0]])}
                if not battle_needed <= covered or not usable:
                    detail = "; ".join(f"{key}: {value}" for key, value in unavailable.items())
                    raise ValueError(
                        "未覆盖全部待打目标或阵容不适用" + (": " + detail if detail else "")
                    )
                reuse, reuse_reason = None, "not_one_placement"
                if "一摆到底" in row["name"]:
                    reuse, reuse_reason = shared_formation(scripts, owned)
                if reuse is not None:
                    usable = {identifier: reuse[identifier] for identifier in usable}
                    formation_groups.update(
                        {identifier: f"set-{row['id']}" for identifier in usable}
                    )
                selected.update(usable)
                support.update(
                    {identifier: 0 if region == "normal" else 1 for identifier in usable}
                )
                decisions.append(
                    {
                        "region": region,
                        "status": "selected",
                        "set_id": row["id"],
                        "name": row["name"],
                        "missing_operators": {str(k): v for k, v in borrowed.items()},
                        "missing_tutorials": [
                            stage
                            for stage, raid in sorted(needed - covered)
                            if not raid and is_tutorial(stages[stage])
                        ],
                        "formation_reuse": reuse is not None,
                        "formation_reuse_reason": reuse_reason,
                        "rejected": rejected,
                    }
                )
                break
            except (OSError, ValueError, KeyError, TypeError) as error:
                if cancel.is_set():
                    raise RuntimeError("自动作业搜索已取消") from error
                if source.total >= MAX_TOTAL or source.requests >= MAX_REQUESTS:
                    raise ValueError("自动作业搜索超过下载限额") from error
                rejected.append({"set_id": row["id"], "reason": str(error)})
        else:
            decisions.append({"region": region, "status": "no_usable_set", "rejected": rejected})
    if selected:
        base = prepare_clearance(tuple(selected.items()), catalog, activity, now=now)
        base[0]["completed"] = [list(v) for v in sorted(completed)]
        for target in base[0]["targets"]:
            target["support_unit_usage"] = support[target["script_id"]]
            if target["script_id"] in formation_groups:
                target["formation_group"] = formation_groups[target["script_id"]]
                target["support_unit_usage"] = 0
    base[0]["selection"] = decisions
    base = add_tutorial_barriers(base, catalog, now=now)
    if len(base[0]["targets"]) > 200:
        raise ValueError("自动通关超过 200 个目标")
    return base


async def prepare_automatic(
    settings, profile, info, activity, store, load_catalog, load_sets, cancel, *, now
):
    if info is None:
        info, _ = await collect_character_info(settings, profile)
    owned = observe_operators(info.cultivate, info.player_info)
    catalog = await load_catalog()
    _, activity_id = current_activity(
        catalog, activity, now, lambda identifier: store.completed_stages(profile.name, identifier)
    )
    completed = store.completed_stages(profile.name, activity_id)
    full = prepare_clearance((), catalog, activity, now=now, activity_id=activity_id)[0]
    collections = (
        ()
        if all(
            tuple(v) in completed
            or catalog["stage_table"]["stages"][v[0]].get("isStoryOnly") is True
            for v in full["required"]
        )
        else await load_sets()
    )
    return await asyncio.to_thread(
        select_sets,
        settings,
        activity,
        catalog,
        owned,
        completed,
        collections,
        cancel,
        now=now,
        activity_id=activity_id,
    )

"""Observe current base production and freeze configured tasks for one occurrence."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.execution.tasks import CoreTasks, json_object, validate_resource_tasks
from maa_planner.daemon.planning.activity import ActivitySnapshot
from maa_planner.daemon.planning.activity_shop import shop_navigation
from maa_planner.daemon.planning.annihilation import (
    AnnihilationDecision,
    WeeklyProgress,
    decide_annihilation,
    is_annihilation_stage,
)
from maa_planner.daemon.planning.expert import (
    InfrastDecision,
    InfrastMode,
    ShardRecipe,
    decide_infrast,
)
from maa_planner.daemon.planning.farming import FarmingDecision, decide_farming
from maa_planner.daemon.planning.schedule import DailySchedule, clock_minute


@dataclass(frozen=True, slots=True)
class InfrastObservation:
    depot: dict[str, int]
    mode: InfrastMode
    recipe: ShardRecipe | None
    store_ts: float


def observe_depot(cultivate: dict) -> dict[str, int]:
    """A missing item is zero only after the whole inventory parsed successfully."""
    items = cultivate.get("items")
    if not isinstance(items, list):
        raise ValueError("Skland 仓库数据缺失，不能当作空仓库")
    depot = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("Skland 仓库物品格式错误")
        value = item.get("count")
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            value = int(value)
        if type(value) is not int or value < 0 or item["id"] in depot:
            raise ValueError("Skland 仓库数量无效或物品重复")
        depot[item["id"]] = value
    return depot


def observe_infrast(cultivate: dict, player: dict, *, now: float) -> InfrastObservation:
    """Read actual production, rather than treating a selected plan as applied."""
    timestamp = player.get("status", {}).get("storeTs")
    if (
        type(timestamp) not in (int, float)
        or not math.isfinite(timestamp)
        or not 0 < timestamp <= now
    ):
        raise ValueError("Skland 基建快照缺失或时间异常，未生成 expert 任务")
    depot = observe_depot(cultivate)
    try:
        factories = player["building"]["manufactures"]
        trades = player["building"]["tradings"]
        formulas = player["manufactureFormulaInfoMap"]
        outputs = [formulas[str(factory["formulaId"])] for factory in factories]
        products = Counter(formula["itemId"] for formula in outputs)
        strategies = Counter(trade["strategy"] for trade in trades)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Skland 基建产物/配方数据不完整") from error
    recipe = None
    if products == {"3141": 1, "3003": 2, "2003": 1} and strategies == {
        "O_GOLD": 1,
        "O_DIAMOND": 1,
    }:
        mode = InfrastMode.ORUNDUM
        shard = next(formula for formula in outputs if formula["itemId"] == "3141")
        costs = {item["id"] for item in shard.get("costs", [])}
        if ("30012" in costs) == ("30062" in costs):
            raise ValueError("无法识别当前源石碎片配方使用固源岩还是装置")
        recipe = ShardRecipe.ORIROCK if "30012" in costs else ShardRecipe.DEVICE
    elif products == {"3003": 2, "2003": 2} and strategies == {"O_GOLD": 1, "O_DIAMOND": 1}:
        mode = InfrastMode.SELL_ONLY
    elif products == {"3003": 3, "2003": 1} and strategies == {"O_GOLD": 2}:
        mode = InfrastMode.THREE_GOLD
    elif products == {"3003": 2, "2003": 2} and strategies == {"O_GOLD": 2}:
        mode = InfrastMode.NORMAL
    else:
        raise ValueError("当前基建产物不属于约定的四种 243 配置，不能猜测模式")
    return InfrastObservation(depot, mode, recipe, float(timestamp))


def select_plan(document: dict, now: datetime, explicit: int | None = None) -> int:
    plans = document.get("plans")
    if not isinstance(plans, list) or not plans or any(not isinstance(p, dict) for p in plans):
        raise ValueError("原生基建文件必须包含非空 plans")
    if explicit is not None:
        if type(explicit) is not int or not 0 <= explicit < len(plans):
            raise ValueError("基建 plan_index 超出 plans 范围")
        return explicit
    if len(plans) == 1 and "period" not in plans[0]:
        return 0
    minute = now.hour * 60 + now.minute
    matches = []
    for index, plan in enumerate(plans):
        periods = plan.get("period")
        if not isinstance(periods, list) or not periods:
            raise ValueError("自动选择基建子计划要求每个 plan 都有非空 period")
        matched = False
        for period in periods:
            if not isinstance(period, list) or len(period) != 2:
                raise ValueError("基建 period 必须为 [HH:MM, HH:MM] 区间列表")
            start, end = map(clock_minute, period)
            matched |= start <= minute <= end if start <= end else minute >= start or minute <= end
        if matched:
            matches.append(index)
    if len(matches) != 1:
        raise ValueError("当前时间必须恰好匹配一个基建 period，不能猜测子计划")
    return matches[0]


@dataclass(frozen=True, slots=True)
class DailyPlan:
    tasks: CoreTasks | None
    decisions: tuple[InfrastDecision, ...] = ()
    annihilation: AnnihilationDecision | None = None
    clearance: dict | None = None
    farming: tuple[FarmingDecision, ...] = ()
    shops: tuple[dict, ...] = ()
    annihilation_rescue: dict | None = None
    failures: tuple[dict, ...] = ()


def build_daily_plan(
    schedule: DailySchedule,
    *,
    now: float,
    observation: InfrastObservation | None = None,
    activity: ActivitySnapshot | None = None,
    weekly_progress: WeeklyProgress | None = None,
    clearance: tuple[dict, dict, dict] | None = None,
    depot: dict[str, int] | None = None,
    shops: tuple[dict, ...] = (),
    skip_farming: bool = False,
    annihilation_rescue: tuple[dict, dict, dict] | None = None,
    pending: frozenset[str] | None = None,
    processing: dict | None = None,
    manual_entry: str | None = None,
) -> DailyPlan:
    """Read source files once here; runners receive immutable JSON attachments."""
    local = datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
    tasks, attachments, decisions = [], {}, []
    business_ids = []
    session = json.loads(schedule.session_json)
    failures = []
    resource_tasks = {}
    resource_images = {}
    annihilation = None
    clearance_plan = None
    farming = []
    shop_phases: list[dict] = []
    processing_phases = []
    orundum_plan = None
    for index, task in enumerate(schedule.tasks):
        if pending is not None and f"task:{index}" not in pending:
            continue
        if not task.enabled or local.isoweekday() not in task.weekdays:
            continue
        if orundum_plan is not None and task.strategy == "expert" and task.task_type == "Infrast":
            failures.append(
                {
                    "id": f"task:{index}",
                    "status": "error",
                    "error": {"kind": "preparation", "reason": "fresh_observation_required"},
                }
            )
            continue
        if (
            skip_farming
            and task.task_type == "Fight"
            and not is_annihilation_stage(json.loads(task.params_json).get("stage"))
        ):
            failures.append(
                {
                    "id": f"task:{index}",
                    "status": "error",
                    "error": {"kind": "preparation", "reason": "自动通关准备失败，未执行刷理智"},
                }
            )
            continue
        if task.annihilation_expert:
            if activity is None:
                raise ValueError("剿灭 expert 需要共享准备中的活动快照")
            annihilation = decide_annihilation(
                now=now,
                activity=activity,
                progress=weekly_progress,
            )
            if not annihilation.run:
                continue
        if task.resource_file is not None:
            nodes = validate_resource_tasks(
                json_object(task.resource_file.read_text(encoding="utf-8-sig"))
            )
            for name, node in nodes.items():
                if name in resource_tasks and resource_tasks[name] != node:
                    raise ValueError(f"同一次运行存在冲突的私有任务节点: {name}")
                resource_tasks[name] = node
        for name, source in task.resource_images:
            from maa_planner.daemon.execution.png import encode_png

            content = source.read_bytes()
            filename = "image-" + hashlib.sha256(content).hexdigest() + ".png"
            if name in resource_images and resource_images[name] != "@file:" + filename:
                raise ValueError(f"同一次运行存在冲突的私有图片: {name}")
            attachments[filename] = encode_png(content)
            resource_images[name] = "@file:" + filename
        params = json.loads(task.params_json)
        if task.farming_expert:
            if activity is None or depot is None:
                raise ValueError("活动刷图 expert 需要活动快照和成功解析的 Skland 仓库")
            selection = decide_farming(
                activity,
                depot,
                now=now,
                task_name=task.name,
                default_stage=params.get("stage", "1-7"),
                garbage_activities=tuple(s["key"] for s in shops if s["garbage"]),
                chip_targets=task.chip_targets,
                fallback_stages=task.fallback_stages,
            )
            farming.append(selection)
            params["stage"] = selection.stage
        if (
            clearance is not None
            and clearance_plan is None
            and task.task_type == "Fight"
            and not is_annihilation_stage(params.get("stage"))
        ):
            clearance_plan = {**clearance[0], "before_task": len(tasks)}
            attachments.update(clearance[1])
            for name, node in clearance[2].items():
                if name in resource_tasks and resource_tasks[name] != node:
                    raise ValueError(f"自动通关私有节点冲突: {name}")
                resource_tasks[name] = node
        source = task.file
        if task.strategy == "expert" and task.task_type == "Infrast":
            if observation is None:
                raise ValueError("基建 expert 需要库存和实际配方快照")
            if manual_entry is not None and orundum_plan is None:
                decision = InfrastDecision(
                    InfrastMode.ORUNDUM, dict(task.files)["orundum"], "手动搓玉请求：首次直接进入"
                )
            else:
                decision = decide_infrast(
                    observation.depot,
                    dict(task.files),
                    current_mode=observation.mode,
                    recipe=observation.recipe,
                )
            decisions.append(decision)
            source = decision.file
        if source is not None:
            content = json_object(source.read_text(encoding="utf-8-sig"))
            name = f"task-{index}.json"
            attachments[name] = content
            params["filename"] = "@file:" + name
            if task.task_type == "Infrast":
                if params.get("mode", 10000) != 10000:
                    raise ValueError("使用原生基建文件时 mode 必须为 10000")
                params["mode"] = 10000
                params["plan_index"] = select_plan(content, local, task.plan_index)
                if manual_entry is not None and orundum_plan is None and task.strategy == "expert":
                    from maa_planner.daemon.execution.orundum import production_targets

                    orundum_plan = {
                        "intent_id": manual_entry,
                        "business_id": f"task:{index}",
                        "targets": production_targets(content, params["plan_index"]),
                    }
        if task.task_type == "Infrast" and any(o != "九色鹿" for o in task.processing_operators):
            from maa_planner.daemon.planning.processing import processing_phase

            if processing is None:
                raise ValueError("指定干员加工需要库存与配方准备结果")
            if "error" not in processing:
                phase, files, nodes = processing_phase(
                    task.processing_operators,
                    task.processing_targets,
                    task.processing_strategy,
                    processing,
                    len(tasks),
                )
                processing_phases.append(phase)
                attachments.update(files)
                resource_tasks.update(nodes)
        tasks.append({"name": task.name, "type": task.task_type, "params": params})
        business_ids.append(f"task:{index}")
        if task.task_type == "Fight" and not is_annihilation_stage(params.get("stage")):
            for shop in shops:
                if (
                    shop.get("shop_completed", False)
                    or shop["garbage"]
                    or shop.get("navigation_status") == "disabled"
                ):
                    continue
                prefix = f"Planner@Shop{len(shop_phases)}"
                shop_phases.append({**shop, "after_task": len(tasks) - 1, "navigation": prefix})
                if shop["entry"]:
                    for name, node in shop_navigation(shop, prefix).items():
                        if name in resource_tasks and resource_tasks[name] != node:
                            raise ValueError(f"活动商店私有节点冲突: {name}")
                        resource_tasks[name] = node
    # Freeze generated images with the same attachment mechanism as user PNGs.
    for shop in shops:
        for name, content in shop.get("navigation_images", {}).items():
            from maa_planner.daemon.execution.png import decode_png

            filename = "image-" + hashlib.sha256(decode_png(content)).hexdigest() + ".png"
            reference = "@file:" + filename
            if name in resource_images and resource_images[name] != reference:
                raise ValueError(f"活动导航私有图片冲突: {name}")
            attachments[filename] = content
            resource_images[name] = reference
    from maa_planner.daemon.planning.activity_rewards import reward_task

    # Rewards belong to the reward phase, even if farming/full-clear is skipped.
    position = next(
        (i for i, t in enumerate(tasks) if t["type"] in {"Award", "CloseDown"}), len(tasks)
    )
    rewards = []
    reward_ids = []
    for shop in shops:
        if shop.get("mission_entry") and (
            pending is None or "reward:" + shop["activity_id"] in pending
        ):
            try:
                task, nodes = reward_task(shop, len(rewards))
            except ValueError as error:
                failures.append(
                    {
                        "id": "reward:" + shop["activity_id"],
                        "status": "error",
                        "error": {"kind": "preparation", "reason": str(error)},
                    }
                )
                continue
            if resource_tasks.keys() & nodes.keys():
                raise ValueError("活动奖励私有节点冲突")
            rewards.append(task)
            reward_ids.append("reward:" + shop["activity_id"])
            resource_tasks.update(nodes)
    if rewards:
        tasks[position:position] = rewards
        business_ids[position:position] = reward_ids
        if clearance_plan is not None and clearance_plan["before_task"] >= position:
            clearance_plan["before_task"] += len(rewards)
        for phase in processing_phases:
            if phase["before_task"] >= position:
                phase["before_task"] += len(rewards)
        for shop in shop_phases:
            if shop["after_task"] >= position:
                shop["after_task"] += len(rewards)
    if (
        not tasks
        and not failures
        and not (pending and any(key.startswith("lifecycle:") for key in pending))
    ):
        return DailyPlan(None, annihilation=annihilation)
    if not tasks and not session:
        return DailyPlan(None, annihilation=annihilation, failures=tuple(failures))
    if pending is not None:
        session.setdefault("startup", {})
    if "startup" in session:
        tasks.insert(
            0,
            {
                "type": "StartUp",
                "name": "准备游戏会话",
                "params": {
                    "client_type": schedule.client_type or "Official",
                    "start_game_enabled": True,
                    **session["startup"],
                    **({"start_game_enabled": True} if pending is not None else {}),
                },
            },
        )
        business_ids.insert(0, "lifecycle:startup")
        if clearance_plan is not None:
            clearance_plan["before_task"] += 1
        for phase in processing_phases:
            phase["before_task"] += 1
        for shop in shop_phases:
            shop["after_task"] += 1
    if "closedown" in session:
        tasks.append(
            {
                "type": "CloseDown",
                "name": "结束游戏会话",
                "params": {
                    "client_type": schedule.client_type or "Official",
                    **session["closedown"],
                },
            }
        )
        business_ids.append("lifecycle:closedown")
    document = {"tasks": tasks, "business_ids": business_ids}
    if orundum_plan is not None:
        document["infrast_orundum"] = orundum_plan
    if failures:
        document["business_failures"] = failures
    rescue_context = None
    if annihilation_rescue is not None and any(
        t["type"] == "Fight" and is_annihilation_stage(t.get("params", {}).get("stage"))
        for t in tasks
    ):
        rescue_context, rescue_files, rescue_resources = annihilation_rescue
        if (
            attachments.keys() & rescue_files.keys()
            or resource_tasks.keys() & rescue_resources.keys()
        ):
            raise ValueError("剿灭补救私有资源冲突")
        attachments.update(rescue_files)
        resource_tasks.update(rescue_resources)
        document["annihilation_copilot"] = rescue_context
    if processing_phases:
        document["infrast_processing"] = processing_phases
    if clearance_plan is not None:
        document["activity_copilot"] = clearance_plan
    if resource_tasks:
        document["resource_tasks"] = resource_tasks
    if resource_images:
        document["resource_images"] = resource_images
    if shop_phases:
        document["activity_shops"] = shop_phases
    if schedule.client_type is not None:
        document["client_type"] = schedule.client_type
    return DailyPlan(
        CoreTasks(
            json.dumps(document, ensure_ascii=False), json.dumps(attachments, ensure_ascii=False)
        ),
        tuple(decisions),
        annihilation,
        clearance_plan,
        tuple(farming),
        shops,
        rescue_context,
        tuple(failures),
    )

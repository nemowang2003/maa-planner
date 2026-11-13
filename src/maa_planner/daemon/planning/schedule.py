"""Configured daily occurrences; no network, game control or timer ownership."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from maa_planner.daemon.adapters.windows import local_path
from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.execution.tasks import TASK_TYPES
from maa_planner.daemon.planning.activity_shop import DEFAULT_SHOP_BLACKLIST
from maa_planner.daemon.planning.annihilation import is_annihilation_stage
from maa_planner.daemon.planning.expert import InfrastMode
from maa_planner.daemon.planning.farming import DEFAULT_CHIP_TARGETS, MATERIAL_BLACKLIST
from maa_planner.daemon.planning.resource_stages import CHIP_ITEM_IDS, validate_fallback_stage

DAILY_TASK_TYPES = TASK_TYPES - {"StartUp", "CloseDown"}


@dataclass(frozen=True, slots=True)
class DailyTask:
    task_type: str
    name: str
    params_json: str
    enabled: bool = True
    weekdays: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)
    strategy: str = "fixed"
    file: Path | None = None
    files: tuple[tuple[str, Path], ...] = ()
    plan_index: int | None = None
    resource_file: Path | None = None
    garbage_activities: tuple[str, ...] = ()
    chip_targets: tuple[tuple[str, int], ...] = DEFAULT_CHIP_TARGETS
    fallback_stages: tuple[str, ...] = ("1-7",)
    resource_images: tuple[tuple[str, Path], ...] = ()
    processing_operators: tuple[str, ...] = ()
    processing_targets: tuple[tuple[str, int], ...] = ()
    processing_strategy: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def annihilation_expert(self) -> bool:
        return (
            self.strategy == "expert"
            and self.task_type == "Fight"
            and is_annihilation_stage(json.loads(self.params_json).get("stage"))
        )

    @property
    def farming_expert(self) -> bool:
        return (
            self.strategy == "expert" and self.task_type == "Fight" and not self.annihilation_expert
        )


@dataclass(frozen=True, slots=True)
class DailySchedule:
    id: str
    profiles: tuple[str, ...]
    times: tuple[int, ...]
    tasks: tuple[DailyTask, ...]
    enabled: bool = True
    weekdays: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)
    execution_timeout: float = 7200
    options_json: str = "{}"
    session_json: str = "{}"
    client_type: str | None = None
    shop_blacklist: tuple[str, ...] = DEFAULT_SHOP_BLACKLIST
    garbage_material_threshold: int | None = 100
    garbage_material_targets: tuple[tuple[str, int], ...] = ()

    def next_after(self, timestamp: float, *, inclusive: bool = False) -> float:
        now = datetime.fromtimestamp(timestamp, BUSINESS_TIMEZONE)
        for offset in range(8):
            day = now.date() + timedelta(days=offset)
            if day.isoweekday() not in self.weekdays:
                continue
            for minute in self.times:
                value = datetime(
                    day.year,
                    day.month,
                    day.day,
                    minute // 60,
                    minute % 60,
                    tzinfo=BUSINESS_TIMEZONE,
                ).timestamp()
                if value > timestamp or (inclusive and value == timestamp):
                    return value
        raise ValueError("日程没有下一次执行时间")


@dataclass(frozen=True, slots=True)
class ScheduledOccurrence:
    schedule_id: str
    profile: str
    scheduled_at: float
    expires_at: float
    identifier: str | None = None
    not_before: float | None = None

    @property
    def id(self) -> str:
        if self.identifier is not None:
            return self.identifier
        payload = json.dumps([self.schedule_id, self.profile, int(self.scheduled_at)])
        return "daily-" + hashlib.sha256(payload.encode()).hexdigest()[:40]


def clock_minute(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时间必须为 HH:MM（00:00 至 23:59）")
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _weekdays(value: object) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(day) is not int or not 1 <= day <= 7 for day in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("weekdays 必须是无重复的 1 至 7 列表（周一至周日）")
    return tuple(sorted(value))


def _positive(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} 必须是有限正数")
    return float(value)


def _boolean(row: dict, key: str) -> bool:
    value = row.get(key, True)
    if type(value) is not bool:
        raise ValueError(f"{key} 必须为布尔值")
    return value


def _json(value: object, name: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须为对象")
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{name} 必须可序列化为 JSON: {error}") from error


def parse_schedules(raw: object, profiles: set[str], config: Path) -> tuple[DailySchedule, ...]:
    if not isinstance(raw, list):
        raise ValueError("schedules 必须为日程列表")

    def source(value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError("任务文件必须为非空路径")
        path = local_path(value)
        return ((config.parent / path) if not path.is_absolute() else path).resolve()

    def task(row: object) -> DailyTask:
        if not isinstance(row, dict) or set(row) - {
            "type",
            "name",
            "params",
            "enabled",
            "weekdays",
            "strategy",
            "file",
            "files",
            "plan_index",
            "resource_file",
            "resource_images",
            "garbage_activities",
            "chip_targets",
            "fallback_stages",
            "processing",
        }:
            raise ValueError("日程任务包含未知字段")
        kind = row.get("type")
        if not isinstance(kind, str) or kind not in DAILY_TASK_TYPES:
            raise ValueError(f"未知日程任务类型: {kind}")
        name = row.get("name", kind)
        if not isinstance(name, str):
            raise ValueError("任务名称必须为字符串")
        processing = row.get("processing", {})
        if not isinstance(processing, dict) or set(processing) - {
            "operators",
            "targets",
            "strategy",
        }:
            raise ValueError("processing 仅接受 operators、targets、strategy")
        if "processing" in row and kind != "Infrast":
            raise ValueError("processing 仅用于 Infrast")
        operators = processing.get("operators", [])
        if (
            not isinstance(operators, list)
            or any(not isinstance(v, str) or not v or v != v.strip() for v in operators)
            or len(set(operators)) != len(operators)
        ):
            raise ValueError("processing.operators 必须为有序、不重复的非空干员名列表")
        processing_targets = processing.get("targets", {})
        if not isinstance(processing_targets, dict) or any(
            not isinstance(k, str) or not k or k != k.strip() or type(v) is not int or v < 0
            for k, v in processing_targets.items()
        ):
            raise ValueError("processing.targets 必须为材料名／T3／T4 到非负整数库存目标的映射")
        if operators and not processing_targets:
            raise ValueError("配置 processing.operators 时需要显式提供 processing.targets")
        processing_strategy = processing.get("strategy", {})
        if not isinstance(processing_strategy, dict) or any(
            name not in operators
            or not isinstance(tokens, list)
            or any(not isinstance(t, str) or not t or t != t.strip() or t == "-" for t in tokens)
            for name, tokens in processing_strategy.items()
        ):
            raise ValueError("processing.strategy 必须为列表内干员到材料／分组名称列表的映射")
        params = _json(row.get("params", {}), "params")
        if "filename" in json.loads(params):
            raise ValueError("日程任务通过 file/files 固定附件，不能在 params 中指定 filename")
        strategy = row.get("strategy", "fixed")
        if strategy not in ("fixed", "expert") or (
            strategy == "expert" and kind not in {"Infrast", "Fight"}
        ):
            raise ValueError("strategy 仅接受 fixed；Infrast/Fight 可选择 expert")
        if strategy == "expert" and kind == "Fight":
            values = json.loads(params)
            if "stage" in values and (not isinstance(values["stage"], str) or not values["stage"]):
                raise ValueError("Fight expert 的 stage 必须为非空字符串")
            if is_annihilation_stage(values.get("stage")) and (
                type(values.get("times")) is not int or values["times"] <= 0
            ):
                raise ValueError("剿灭 expert 必须显式指定正整数 times，避免无限尝试")
            if "file" in row:
                raise ValueError("Fight expert 不接受 file")
        ordinary_expert = (
            strategy == "expert"
            and kind == "Fight"
            and not is_annihilation_stage(json.loads(params).get("stage"))
        )
        targets = row.get("chip_targets", dict(DEFAULT_CHIP_TARGETS) if ordinary_expert else {})
        if not isinstance(targets, dict) or any(
            item not in CHIP_ITEM_IDS or type(count) is not int or count < 0
            for item, count in targets.items()
        ):
            raise ValueError("chip_targets 必须为可直接掉落的芯片 ID 到非负整数库存目标的映射")
        # Preserve the previously configured default stage until a list is supplied.
        fallback = row.get(
            "fallback_stages",
            [json.loads(params).get("stage", "1-7")] if ordinary_expert else ["1-7"],
        )
        if not isinstance(fallback, list) or not fallback:
            raise ValueError("fallback_stages 必须为非空关卡列表")
        fallback = [validate_fallback_stage(stage) for stage in fallback]
        if len(set(fallback)) != len(fallback):
            raise ValueError("fallback_stages 不能重复")
        if {"chip_targets", "fallback_stages"} & row.keys() and not ordinary_expert:
            raise ValueError("chip_targets/fallback_stages 仅用于普通 Fight expert")
        garbage = row.get("garbage_activities", [])
        if (
            not isinstance(garbage, list)
            or any(not isinstance(key, str) or not key for key in garbage)
            or len(set(garbage)) != len(garbage)
        ):
            raise ValueError("garbage_activities 必须为无重复的非空活动 key 列表")
        if "garbage_activities" in row and not (
            strategy == "expert"
            and kind == "Fight"
            and not is_annihilation_stage(json.loads(params).get("stage"))
        ):
            raise ValueError("garbage_activities 仅用于普通 Fight expert")
        if garbage:
            raise ValueError("garbage_activities 已由自动双条件判定替代，请删除手工标记")
        sources = row.get("files", {})
        if not isinstance(sources, dict):
            raise ValueError("files 必须为四类基建文件映射")
        if strategy == "expert" and kind == "Infrast":
            if set(sources) != set(InfrastMode) or "file" in row:
                raise ValueError("基建 expert 必须恰好提供四类 files，不能同时指定 file")
        elif sources:
            raise ValueError("只有基建 expert 使用 files")
        index = row.get("plan_index", "auto")
        if index != "auto" and (type(index) is not int or index < 0):
            raise ValueError("plan_index 必须为非负整数或 auto")
        if "plan_index" in row and kind != "Infrast":
            raise ValueError("plan_index 仅用于 Infrast")
        if ("file" in row or sources) and "plan_index" in json.loads(params):
            raise ValueError("有基建文件时请使用任务的 plan_index 字段")
        images = row.get("resource_images", {})
        if not isinstance(images, dict):
            raise ValueError("resource_images 必须为私有图片名到路径的映射")
        from maa_planner.daemon.execution.png import image_name

        for template_name in images:
            image_name(template_name)
        return DailyTask(
            task_type=kind,
            name=name,
            params_json=params,
            enabled=_boolean(row, "enabled"),
            weekdays=_weekdays(row.get("weekdays", list(range(1, 8)))),
            strategy=strategy,
            file=source(row["file"]) if "file" in row else None,
            files=tuple((key, source(value)) for key, value in sources.items()),
            plan_index=None if index == "auto" else index,
            resource_file=source(row["resource_file"]) if "resource_file" in row else None,
            garbage_activities=tuple(garbage),
            chip_targets=tuple(targets.items()),
            fallback_stages=tuple(fallback),
            resource_images=tuple((name, source(value)) for name, value in images.items()),
            processing_operators=tuple(operators),
            processing_targets=tuple(processing_targets.items()),
            processing_strategy=tuple(
                (name, tuple(tokens)) for name, tokens in processing_strategy.items()
            ),
        )

    schedules = []
    ids = set()
    for row in raw:
        if not isinstance(row, dict) or set(row) - {
            "id",
            "profiles",
            "times",
            "tasks",
            "enabled",
            "weekdays",
            "execution_timeout",
            "options",
            "session",
            "client_type",
            "shop_blacklist",
            "garbage_material_threshold",
            "garbage_material_targets",
        }:
            raise ValueError("日程包含未知字段")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            raise ValueError("日程 id 必须为 1 至 80 位字母、数字、下划线或连字符")
        if identifier in ids:
            raise ValueError(f"重复的日程 id: {identifier}")
        ids.add(identifier)
        targets = row.get("profiles")
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(name, str) or name not in profiles for name in targets)
            or len(set(targets)) != len(targets)
        ):
            raise ValueError("日程 profiles 必须引用已配置且不重复的账号")
        times = row.get("times")
        if not isinstance(times, list) or not times:
            raise ValueError("日程 times 必须为非空时间列表")
        minutes = tuple(sorted(clock_minute(value) for value in times))
        if len(set(minutes)) != len(minutes):
            raise ValueError("日程 times 不能重复")
        tasks = row.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("日程必须明确配置非空 tasks")
        options = _json(row.get("options", {}), "options")
        decoded = json.loads(options)
        if (
            set(decoded) - {"static_options", "instance_options", "behavior"}
            or any(not isinstance(value, dict) for value in decoded.values())
            or decoded.get("instance_options", {}).get("kill_adb_on_exit")
        ):
            raise ValueError("日程 options 仅接受识别、实例和行为选项，禁止 kill_adb_on_exit")
        client = row.get("client_type")
        if client is not None and (not isinstance(client, str) or not client):
            raise ValueError("client_type 必须为非空字符串")
        session = row.get("session", {})
        if (
            not isinstance(session, dict)
            or set(session) - {"startup", "closedown"}
            or any(not isinstance(params, dict) for params in session.values())
        ):
            raise ValueError("session 仅接受 startup／closedown Core 参数对象")
        parsed_tasks = tuple(task(t) for t in tasks)
        blacklist = row.get("shop_blacklist", list(DEFAULT_SHOP_BLACKLIST))
        threshold = row.get("garbage_material_threshold", 100)
        material_targets = row.get("garbage_material_targets", {})
        if threshold is not None and (type(threshold) is not int or threshold < 0):
            raise ValueError("garbage_material_threshold 必须为非负整数或省略")
        if not isinstance(material_targets, dict) or any(
            not isinstance(item, str)
            or not item.isascii()
            or not item.isdecimal()
            or type(count) is not int
            or count < 0
            for item, count in material_targets.items()
        ):
            raise ValueError(
                "garbage_material_targets 必须为材料 ID 到非负整数阈值的映射；0 表示豁免"
            )
        if any(
            item in MATERIAL_BLACKLIST and count > 0 for item, count in material_targets.items()
        ):
            raise ValueError("黑名单材料固定豁免，不能设置正的垃圾时间阈值")
        if (
            not isinstance(blacklist, list)
            or any(not isinstance(v, str) or not v.strip() for v in blacklist)
            or len(set(blacklist)) != len(blacklist)
        ):
            raise ValueError("shop_blacklist 必须为不重复的非空商品名列表")
        ordinary_fight = any(
            t.task_type == "Fight"
            and not is_annihilation_stage(json.loads(t.params_json).get("stage"))
            for t in parsed_tasks
        )
        if ordinary_fight and client not in {None, "Official", "Bilibili"}:
            raise ValueError("包含普通 Fight 的日程当前仅支持官服/Bilibili 自动通关")
        if sum(t.annihilation_expert for t in parsed_tasks) > 1:
            raise ValueError("每个日程最多配置一个剿灭 expert")
        if any(
            t.strategy == "expert" and t.task_type == "Fight" for t in parsed_tasks
        ) and client not in {None, "Official", "Bilibili"}:
            raise ValueError("Fight expert 当前仅支持官服/Bilibili")
        schedules.append(
            DailySchedule(
                id=identifier,
                profiles=tuple(targets),
                times=minutes,
                tasks=parsed_tasks,
                enabled=_boolean(row, "enabled"),
                weekdays=_weekdays(row.get("weekdays", list(range(1, 8)))),
                execution_timeout=_positive(
                    row.get("execution_timeout", 7200), "execution_timeout"
                ),
                options_json=options,
                session_json=_json(session, "session"),
                client_type=client,
                shop_blacklist=tuple(blacklist),
                garbage_material_threshold=threshold,
                garbage_material_targets=tuple(material_targets.items()),
            )
        )
    return tuple(schedules)

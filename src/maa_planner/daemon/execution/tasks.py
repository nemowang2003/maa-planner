"""Immutable MaaCore task plans for the configured Core runtime."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

TASK_TYPES = frozenset(
    "StartUp CloseDown Fight Recruit Infrast Mall Award Roguelike Copilot SSSCopilot "
    "ParadoxCopilot Depot OperBox Reclamation Custom SingleStep VideoRecognition SwitchTheme".split()
)


def json_object(content: str) -> dict:
    def invalid(value):
        raise ValueError(f"JSON 不允许 {value}")

    value = json.loads(content, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("任务配置必须是 JSON 对象")
    return value


def validate_resource_tasks(value: object) -> dict:
    """Project-owned nodes may inherit Core nodes, without replacing shared names."""
    if not isinstance(value, dict):
        raise ValueError("resource_tasks 必须为任务节点对象")
    for name, node in value.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"Planner@[A-Za-z0-9_@-]+", name)
            or not isinstance(node, dict)
        ):
            raise ValueError("私有任务资源必须使用 Planner@ 命名空间和节点对象")
    return value


def normalize_fight(task: dict) -> dict:
    """Ordinary sanity farming has no attempt cap and always uses auto series."""
    from maa_planner.daemon.planning.annihilation import is_annihilation_stage

    params = task.get("params", {})
    if task["type"] != "Fight" or is_annihilation_stage(params.get("stage")):
        return task
    return {
        **task,
        "params": {**{key: value for key, value in params.items() if key != "times"}, "series": 0},
    }


@dataclass(frozen=True)
class CoreTasks:
    """Files are JSON text, embedded in the request rather than read during execution."""

    document: str
    attachments: str = "{}"

    def __post_init__(self):
        doc = json_object(self.document)
        if set(doc) - {
            "tasks",
            "client_type",
            "startup",
            "closedown",
            "resource_tasks",
            "activity_copilot",
            "activity_shops",
            "resource_images",
            "annihilation_copilot",
            "business_ids",
            "business_failures",
            "infrast_processing",
            "infrast_orundum",
        }:
            raise ValueError("计划包含不支持的顶层字段")
        tasks = doc.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("必须明确提供非空 tasks；不会隐式读取 GUI 任务")
        if "business_ids" in doc:
            keys = doc["business_ids"]
            if (
                not isinstance(keys, list)
                or len(keys) != len(tasks)
                or any(
                    not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_:#-]{1,100}", key)
                    for key in keys
                )
                or len(set(keys)) != len(keys)
            ):
                raise ValueError("business_ids 必须与 tasks 一一对应且不重复")
        failures = doc.get("business_failures", [])
        if not isinstance(failures, list) or any(
            not isinstance(f, dict)
            or not isinstance(f.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_:#-]{1,100}", f["id"])
            or f.get("status") != "error"
            or not isinstance(f.get("error"), dict)
            for f in failures
        ):
            raise ValueError("business_failures 必须包含业务任务失败及原因")
        for task in tasks:
            if not isinstance(task, dict) or task.get("type") not in TASK_TYPES:
                raise ValueError(f"未知任务类型: {task}")
            if set(task) - {"name", "type", "params"}:
                raise ValueError("执行计划必须先解析条件，不接受 variants 等隐式决策")
            if not isinstance(task.get("params", {}), dict):
                raise ValueError("任务 params 必须是对象")
            from maa_planner.daemon.planning.activity_rewards import validate_reward_marker

            validate_reward_marker(task)
            from maa_planner.daemon.planning.game_update import validate_marker

            validate_marker(task)
            if task["type"] == "Fight":
                from maa_planner.daemon.execution.annihilation_tasks import delegation_task
                from maa_planner.daemon.planning.annihilation import is_annihilation_stage

                if is_annihilation_stage(task.get("params", {}).get("stage")):
                    delegation_task(task, 0)  # Validate before admitting/booting a worker.
        normalized = [normalize_fight(task) for task in tasks]
        if normalized != tasks:
            doc["tasks"] = normalized
            object.__setattr__(self, "document", json.dumps(doc, ensure_ascii=False))
        validate_resource_tasks(doc.get("resource_tasks", {}))
        if "activity_copilot" in doc:
            from maa_planner.daemon.execution.activity_copilot import validate_clearance_plan

            validate_clearance_plan(doc["activity_copilot"], len(tasks))
        if "activity_shops" in doc:
            from maa_planner.daemon.planning.activity_shop import validate_shop_plans

            validate_shop_plans(doc["activity_shops"], tasks)
        if "annihilation_copilot" in doc:
            from maa_planner.daemon.planning.annihilation_rescue import validate_context

            validate_context(doc["annihilation_copilot"])
        if "infrast_processing" in doc:
            from maa_planner.daemon.planning.processing import validate_phases

            validate_phases(doc["infrast_processing"], tasks)
        if "infrast_orundum" in doc:
            from maa_planner.daemon.execution.orundum import validate_intent_plan

            validate_intent_plan(doc["infrast_orundum"], doc)
        files = json_object(self.attachments)
        for name, value in files.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.(json|png)", name):
                raise ValueError(f"附件名称无效: {name}")
            if name.endswith(".png"):
                from maa_planner.daemon.execution.png import decode_png

                decode_png(value)
            elif not isinstance(value, dict):
                raise ValueError(f"附件必须是 JSON 对象: {name}")
        images = doc.get("resource_images", {})
        if not isinstance(images, dict):
            raise ValueError("resource_images 必须为图片名称到 PNG 附件引用的映射")
        from maa_planner.daemon.execution.png import image_name

        for name, reference in images.items():
            image_name(name)
            if (
                not isinstance(reference, str)
                or not reference.startswith("@file:")
                or not reference.endswith(".png")
                or reference[6:] not in files
            ):
                raise ValueError("私有图片必须引用已固定 PNG 附件")

        def visit(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "filename" and (
                        not isinstance(child, str) or not child.startswith("@file:")
                    ):
                        raise ValueError(
                            "filename 必须引用 @file: 附件，避免执行时读取已变化的文件"
                        )
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and value.startswith("@file:"):
                if value[6:] not in files:
                    raise ValueError(f"缺少附件: {value}")

        visit(doc)

    @classmethod
    def read(cls, path: Path) -> CoreTasks:
        text = path.read_text(encoding="utf-8-sig")
        doc = tomllib.loads(text) if path.suffix == ".toml" else json_object(text)
        files = doc.pop("attachments", {})
        return cls(json.dumps(doc, ensure_ascii=False), json.dumps(files, ensure_ascii=False))

    def materialize(self, directory: Path, path_string=str) -> dict:
        files = json_object(self.attachments)
        attachment_dir = directory / "files"
        attachment_dir.mkdir(mode=0o700)
        for name, content in files.items():
            if name.endswith(".png"):
                from maa_planner.daemon.execution.png import decode_png

                (attachment_dir / name).write_bytes(decode_png(content))
            else:
                (attachment_dir / name).write_text(
                    json.dumps(content, ensure_ascii=False), encoding="utf-8"
                )

        def rewrite(value):
            if isinstance(value, dict):
                return {k: rewrite(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rewrite(v) for v in value]
            if isinstance(value, str) and value.startswith("@file:"):
                return path_string(attachment_dir / value[6:])
            return value

        return rewrite(json_object(self.document))

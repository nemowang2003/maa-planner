from __future__ import annotations

from pathlib import Path

import tomlkit

from .config import PlannerSettings
from .expert import PlanResult


def task_file_path(settings: PlannerSettings, user_name: str) -> Path:
    return settings.maa_config_dir / "tasks" / f"{settings.task_prefix}-{user_name}.toml"


def profile_file_path(settings: PlannerSettings) -> Path:
    return settings.maa_config_dir / "profiles" / f"{settings.profile_name}.toml"


def render_tasks(plan: PlanResult) -> str:
    doc = tomlkit.document()
    aot = tomlkit.aot()
    for task in plan.tasks:
        table = tomlkit.table()
        table["name"] = task.name
        table["type"] = task.task_type
        if task.params:
            table["params"] = task.params
        aot.append(table)
    doc["tasks"] = aot
    return doc.as_string()


def render_profile(settings: PlannerSettings) -> str:
    doc = tomlkit.document()
    for section, payload in settings.profile_sections.items():
        table = tomlkit.table()
        for key, value in payload.items():
            table[key] = value
        doc[section] = table
    return doc.as_string()


def write_plan(settings: PlannerSettings, plan: PlanResult) -> Path:
    path = task_file_path(settings, plan.user_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_tasks(plan), encoding="utf-8")
    return path


def write_profile(settings: PlannerSettings) -> Path:
    path = profile_file_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_profile(settings), encoding="utf-8")
    return path

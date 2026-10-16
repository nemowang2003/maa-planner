from __future__ import annotations

from dataclasses import dataclass

from .config import PlannerSettings, PlannerUser
from .skland_client import PlayerContext


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    task_type: str
    params: dict


@dataclass(frozen=True, slots=True)
class PlanResult:
    user_name: str
    character_name: str
    reason: str
    tasks: list[TaskSpec]


def build_plan(settings: PlannerSettings, user: PlannerUser, context: PlayerContext) -> PlanResult:
    del settings
    del user
    return PlanResult(
        user_name=context.user_name,
        character_name=context.character_name,
        reason="专家系统尚未实现；当前仅完成认证、数据采集和模块调用流程。",
        tasks=[],
    )

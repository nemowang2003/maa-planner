from __future__ import annotations

from dataclasses import dataclass

from .config import PlannerSettings, PlannerUser
from .skland_client import ActivityStage, PlayerSnapshot


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    task_type: str
    params: dict


@dataclass(frozen=True, slots=True)
class PlanResult:
    user_name: str
    character_name: str
    stage: str
    reason: str
    tasks: list[TaskSpec]


def build_plan(
    settings: PlannerSettings, user: PlannerUser, snapshot: PlayerSnapshot
) -> PlanResult:
    stage, reason = choose_stage(
        snapshot.activity_stages, user.material_weights, settings.fallback_stage
    )
    tasks = [
        TaskSpec(
            name="StartUp",
            task_type="StartUp",
            params={
                "client_type": settings.client_type,
                "start_game_enabled": True,
            },
        ),
        TaskSpec(
            name="Fight",
            task_type="Fight",
            params={
                "stage": stage,
                "times": settings.fight_times,
                "expiring_medicine": settings.expiring_medicine,
                "stone": settings.expiring_stone,
            },
        ),
        TaskSpec(
            name="Award",
            task_type="Award",
            params={},
        ),
    ]

    if snapshot.recruit_idle_slots > 0 or snapshot.recruit_refreshes > 0:
        tasks.append(
            TaskSpec(
                name="Recruit",
                task_type="Recruit",
                params={
                    "refresh": settings.recruit_refresh,
                    "select": settings.recruit_select,
                    "confirm": settings.recruit_confirm,
                    "times": settings.recruit_times,
                },
            )
        )

    if snapshot.drones >= settings.drone_threshold or snapshot.exhausted_operators:
        tasks.append(
            TaskSpec(
                name="Infrast",
                task_type="Infrast",
                params={
                    "filename": settings.infrast_filename,
                    "facility": settings.infrast_facility,
                    "dorm_trust_enabled": settings.dorm_trust_enabled,
                },
            )
        )

    return PlanResult(
        user_name=user.name,
        character_name=snapshot.character_name,
        stage=stage,
        reason=reason,
        tasks=tasks,
    )


def choose_stage(
    activity_stages: list[ActivityStage],
    material_weights: dict[str, int],
    fallback_stage: str,
) -> tuple[str, str]:
    best_stage: ActivityStage | None = None
    best_score = 0

    for stage in activity_stages:
        score = sum(material_weights.get(item, 0) for item in stage.items)
        if score > best_score:
            best_score = score
            best_stage = stage

    if best_stage is None or best_score <= 0:
        return fallback_stage, "未命中活动掉落偏好，回退到默认关卡"

    return (
        best_stage.stage,
        f"活动 {best_stage.activity_name} 中 {best_stage.stage} 命中材料偏好，得分 {best_score}",
    )

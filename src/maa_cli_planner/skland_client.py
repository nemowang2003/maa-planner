from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from skland_api import CharacterInfoLoader
from skland_api.models.auth import AuthInfo
from skland_api.modules.activity import main as get_activity
from skland_api.modules.infrast_basic import main as get_infrast
from skland_api.modules.mission import main as get_mission
from skland_api.modules.recruit import main as get_recruit
from skland_api.modules.routine import main as get_routine
from skland_api.modules.sanity import main as get_sanity

from .config import PlannerSettings, PlannerUser


@dataclass(frozen=True, slots=True)
class ActivityStage:
    activity_name: str
    stage: str
    items: list[str]


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    user_name: str
    character_name: str
    depot: dict[str, int]
    activity_stages: list[ActivityStage]
    sanity_current: int
    sanity_total: int
    recruit_idle_slots: int
    recruit_refreshes: int
    drones: int
    exhausted_operators: list[str]
    daily_mission_progress: tuple[int, int]
    weekly_mission_progress: tuple[int, int]
    annihilation_progress: tuple[int, int]


async def fetch_snapshot(settings: PlannerSettings, user: PlannerUser) -> PlayerSnapshot:
    auth = _load_auth_info(settings.skland_auth_dir / f"{user.auth_name}.json")
    api = await auth.full_auth()
    characters = await api.arknights_binding_list()
    binding = _select_character(user, characters)
    character_name = _binding_name(binding)
    character_info = await CharacterInfoLoader(user.name, api, binding).full_load()

    activity = get_activity(character_info, None)
    sanity = get_sanity(character_info, None)
    mission = get_mission(character_info, None)
    routine = get_routine(character_info, None)
    recruit = get_recruit(character_info, None)
    infrast = get_infrast(character_info, None)

    return PlayerSnapshot(
        user_name=user.name,
        character_name=character_name,
        depot=character_info.depot,
        activity_stages=_flatten_activity_stages(activity.activities),
        sanity_current=sanity.sanity.current,
        sanity_total=sanity.sanity.total,
        recruit_idle_slots=sum(1 for slot in recruit.recruits if slot.is_idle),
        recruit_refreshes=recruit.refresh.current,
        drones=infrast.drones.current,
        exhausted_operators=infrast.exhausted_operators,
        daily_mission_progress=(mission.daily.current, mission.daily.total),
        weekly_mission_progress=(mission.weekly.current, mission.weekly.total),
        annihilation_progress=(routine.annihilation.current, routine.annihilation.total),
    )


def _load_auth_info(path: Path) -> AuthInfo:
    with path.open(encoding="utf-8") as fp:
        raw = json.load(fp)
    if not isinstance(raw, dict):
        raise ValueError(f"认证文件格式错误: {path}")
    return AuthInfo(
        phone=raw.get("phone"),
        password=raw.get("password"),
        token=raw.get("token"),
        cred=raw.get("cred"),
    )


def _select_character(user: PlannerUser, characters: list[dict]) -> dict:
    if not characters:
        raise ValueError(f"{user.name} 没有绑定任何明日方舟角色")

    if user.character_name is None:
        return characters[0]

    expected = user.character_name.casefold()
    for character in characters:
        if _binding_name(character).casefold() == expected:
            return character

    known = ", ".join(_binding_name(character) for character in characters)
    raise ValueError(f"{user.name} 未找到角色 {user.character_name!r}，可选角色: {known}")


def _binding_name(binding: dict) -> str:
    for key in ("nickName", "nickname", "name", "channelName", "uid"):
        value = binding.get(key)
        if isinstance(value, str) and value:
            return value
    return str(binding["uid"])


def _flatten_activity_stages(activities: list) -> list[ActivityStage]:
    stages: list[ActivityStage] = []
    for activity in activities:
        for entry in activity.drop_stages:
            stage, _, items = entry.partition(":")
            stage_name = stage.strip()
            drop_items = [item.strip() for item in items.split("、") if item.strip()]
            if stage_name:
                stages.append(
                    ActivityStage(
                        activity_name=activity.name,
                        stage=stage_name,
                        items=drop_items,
                    )
                )
    return stages

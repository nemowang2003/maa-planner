from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skland_api import CharacterInfo, CharacterInfoLoader
from skland_api.models.auth import AuthInfo
from skland_api.modules.activity import ActivityOverview
from skland_api.modules.activity import main as get_activity
from skland_api.modules.infrast_basic import InfrastOverview
from skland_api.modules.infrast_basic import main as get_infrast
from skland_api.modules.mission import MissionStatus
from skland_api.modules.mission import main as get_mission
from skland_api.modules.recruit import RecruitOverview
from skland_api.modules.recruit import main as get_recruit
from skland_api.modules.routine import RoutineStatus
from skland_api.modules.routine import main as get_routine
from skland_api.modules.sanity import SanityStatus
from skland_api.modules.sanity import main as get_sanity

from .config import PlannerSettings, PlannerUser


@dataclass(frozen=True, slots=True)
class PlayerContext:
    user_name: str
    character_name: str
    character_info: CharacterInfo
    activity: ActivityOverview
    sanity: SanityStatus
    mission: MissionStatus
    routine: RoutineStatus
    recruit: RecruitOverview
    infrast: InfrastOverview


async def collect_player_context(settings: PlannerSettings, user: PlannerUser) -> PlayerContext:
    auth = _load_auth_info(settings.skland_auth_dir / f"{user.auth_name}.json")
    api = await auth.full_auth()
    characters = await api.arknights_binding_list()
    binding = _select_character(user, characters)
    character_name = _binding_name(binding)
    character_info = await CharacterInfoLoader(user.name, api, binding).full_load()

    return PlayerContext(
        user_name=user.name,
        character_name=character_name,
        character_info=character_info,
        activity=get_activity(character_info, None),
        sanity=get_sanity(character_info, None),
        mission=get_mission(character_info, None),
        routine=get_routine(character_info, None),
        recruit=get_recruit(character_info, None),
        infrast=get_infrast(character_info, None),
    )


def summarize_context(context: PlayerContext) -> dict[str, Any]:
    return {
        "user_name": context.user_name,
        "character_name": context.character_name,
        "depot": context.character_info.depot,
        "activity": [
            {
                "name": entry.name,
                "is_replica": entry.is_replica,
                "starts_at": int(entry.starts_at),
                "ends_at": int(entry.ends_at),
                "drop_stages": entry.drop_stages,
            }
            for entry in context.activity.activities
        ],
        "sanity": {
            "current": context.sanity.sanity.current,
            "total": context.sanity.sanity.total,
            "full_at": int(context.sanity.full_at),
        },
        "mission": {
            "daily": {
                "current": context.mission.daily.current,
                "total": context.mission.daily.total,
            },
            "weekly": {
                "current": context.mission.weekly.current,
                "total": context.mission.weekly.total,
            },
        },
        "routine": {
            "annihilation": {
                "current": context.routine.annihilation.current,
                "total": context.routine.annihilation.total,
            },
            "annihilation_reset_at": int(context.routine.annihilation_reset_at),
            "sss_instrument": {
                "current": context.routine.sss_instrument.current,
                "total": context.routine.sss_instrument.total,
            },
            "sss_component": {
                "current": context.routine.sss_component.current,
                "total": context.routine.sss_component.total,
            },
            "sss_reset_at": int(context.routine.sss_reset_at),
        },
        "recruit": {
            "idle_slots": sum(1 for slot in context.recruit.recruits if slot.is_idle),
            "slots": [
                {
                    "is_idle": slot.is_idle,
                    "finish_at": None if slot.finish_at is None else int(slot.finish_at),
                }
                for slot in context.recruit.recruits
            ],
            "refresh_current": context.recruit.refresh.current,
            "refresh_total": context.recruit.refresh.total,
            "refresh_at": (
                None if context.recruit.refresh_at is None else int(context.recruit.refresh_at)
            ),
        },
        "infrast": {
            "drones": {
                "current": context.infrast.drones.current,
                "total": context.infrast.drones.total,
            },
            "drones_full_in": int(context.infrast.drones_full_in),
            "exhausted_operators": context.infrast.exhausted_operators,
            "mastery": (
                None
                if context.infrast.mastery is None
                else {
                    "trainee_name": context.infrast.mastery.trainee_name,
                    "skill_id": context.infrast.mastery.skill_id,
                    "skill_mastery_level": context.infrast.mastery.skill_mastery_level,
                    "trainer_name": context.infrast.mastery.trainer_name,
                    "speed": context.infrast.mastery.speed,
                    "progress": {
                        "current": context.infrast.mastery.progress.current,
                        "total": context.infrast.mastery.progress.total,
                    },
                    "remain_seconds": int(context.infrast.mastery.remain_seconds),
                }
            ),
        },
    }


def summarize_context_text(context: PlayerContext) -> str:
    return json.dumps(summarize_context(context), ensure_ascii=False, indent=2)


def _load_auth_info(path: Path) -> AuthInfo:
    if not path.exists():
        raise FileNotFoundError(
            f"认证文件不存在: {path}。请先运行 `skland auth add {path.stem}`，或修改 planner 配置中的 auth_name / skland_auth_dir。"
        )
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

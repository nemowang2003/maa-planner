import unittest
from pathlib import Path
from unittest.mock import MagicMock

from maa_cli_planner.config import PlannerSettings, PlannerUser
from maa_cli_planner.expert import build_plan
from maa_cli_planner.skland_client import (
    ActivityOverview,
    CharacterInfo,
    InfrastOverview,
    MissionStatus,
    PlayerContext,
    RecruitOverview,
    RoutineStatus,
    SanityStatus,
)


class ExpertPlaceholderTest(unittest.TestCase):
    def test_build_plan_returns_placeholder_without_tasks(self) -> None:
        settings = PlannerSettings(
            config_file=Path("/tmp/config.toml"),
            maa_config_dir=Path("/tmp/maa"),
            skland_auth_dir=Path("/tmp/auth"),
            task_prefix="daily",
            profile_name="default",
            write_profile=False,
            client_type="Official",
            fallback_stage="1-7",
            fight_times=99,
            expiring_medicine=0,
            expiring_stone=0,
            recruit_refresh=True,
            recruit_select=[4],
            recruit_confirm=[3],
            recruit_times=4,
            infrast_filename="default.json",
            infrast_facility=["Mfg"],
            dorm_trust_enabled=True,
            drone_threshold=180,
            material_weights={},
            users=[],
            profile_sections={},
        )
        user = PlannerUser(name="main", auth_name="main")
        context = PlayerContext(
            user_name="main",
            character_name="Doctor",
            character_info=MagicMock(spec=CharacterInfo),
            activity=MagicMock(spec=ActivityOverview),
            sanity=MagicMock(spec=SanityStatus),
            mission=MagicMock(spec=MissionStatus),
            routine=MagicMock(spec=RoutineStatus),
            recruit=MagicMock(spec=RecruitOverview),
            infrast=MagicMock(spec=InfrastOverview),
        )
        plan = build_plan(settings, user, context)
        self.assertEqual(plan.user_name, "main")
        self.assertEqual(plan.tasks, [])
        self.assertIn("尚未实现", plan.reason)


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path

from maa_cli_planner.config import PlannerSettings
from maa_cli_planner.expert import PlanResult, TaskSpec
from maa_cli_planner.maa import render_profile, render_tasks


def _settings() -> PlannerSettings:
    return PlannerSettings(
        config_file=Path("/tmp/config.toml"),
        maa_config_dir=Path("/tmp/maa"),
        skland_auth_dir=Path("/tmp/auth"),
        task_prefix="daily",
        profile_name="default",
        write_profile=True,
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
        profile_sections={"connection": {"preset": "MuMuPro", "address": "127.0.0.1:16384"}},
    )


class RenderTest(unittest.TestCase):
    def test_render_tasks_contains_task_array(self) -> None:
        content = render_tasks(
            PlanResult(
                user_name="main",
                character_name="Doctor",
                reason="picked",
                tasks=[
                    TaskSpec(
                        name="StartUp", task_type="StartUp", params={"client_type": "Official"}
                    ),
                    TaskSpec(name="Fight", task_type="Fight", params={"stage": "SL-8"}),
                ],
            )
        )
        self.assertIn("[[tasks]]", content)
        self.assertIn('type = "Fight"', content)
        self.assertIn('stage = "SL-8"', content)

    def test_render_profile_contains_sections(self) -> None:
        content = render_profile(_settings())
        self.assertIn("[connection]", content)
        self.assertIn('preset = "MuMuPro"', content)


if __name__ == "__main__":
    unittest.main()

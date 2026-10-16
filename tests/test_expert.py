import unittest

from maa_cli_planner.expert import choose_stage
from maa_cli_planner.skland_client import ActivityStage


class ChooseStageTest(unittest.TestCase):
    def test_prefers_weighted_activity_stage(self) -> None:
        stage, reason = choose_stage(
            [
                ActivityStage(activity_name="A", stage="SL-6", items=["糖组"]),
                ActivityStage(activity_name="A", stage="SL-8", items=["固源岩组", "酮凝集组"]),
            ],
            {"糖组": 1, "固源岩组": 5, "酮凝集组": 3},
            "1-7",
        )
        self.assertEqual(stage, "SL-8")
        self.assertIn("得分 8", reason)

    def test_falls_back_without_matching_weight(self) -> None:
        stage, reason = choose_stage(
            [ActivityStage(activity_name="A", stage="SL-6", items=["糖组"])],
            {"固源岩组": 5},
            "1-7",
        )
        self.assertEqual(stage, "1-7")
        self.assertIn("回退", reason)


if __name__ == "__main__":
    unittest.main()

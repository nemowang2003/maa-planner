from tests.helpers.core import FakeCore


class ClearanceCore(FakeCore):
    def __init__(
        self,
        outcomes=(),
        *,
        navigation_failure=False,
        settlement_exit="ready",
        stage_ids=None,
        story_nodes=("PlotPanel", "PlotStart", "PlotSkipped", "PlotReturned"),
        story_failure=False,
    ):
        super().__init__()
        self.outcomes = iter(outcomes)
        self.navigation_failure = navigation_failure
        self.settlement_exit = settlement_exit
        self.story_nodes = story_nodes
        self.story_failure = story_failure
        self.nonperfect = False
        self.visited = []
        self.stage_ids = stage_ids or {"EV-EX-1": "act_01", "EV-EX-2": "act_02"}

    def task_outcome(self, identifier, kind, params):
        common = {"taskid": identifier, "taskchain": kind}
        failed = False
        if kind == "Custom":
            phase = params["task_names"][0].rsplit("@", 1)[-1]
            if phase == "PlotBegin":
                for node in self.story_nodes:
                    self.emit(
                        20002, {**common, "subtask": "ProcessTask", "details": {"task": node}}
                    )
                failed = self.story_failure
            elif phase == "Area" and self.navigation_failure:
                failed = True
            elif phase == "NonPerfect":
                failed = not self.nonperfect
                if self.nonperfect:
                    self.emit(
                        20002,
                        {**common, "subtask": "ProcessTask", "details": {"task": "NonPerfect"}},
                    )
            elif phase == "ClickCornerUntilStartButton" and self.settlement_exit != "ready":
                failed = self.settlement_exit == "error"
            else:
                node = {
                    "ToHome": "Home",
                    "Terminal": "TerminalReady",
                    "Activity": "Activity",
                    "Area": "Map",
                    "SwitchArea": "Map",
                    "ClickCornerUntilStartButton": "BattleStartPreFlag",
                }[phase]
                self.emit(20002, {**common, "subtask": "ProcessTask", "details": {"task": node}})
        elif kind == "Copilot":
            entry = params["copilot_list"][0]
            self.visited.append((entry["nav_name_override"], entry["is_raid"]))
            assert params["use_sanity_potion"] is True
            assert type(params["formation"]) is bool
            assert params["support_unit_usage"] in (0, 1)
            self.emit(
                20003,
                {
                    **common,
                    "what": "CopilotListLoadTaskFileSuccess",
                    "details": {
                        "id": 0,
                        "stage_name": self.stage_ids[entry["nav_name_override"]],
                        "file_name": entry["filename"],
                    },
                },
            )
            outcome = next(self.outcomes, "completed")
            self.nonperfect = outcome == "two_star"
            node = {
                "completed": "StageDrops-Stars-3",
                "failed": "FightMissionFailed",
            }.get(outcome, "UseMedicine")
            self.emit(
                20002,
                {
                    **common,
                    "subtask": "ProcessTask",
                    "first": ["Copilot@WaitUntilEndOfAction"],
                    "details": {"task": node},
                },
            )
            failed = outcome != "completed"
        return 10000 if failed else 10002

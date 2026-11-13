from tests.helpers.core import FakeCore


class ShopCore(FakeCore):
    def __init__(self, outcome="completed", navigation=True, verified=True):
        super().__init__()
        self.outcome, self.navigation = outcome, navigation
        self.verified = verified

    def task_outcome(self, identifier, kind, params):
        common = {"taskid": identifier, "taskchain": kind}
        names = params.get("task_names", [])
        if names and names[0].endswith("@Open"):
            if not self.navigation:
                return 10000
            if self.verified:
                self.emit(
                    20002,
                    {**common, "subtask": "ProcessTask", "details": {"task": "ShopReady"}},
                )
        if names == ["SS@Store@Begin"] and self.outcome != "unknown":
            self.emit(20001, {**common, "subtask": "EventShopTask"})
            self.emit(
                20002,
                {
                    **common,
                    "first": names,
                    "details": {
                        "task": "CheckUnlimited" if self.outcome == "completed" else "Underfunded"
                    },
                },
            )
        return 10002

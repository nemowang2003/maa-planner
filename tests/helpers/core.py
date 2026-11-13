import json


class FakeCore:
    def __init__(self, *, method="MumuExtras", error=False, cancel=None, malformed=False):
        self.method, self.error, self.cancel, self.malformed = method, error, cancel, malformed
        self.appended, self.options, self.extras = [], [], []
        self.cursor = 0
        self.destroyed = False
        self.started = False

    def emit(self, message, payload):
        self.callback(message, json.dumps(payload).encode(), None)

    def AsstGetVersion(self):
        return b"v6.21.1"

    def AsstSetUserDir(self, path):
        return True

    def AsstLoadResource(self, path):
        return True

    def AsstSetStaticOption(self, key, value):
        return True

    def AsstCreateEx(self, callback, arg):
        self.callback = callback
        return 1

    def AsstDestroy(self, handle):
        self.destroyed = True

    def AsstSetConnectionExtras(self, name, value):
        self.extras.append((name, json.loads(value)))

    def AsstSetInstanceOption(self, handle, key, value):
        self.options.append((key, value))
        return True

    def AsstConnect(self, *args):
        self.emit(2, {"what": "FastestWayToScreencap", "details": {"method": self.method}})
        return True

    def AsstAppendTask(self, handle, task_type, params):
        self.appended.append((task_type.decode(), json.loads(params)))
        return len(self.appended)

    def pending_tasks(self):
        pending = list(enumerate(self.appended[self.cursor :], self.cursor + 1))
        self.cursor = len(self.appended)
        return pending

    def task_outcome(self, identifier, task_type, params, /):
        if task_type == "Fight":
            self.emit(
                20003,
                {
                    "taskid": identifier,
                    "taskchain": task_type,
                    "what": "StageDrops",
                    "details": {"stars": 3, "drops": []},
                },
            )
        return 10000 if self.error else 10002

    def AsstStart(self, handle):
        self.started = True
        for identifier, (task_type, params) in self.pending_tasks():
            common = {"taskid": identifier, "taskchain": task_type}
            self.emit(10001, common)
            if self.malformed:
                self.callback(20003, b"bad json", None)
            outcome = self.task_outcome(identifier, task_type, params)
            if self.cancel:
                self.cancel.set()
            elif outcome is not None:
                self.emit(outcome, common)
        return True

    def AsstRunning(self, handle):
        return self.cancel is not None and self.started

    def AsstStop(self, handle):
        self.cursor = len(self.appended)
        self.started = False
        return True


def spec(tmp_path):
    return {
        "directory": str(tmp_path),
        "library": "MaaCore.dll",
        "resources": ["base", "hot"],
        "mumu_path": "C:\\MuMu",
        "instance_index": 3,
        "adb": "adb.exe",
        "address": "127.0.0.1:16480",
        "client_type": "Official",
        "touch_mode": "MiniTouch",
        "tasks": [
            {"type": "StartUp", "params": {"start_game_enabled": True}},
            {"type": "Fight", "params": {"stage": "1-7", "times": 2}},
        ],
    }

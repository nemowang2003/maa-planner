"""Immutable Core requests and process doubles shared by runtime tests."""

import json

from maa_planner.daemon.adapters.mumu import Instance
from maa_planner.daemon.config import Profile
from maa_planner.daemon.execution.context import (
    ImmediateControl,
    RunOptions,
    RunRequest,
    UnitBinding,
)
from maa_planner.daemon.execution.runtime import CoreRun, CoreRuntime
from maa_planner.daemon.execution.tasks import CoreTasks


def request(tmp_path, name="nemo"):
    folder = tmp_path / name
    (folder / "config").mkdir(parents=True)
    (folder / "config/gui.new.json").write_text('{"manual":"untouched"}')
    root = tmp_path / "runtime"
    runtime = CoreRuntime(root / "maa", root / "data", root / "navigation", tmp_path / "runs")
    return RunRequest(
        "job-" + name,
        "attempt-" + name,
        "revision",
        UnitBinding(Profile(name, folder), tmp_path / "MuMu", 0, name),
        options=RunOptions(allow_emulator_start=True),
        core=CoreRun(runtime, CoreTasks(json.dumps({"tasks": [{"type": "Fight"}]}))),
    )


def file_contents(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


class FakeRuntime:
    def __init__(self, *, running=False, hang=False):
        self.was_running = running
        self.hang = hang
        self.started = False
        self.stops = 0
        self.shutdowns = 0
        self.boots = 0
        self.mutate_manual = False

    def ensure_stopped(self, binding):
        pass

    def inspect(self, binding):
        return Instance(
            binding.instance_index, binding.instance_name, self.was_running, "127.0.0.1:12345"
        )

    def boot(self, binding, control, timeout):
        self.boots += 1
        return self.inspect(binding)

    def shutdown(self, binding):
        self.shutdowns += 1


class CancelWhenStarted(ImmediateControl):
    def __init__(self, runtime):
        self.runtime = runtime

    def cancelled(self):
        return self.runtime.started

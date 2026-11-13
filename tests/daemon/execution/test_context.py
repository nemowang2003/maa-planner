import pytest

from maa_planner.daemon.adapters.mumu import Instance
from maa_planner.daemon.execution.context import EmulatorRuntime, ImmediateControl
from maa_planner.daemon.tools.locks import BusyError
from tests.helpers.execution import request


@pytest.mark.parametrize("allow_foreground", [False, True])
def test_foreground_game_requires_explicit_runtime_permission(
    tmp_path, monkeypatch, allow_foreground
):
    binding = request(tmp_path).binding
    instance = Instance(binding.instance_index, binding.instance_name, True, "127.0.0.1:12345")

    class MuMu:
        def __init__(self, root):
            self.adb_path = root / "adb.exe"

        def connect(self, address):
            pass

        def instances(self):
            return [instance]

        def adb(self, address, *args, **kwargs):
            if args[:2] == ("shell", "getprop"):
                return "1"
            return "mResumedActivity: com.hypergryph.arknights/.Game"

    monkeypatch.setattr("maa_planner.daemon.execution.context.MuMu", MuMu)
    runtime = EmulatorRuntime(
        allow_foreground_game=allow_foreground, package_name="com.hypergryph.arknights"
    )
    if allow_foreground:
        assert runtime.boot(binding, ImmediateControl(), 2) == instance
    else:
        with pytest.raises(BusyError, match="前台"):
            runtime.boot(binding, ImmediateControl(), 2)


@pytest.mark.parametrize("allowed", [False, True])
def test_emulator_launch_requires_explicit_permission(tmp_path, monkeypatch, allowed):
    from maa_planner.daemon.execution.context import CancelledRun

    binding = request(tmp_path).binding
    launches = []

    class MuMu:
        def __init__(self, root):
            pass

        def instances(self):
            return [Instance(binding.instance_index, binding.instance_name, False, None)]

        def command(self, *args):
            launches.append(args)
            raise CancelledRun("模拟启动后取消")

    monkeypatch.setattr("maa_planner.daemon.execution.context.MuMu", MuMu)
    runtime = EmulatorRuntime(allow_emulator_start=allowed, package_name="game")
    with pytest.raises(CancelledRun if allowed else BusyError):
        runtime.boot(binding, ImmediateControl(), 2)
    assert bool(launches) == allowed


@pytest.mark.parametrize(
    "output,valid", [("Status: ok\nComplete", True), ("Error: Activity not started", False)]
)
def test_resource_download_launcher_checks_adb_result_without_booting_emulator(
    tmp_path, monkeypatch, output, valid
):
    binding = request(tmp_path).binding
    calls = []

    class MuMu:
        def __init__(self, root):
            pass

        def instances(self):
            return [Instance(binding.instance_index, binding.instance_name, True, "local:1")]

        def adb(self, address, *args, **kwargs):
            calls.append(args)
            return output

    monkeypatch.setattr("maa_planner.daemon.execution.context.MuMu", MuMu)
    runtime = EmulatorRuntime(package_name="com.hypergryph.arknights")
    if valid:
        runtime.start_game(binding)
    else:
        with pytest.raises(RuntimeError, match="启动游戏失败"):
            runtime.start_game(binding)
    assert calls == [
        (
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            "com.hypergryph.arknights",
        )
    ]

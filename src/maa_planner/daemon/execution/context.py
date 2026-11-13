"""One MAA/MuMu binding and its platform-specific process ownership."""

from __future__ import annotations

import math
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from maa_planner.daemon.adapters.mumu import Instance, MuMu
from maa_planner.daemon.adapters.processes import ensure_stopped
from maa_planner.daemon.config import Profile
from maa_planner.daemon.execution.annihilation import AnnihilationResult
from maa_planner.daemon.execution.copilot_results import CopilotResult
from maa_planner.daemon.execution.event_shop import EventShopResult
from maa_planner.daemon.execution.runtime import CoreRun
from maa_planner.daemon.tools.locks import BusyError


@dataclass(frozen=True)
class UnitBinding:
    profile: Profile
    mumu_root: Path
    instance_index: int
    instance_name: str


@dataclass(frozen=True)
class RunOptions:
    allow_emulator_start: bool = False
    startup_timeout: float = 180
    execution_timeout: float = 7200
    resume_game_session: bool = False


@dataclass(frozen=True)
class RunRequest:
    job_id: str
    attempt_id: str
    config_revision: str
    binding: UnitBinding
    options: RunOptions = RunOptions()
    core: CoreRun | None = None

    def __post_init__(self):
        if self.core is None:
            raise ValueError("请求必须包含完整 Core 任务计划")
        for identifier in (self.job_id, self.attempt_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
                raise ValueError("任务 ID 必须是不含路径分隔符的标识")
        if type(self.binding.instance_index) is not int or self.binding.instance_index < 0:
            raise ValueError("实例编号无效")
        if type(self.options.allow_emulator_start) is not bool:
            raise ValueError("模拟器启动许可必须为布尔值")
        if type(self.options.resume_game_session) is not bool:
            raise ValueError("游戏会话恢复标记必须为布尔值")
        for timeout in (self.options.startup_timeout, self.options.execution_timeout):
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("超时必须是有限正数")

    def to_dict(self) -> dict:
        data = asdict(self)
        profile = data["binding"]["profile"]
        profile["gui_path"] = str(profile["gui_path"]) if profile["gui_path"] is not None else None
        data["binding"]["mumu_root"] = str(data["binding"]["mumu_root"])
        data["core"] = self.core.to_dict() if self.core is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> RunRequest:
        if (
            data.get("backend", "core") != "core"
            or data.get("patch_json", "{}") != "{}"
            or data.get("options", {}).get("hook")
        ):
            raise ValueError("GUI 执行请求已不再支持")
        binding = data["binding"]
        profile = binding["profile"]
        return cls(
            job_id=data["job_id"],
            attempt_id=data["attempt_id"],
            config_revision=data["config_revision"],
            binding=UnitBinding(
                Profile(
                    profile["name"],
                    Path(profile.get("gui_path") or profile.get("path"))
                    if profile.get("gui_path") or profile.get("path")
                    else None,
                    profile["emulator_name"],
                    profile.get("auth_name"),
                    profile.get("character_name"),
                    profile.get("enabled", True),
                ),
                Path(binding["mumu_root"]),
                binding["instance_index"],
                binding["instance_name"],
            ),
            options=RunOptions(
                **{
                    k: v
                    for k, v in data["options"].items()
                    if k
                    in {
                        "allow_emulator_start",
                        "startup_timeout",
                        "execution_timeout",
                        "resume_game_session",
                    }
                }
            ),
            core=CoreRun.from_dict(data["core"]) if data.get("core") else None,
        )


@dataclass(frozen=True)
class RunResult:
    attempt_id: str
    status: str
    reason: str = ""
    exit_code: int | None = None
    game_result: str = "unknown"
    log_directory: str | None = None
    event_shop_results: tuple[EventShopResult, ...] = ()
    task_results: tuple[dict, ...] = ()
    annihilation_results: tuple[AnnihilationResult, ...] = ()
    copilot_results: tuple[CopilotResult, ...] = ()
    activity_copilot_results: tuple[dict, ...] = ()
    activity_shop_results: tuple[dict, ...] = ()
    task_checkpoints: tuple[dict, ...] = ()
    activity_reward_results: tuple[dict, ...] = ()
    session_failure: dict | None = None
    business_results: tuple[dict, ...] = ()
    orundum_results: tuple[dict, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> RunResult:
        data: dict = dict(value)
        data["event_shop_results"] = tuple(
            EventShopResult.from_dict(item) for item in value.get("event_shop_results", ())
        )
        data["task_results"] = tuple(value.get("task_results", ()))
        data["business_results"] = tuple(value.get("business_results", ()))
        data["orundum_results"] = tuple(value.get("orundum_results", ()))
        data["annihilation_results"] = tuple(
            AnnihilationResult.from_dict(v) for v in value.get("annihilation_results", ())
        )
        data["copilot_results"] = tuple(
            CopilotResult.from_dict(v) for v in value.get("copilot_results", ())
        )
        data["activity_copilot_results"] = tuple(value.get("activity_copilot_results", ()))
        data["activity_shop_results"] = tuple(value.get("activity_shop_results", ()))
        data["task_checkpoints"] = tuple(value.get("task_checkpoints", ()))
        data["activity_reward_results"] = tuple(value.get("activity_reward_results", ()))
        return cls(**data)


class CancelledRun(RuntimeError):
    pass


class Control(Protocol):
    def cancelled(self) -> bool: ...
    def ready(self) -> bool: ...
    def report(self, phase: str) -> None: ...


class ImmediateControl:
    """Explicit foreground run: admission has already been given by its caller."""

    def cancelled(self) -> bool:
        return False

    def ready(self) -> bool:
        return True

    def report(self, phase: str) -> None:
        pass


class EmulatorRuntime:
    def __init__(
        self,
        *,
        allow_emulator_start: bool = False,
        allow_foreground_game: bool = False,
        package_name: str | None = None,
    ):
        self.allow_emulator_start = allow_emulator_start
        self.allow_foreground_game = allow_foreground_game
        self.package_name = package_name

    def ensure_stopped(self, binding: UnitBinding) -> None:
        try:
            ensure_stopped((binding.profile,))
        except RuntimeError as error:
            raise BusyError(str(error)) from error

    def inspect(self, binding: UnitBinding) -> Instance:
        found = next(
            (i for i in MuMu(binding.mumu_root).instances() if i.index == binding.instance_index),
            None,
        )
        if found is None or found.name != binding.instance_name:
            raise RuntimeError("MuMu 绑定已改变，需要重新解析")
        return found

    def require_game_running(self, binding: UnitBinding) -> Instance:
        """Observe only: copilot never boots an emulator or launches the game."""
        if self.package_name is None:
            raise ValueError("运行快照缺少游戏包名")
        instance = self.inspect(binding)
        if not instance.running or not instance.address:
            raise RuntimeError("copilot 要求模拟器和明日方舟已运行，ADB 已就绪")
        mumu = MuMu(binding.mumu_root)
        try:
            mumu.connect(instance.address)
            output = mumu.adb(instance.address, "shell", "pidof", self.package_name, timeout=5)
        except subprocess.SubprocessError as error:
            raise RuntimeError("无法确认明日方舟正在运行；copilot 不会启动游戏") from error
        if not re.fullmatch(r"\d+(?:\s+\d+)*", output.strip()):
            raise RuntimeError("明日方舟未运行；请先打开游戏并准备 copilot 界面")
        return instance

    def boot(self, binding: UnitBinding, control: Control, timeout: float) -> Instance:
        if self.package_name is None:
            raise ValueError("运行快照缺少游戏包名")
        mumu = MuMu(binding.mumu_root)
        if control.cancelled():
            raise CancelledRun("准备期间取消")
        if not self.inspect(binding).running:
            if not self.allow_emulator_start:
                raise BusyError("模拟器未启动；自动任务不会启动模拟器，请手动启动或使用显式 API")
            mumu.command("control", "-v", str(binding.instance_index), "launch")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if control.cancelled():
                raise CancelledRun("准备期间取消")
            current = self.inspect(binding)
            if current.address:
                try:
                    mumu.connect(current.address)
                    mumu.adb(current.address, "get-state", timeout=10)
                    if (
                        mumu.adb(
                            current.address, "shell", "getprop", "sys.boot_completed", timeout=10
                        )
                        == "1"
                    ):
                        # A manually active game is not owned by this execution.
                        foreground = mumu.adb(
                            current.address,
                            "shell",
                            "dumpsys",
                            "activity",
                            "activities",
                            timeout=10,
                        )
                        if not self.allow_foreground_game and any(
                            self.package_name in line
                            and ("mResumedActivity" in line or "topResumedActivity" in line)
                            for line in foreground.splitlines()
                        ):
                            raise BusyError("游戏正在前台，视为外部占用")
                        return current
                except subprocess.CalledProcessError:
                    pass
            time.sleep(1)
        raise TimeoutError("等待 MuMu/ADB 就绪超时")

    def start_game(self, binding: UnitBinding) -> None:
        if self.package_name is None:
            raise ValueError("资源下载任务缺少游戏包名")
        instance = self.inspect(binding)
        if not instance.running or not instance.address:
            raise BusyError("模拟器未运行或 ADB 尚未就绪")
        output = MuMu(binding.mumu_root).adb(
            instance.address,
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            self.package_name,
            timeout=60,
        )
        if not re.search(r"(?m)^Status:\s*ok\s*$", output):
            raise RuntimeError("启动游戏失败: " + output)

    def shutdown(self, binding: UnitBinding) -> None:
        self.inspect(binding)
        MuMu(binding.mumu_root).command("control", "-v", str(binding.instance_index), "shutdown")

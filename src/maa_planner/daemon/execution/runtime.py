"""Explicit shared runtime and daemon-prepared navigation snapshots."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maa_planner.daemon.constants import GAME_PACKAGE
from maa_planner.daemon.execution.tasks import CoreTasks, json_object
from maa_planner.daemon.tools.storage import write_json

NAV_FILES = (
    "gui/StageActivityV2.json",
    "resource/tasks.json",
    *(
        f"resource/global/{client}/resource/tasks.json"
        for client in ("YoStarEN", "YoStarJP", "YoStarKR", "txwy")
    ),
    "resource/platform_diff/iOS/resource/tasks.json",
)


@dataclass(frozen=True)
class CoreRuntime:
    executable: Path
    data_dir: Path
    navigation: Path
    work_dir: Path
    adb_path: Path | None = None
    library_dir: Path | None = None
    resource_dir: Path | None = None
    expected_core_version: str | None = None
    connection_preset: str | None = None
    touch_mode: str | None = None
    game_package: str | None = GAME_PACKAGE

    def __post_init__(self):
        for value in (
            self.executable,
            self.data_dir,
            self.navigation,
            self.work_dir,
            self.adb_path,
            self.library_dir,
            self.resource_dir,
        ):
            if value is None:
                continue
            if not value.is_absolute():
                raise ValueError("Core 路径必须为绝对路径")
        for shared in (self.data_dir, self.library, self.resources):
            if self.work_dir == shared or self.work_dir.is_relative_to(shared):
                raise ValueError("私有执行目录不能位于共享运行依赖目录内")

    @property
    def windows(self) -> bool:
        return self.executable.suffix.lower() == ".exe"

    @property
    def library(self) -> Path:
        return self.library_dir or self.data_dir / "lib"

    @property
    def core_path(self) -> Path:
        return self.library / ("MaaCore.dll" if self.windows else "libMaaCore.so")

    @property
    def resources(self) -> Path:
        return self.resource_dir or self.data_dir / "resource"

    def validate(self, *, check_hot_update: bool = True) -> None:
        if not self.core_path.is_file():
            raise FileNotFoundError(self.core_path)
        if not (self.resources / "version.json").is_file():
            raise FileNotFoundError(self.resources / "version.json")
        if check_hot_update:
            navigation_files(self.navigation)
        if self.adb_path is not None and not self.adb_path.is_file():
            raise FileNotFoundError(self.adb_path)

    def to_dict(self) -> dict:
        value = {
            name: str(getattr(self, name))
            for name in ("executable", "data_dir", "navigation", "work_dir")
        }
        if self.adb_path is not None:
            value["adb_path"] = str(self.adb_path)
        for name in (
            "library_dir",
            "resource_dir",
            "expected_core_version",
            "connection_preset",
            "touch_mode",
            "game_package",
        ):
            item = getattr(self, name)
            if item is not None:
                value[name] = str(item)
        return value

    @classmethod
    def from_dict(cls, value: dict) -> CoreRuntime:
        fields: dict[str, Any] = {
            name: Path(value[name])
            for name in (
                "executable",
                "data_dir",
                "navigation",
                "work_dir",
                "adb_path",
                "library_dir",
                "resource_dir",
            )
            if value.get(name) is not None
        }
        fields.update(
            {
                name: value[name]
                for name in (
                    "expected_core_version",
                    "connection_preset",
                    "touch_mode",
                    "game_package",
                )
                if value.get(name) is not None
            }
        )
        return cls(**fields)


@dataclass(frozen=True)
class CoreRun:
    runtime: CoreRuntime
    tasks: CoreTasks
    profile_json: str = "{}"

    def __post_init__(self):
        profile = json_object(self.profile_json)
        if set(profile) - {"static_options", "instance_options", "behavior"}:
            raise ValueError("连接与资源路径由 runner 管理，profile 仅接受识别、实例和行为选项")
        if any(not isinstance(value, dict) for value in profile.values()):
            raise ValueError("Core profile 各节必须为对象")
        if profile.get("behavior"):
            raise ValueError("原生 Core 不接受 CLI behavior 选项，请使用任务和实例参数")
        options = profile.get("instance_options", {})
        if options.get("kill_adb_on_exit"):
            raise ValueError("多开禁止 kill_adb_on_exit")

    def to_dict(self) -> dict:
        return {
            "runtime": self.runtime.to_dict(),
            "tasks": self.tasks.document,
            "attachments": self.tasks.attachments,
            "profile_json": self.profile_json,
        }

    @classmethod
    def from_dict(cls, value: dict) -> CoreRun:
        return cls(
            CoreRuntime.from_dict(value["runtime"]),
            CoreTasks(value["tasks"], value.get("attachments", "{}")),
            value.get("profile_json", "{}"),
        )


def navigation_files(root: Path) -> dict[str, bytes]:
    result = {}
    for name in NAV_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"缺少已准备的导航快照: {path}")
        content = path.read_bytes()
        json_object(content.decode("utf-8-sig"))
        result[name] = content
    return result


def fetch_navigation(destination: Path, *, api_url: str) -> None:
    """Explicit maintenance only. Caller publishes destination under the runtime lock."""
    if destination.exists():
        raise FileExistsError("导航快照必须发布到新目录，不能原地更新")
    values = {}
    for name in NAV_FILES:
        request = urllib.request.Request(
            api_url.rstrip("/") + "/" + name,
            headers={"User-Agent": "maa-planner"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    values[name] = json_object(response.read().decode("utf-8-sig"))
                break
            except OSError as error:
                if (
                    isinstance(error, urllib.error.HTTPError)
                    and error.code < 500
                    and error.code != 429
                ):
                    raise
                if attempt == 2:
                    raise
                # Retry only the failed document; never substitute a stale snapshot.
                time.sleep(0.25 * 2**attempt)
    destination.mkdir(parents=True, mode=0o700)
    for name, value in values.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, value)


def probe_runtime(runtime: CoreRuntime) -> str:
    """Load the same-platform Core in a disposable process, without connecting a game."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "maa_planner.daemon.worker",
            "--probe",
            str(runtime.core_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    value = json.loads(result.stdout)
    actual = value["core_version"]
    if runtime.expected_core_version is not None and actual != runtime.expected_core_version:
        raise ValueError(f"配置要求 Core {runtime.expected_core_version}，实际为 {actual}")
    return "MaaCore v" + actual


def ensure_core_stopped(runtime: CoreRuntime) -> None:
    # The worker which loads Core is the owned process. Its PID and creation time
    # are recorded in the recovery fence; unrelated Python processes are allowed.
    from maa_planner.daemon.tools.storage import RecoveryRequired

    for record in (
        *runtime.work_dir.glob("*/worker-process.json"),
        *runtime.work_dir.glob("*/host-process.json"),
    ):
        import psutil

        value = json.loads(record.read_text(encoding="utf-8"))
        try:
            process = psutil.Process(value["pid"])
            if abs(process.create_time() - value["created"]) < 0.01 and process.is_running():
                raise RecoveryRequired(f"Core worker 尚在运行: {process.pid}")
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as error:
            raise RecoveryRequired("无法检查 Core worker 进程") from error

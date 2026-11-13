"""Unified account, authentication and daemon runtime configuration."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_cache_path, user_data_path

from maa_planner.daemon.adapters.windows import local_path
from maa_planner.daemon.constants import GAME_PACKAGE
from maa_planner.daemon.execution.runtime import CoreRuntime
from maa_planner.daemon.planning.game_update import GameUpdateSchedule
from maa_planner.daemon.planning.schedule import DailySchedule, parse_schedules
from maa_planner.daemon.tools.cache_cleanup import CleanupPolicy
from maa_planner.schema import ADMIN_CHANNEL


def default_config_file() -> Path:
    root = Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else Path.home() / ".config"
    return root / "maa-planner" / "config.toml"


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    gui_path: Path | None = None
    emulator_name: str | None = None
    auth_name: str | None = None
    character_name: str | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Settings:
    config_file: Path
    profiles: tuple[Profile, ...]
    mumu_path: Path | None = None
    core_runtime: CoreRuntime | None = None
    skland_auth_dir: Path = field(default_factory=lambda: Path.home() / ".config/skland-api/auth")
    runtime_platform: str = field(default_factory=lambda: "windows" if os.name == "nt" else "linux")
    runtime_directory: Path = field(
        default_factory=lambda: user_data_path("maa-planner", appauthor=False) / "runtimes"
    )
    cache_directory: Path = field(
        default_factory=lambda: user_cache_path("maa-planner", appauthor=False)
    )
    shared_resource_dir: Path | None = None
    schedules: tuple[DailySchedule, ...] = ()
    game_update: GameUpdateSchedule = field(default_factory=GameUpdateSchedule)
    cleanup: CleanupPolicy = field(default_factory=CleanupPolicy)

    @property
    def game_cache(self) -> Path:
        return self.cache_directory / "apk"

    def runtime_template(self) -> CoreRuntime:
        """An unprepared layout, never used by a worker before daemon preparation."""
        if self.core_runtime is not None:
            return self.core_runtime
        root = self.runtime_directory / "unprepared"
        return CoreRuntime(
            Path(sys.executable),
            root / "data",
            root / "navigation",
            self.runtime_directory / "tmp",
            library_dir=root / "data/lib",
            resource_dir=self.shared_resource_dir or root / "data/resource",
        )


def load_settings(config_file: Path | None = None) -> Settings:
    path = local_path(config_file or default_config_file()).resolve()
    return settings_from_bytes(path.read_bytes(), path)


def settings_from_bytes(content: bytes, path: Path) -> Settings:
    """Parse the daemon's accepted snapshot without rereading a changing file."""
    path = path.resolve()
    raw = tomllib.loads(content.decode("utf-8-sig"))
    if set(raw) - {
        "profiles",
        "mumu",
        "maa_core",
        "skland",
        "schedules",
        "game_update",
        "cleanup",
    }:
        raise ValueError("配置包含未知或已移除字段")
    rows = raw.get("profiles")
    if not isinstance(rows, list) or not rows:
        raise ValueError("配置文件需要至少一个 [[profiles]]")
    profiles = []
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise ValueError("每个 [[profiles]] 都必须包含非空 name")
        if set(row) - {
            "name",
            "emulator_name",
            "gui_path",
            "auth_name",
            "character_name",
            "enabled",
        }:
            raise ValueError("profile 包含未知字段")
        name = row["name"]
        if name == ADMIN_CHANNEL:
            raise ValueError("admin 是管理员告警频道的保留名称，不能作为账号名")
        if name in names:
            raise ValueError(f"重复的 profile: {name}")
        names.add(name)
        gui_path = row.get("gui_path")
        if gui_path is not None:
            if not isinstance(gui_path, str) or not gui_path:
                raise ValueError("gui_path 必须是非空目录字符串")
            gui_path = local_path(gui_path)
            gui_path = (
                (path.parent / gui_path).resolve()
                if not gui_path.is_absolute()
                else gui_path.resolve()
            )
        for key in ("emulator_name", "auth_name", "character_name"):
            if row.get(key) is not None and (not isinstance(row[key], str) or not row[key]):
                raise ValueError(f"{name}: {key} 必须是非空字符串")
        if type(row.get("enabled", True)) is not bool:
            raise ValueError("enabled 必须为布尔值")
        profiles.append(
            Profile(
                name,
                gui_path,
                row.get("emulator_name"),
                row.get("auth_name"),
                row.get("character_name"),
                row.get("enabled", True),
            )
        )
    mumu = raw.get("mumu", {})
    if (
        not isinstance(mumu, dict)
        or set(mumu) - {"path"}
        or ("path" in mumu and (not isinstance(mumu["path"], str) or not mumu["path"]))
    ):
        raise ValueError("[mumu].path 必须是安装目录字符串")
    for profile in profiles:
        if profile.emulator_name is not None and not isinstance(profile.emulator_name, str):
            raise ValueError(f"{profile.name}: emulator_name 必须是字符串")
    cli = raw.get("maa_core", {})
    if not isinstance(cli, dict) or set(cli) - {
        "directory",
        "resource_dir",
        "adb_path",
        "connection_preset",
        "touch_mode",
    }:
        raise ValueError("[maa_core] 包含未知或已移除字段")

    def configured_path(value, name):
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError(f"{name} 必须为非空路径")
        value = local_path(value)
        return ((path.parent / value) if not value.is_absolute() else value).resolve()

    runtime_platform = "windows" if os.name == "nt" else "linux"
    directory = configured_path(
        cli.get("directory", user_data_path("maa-planner", appauthor=False) / "runtimes"),
        "maa_core.directory",
    )
    resource_dir = (
        configured_path(cli["resource_dir"], "maa_core.resource_dir")
        if "resource_dir" in cli
        else None
    )
    root = directory / "unprepared"
    values: dict[str, Any] = {
        "executable": Path(sys.executable),
        "data_dir": root / "data",
        "library_dir": root / "data/lib",
        "resource_dir": resource_dir or root / "data/resource",
        "navigation": root / "navigation",
        "work_dir": directory / "tmp",
        "game_package": GAME_PACKAGE,
    }
    if "adb_path" in cli:
        values["adb_path"] = configured_path(cli["adb_path"], "maa_core.adb_path")
    for key in ("connection_preset", "touch_mode"):
        if key in cli:
            if not isinstance(cli[key], str) or not cli[key]:
                raise ValueError(f"maa_core.{key} 必须为非空字符串")
            values[key] = cli[key]
    if values.get("connection_preset", "MuMuEmulator12") != "MuMuEmulator12":
        raise ValueError("只支持 MuMuEmulator12 连接与增强截图")
    values["connection_preset"] = "MuMuEmulator12"
    core_runtime = CoreRuntime(**values)
    skland = raw.get("skland", {})
    if not isinstance(skland, dict) or set(skland) - {"auth_dir"}:
        raise ValueError("[skland] 仅接受 auth_dir")
    if "auth_dir" in skland and (not isinstance(skland["auth_dir"], str) or not skland["auth_dir"]):
        raise ValueError("skland.auth_dir 必须为非空路径")
    auth_dir = local_path(skland.get("auth_dir", Path.home() / ".config/skland-api/auth"))
    if not auth_dir.is_absolute():
        auth_dir = path.parent / auth_dir
    mumu_path = local_path(mumu["path"]) if mumu.get("path") else None
    if mumu_path is not None:
        mumu_path = (
            (path.parent / mumu_path) if not mumu_path.is_absolute() else mumu_path
        ).resolve()
    return Settings(
        path,
        tuple(profiles),
        mumu_path=mumu_path,
        core_runtime=core_runtime,
        skland_auth_dir=auth_dir.resolve(),
        runtime_platform=runtime_platform,
        runtime_directory=directory,
        shared_resource_dir=resource_dir,
        schedules=parse_schedules(raw.get("schedules", []), names, path),
        game_update=GameUpdateSchedule.parse(raw.get("game_update", {})),
        cleanup=CleanupPolicy.parse(raw.get("cleanup", {})),
    )


def example_config() -> str:
    """A minimal bootstrap config; the full expert example lives in docs/example.toml."""
    return r"""# 最小运行配置，默认保存到 %LOCALAPPDATA%/maa-planner/config.toml。
# 多账号、共享/独占时段及 expert 完整示例见项目文档 docs/example.toml。
[mumu]
path = 'C:\Program Files\Netease\MuMu'

[game_update]
enabled = true
time = "11:00"  # 北京时间；自动更新不启动已关闭的模拟器。
poll_interval = 30  # 10、30 或 60 秒，匹配黄色 START 后关闭游戏。
resource_timeout = 3600

[[profiles]]
name = "nemo"
# emulator_name = "nemo"  # 默认为 name
# auth_name = "nemo"  # 森空岛认证文件名，默认为 name
# character_name = "角色昵称"
enabled = true

[maa_core]
directory = 'C:\MAA\core-runtime'
touch_mode = "MiniTouch"
# resource_dir = 'C:\MAA\share\maa-resource\resource'

# [skland]
# auth_dir = "~/.config/skland-api/auth"
"""

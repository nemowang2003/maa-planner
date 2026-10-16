"""Configuration for the Windows desktop runner."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CHANNELS = {"stable", "beta", "alpha"}
ARCHITECTURES = {"x64", "arm64"}


def default_config_file() -> Path:
    return Path.home() / ".config" / "maa_desktop_planner" / "config.toml"


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    path: Path
    channel: str = "stable"
    architecture: str = "x64"

    @property
    def exe(self) -> Path:
        return self.path / "MAA.exe"

    @property
    def gui_config(self) -> Path:
        return self.path / "config" / "gui.new.json"

    @property
    def resource(self) -> Path:
        return self.path / "resource"


@dataclass(frozen=True, slots=True)
class Settings:
    config_file: Path
    profiles: tuple[Profile, ...]
    update_resource: bool = True
    run_directly: bool = True
    exit_self: bool = True
    hook: str | None = None


def load_settings(config_file: Path | None = None) -> Settings:
    path = (config_file or default_config_file()).expanduser().resolve()
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    rows = raw.get("profiles")
    if not isinstance(rows, list) or not rows:
        raise ValueError("配置文件需要至少一个 [[profiles]]")
    profiles = []
    names: set[str] = set()
    paths: set[Path] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ValueError("每个 [[profiles]] 都必须包含 path")
        folder = Path(row["path"]).expanduser()
        if not folder.is_absolute():
            folder = path.parent / folder
        folder = folder.resolve()
        name = str(row.get("name", folder.name))
        channel = str(row.get("channel", "stable")).lower()
        architecture = str(row.get("architecture", "x64")).lower()
        if channel not in CHANNELS or architecture not in ARCHITECTURES:
            raise ValueError(f"{name}: 无效的 channel 或 architecture")
        if name in names or folder in paths:
            raise ValueError(f"重复的 profile: {name} / {folder}")
        names.add(name)
        paths.add(folder)
        profiles.append(Profile(name, folder, channel, architecture))
    runner = raw.get("runner", {})
    if not isinstance(runner, dict):
        raise ValueError("[runner] 必须是 table")
    hook = runner.get("hook")
    if hook is not None and not isinstance(hook, str):
        raise ValueError("runner.hook 必须是 module:function 字符串")
    return Settings(
        path,
        tuple(profiles),
        update_resource=bool(runner.get("update_resource", True)),
        run_directly=bool(runner.get("run_directly", True)),
        exit_self=bool(runner.get("exit_self", True)),
        hook=hook,
    )


def example_config() -> str:
    return """# 每个安装目录单独列出；resource 可以是指向同一共享目录的链接。
[runner]
update_resource = true
run_directly = true
exit_self = true
# hook = "my_planner:build_patch"

[[profiles]]
name = "nemo"
path = "C:\\\\Users\\\\nemo\\\\workspace\\\\Apps\\\\MAA\\\\profiles\\\\nemo"
channel = "stable"
architecture = "x64"
"""

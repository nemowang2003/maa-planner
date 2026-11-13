"""Observe external MAA processes without depending on the runner."""

import os
from pathlib import Path

import psutil

from maa_planner.daemon.config import Profile


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def ensure_stopped(profiles: tuple[Profile, ...]) -> None:
    """Reject only observed processes matching a configured GUI installation."""
    folders = tuple(p.gui_path for p in profiles if p.gui_path is not None)
    if not folders:
        return
    targets = {
        _path_key(path)
        for folder in folders
        for path in (folder / "MAA.exe", folder / "MAA.Updater.exe")
    }
    running = []
    for process in psutil.process_iter(["name"]):
        try:
            name = (process.info["name"] or "").lower()
            if name not in {"maa.exe", "maa.updater.exe"}:
                continue
            executable = process.exe()
            if not executable:
                continue
            if _path_key(Path(executable)) in targets:
                running.append(f"{name} PID {process.pid}")
        except psutil.NoSuchProcess, psutil.ZombieProcess:
            continue
        except psutil.AccessDenied:
            continue
    if running:
        raise RuntimeError("MAA 已在运行: " + ", ".join(running))

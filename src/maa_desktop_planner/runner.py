"""Transactional config overrides around parallel MAA desktop runs."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psutil

from .config import Profile, Settings
from .planner import GuiPatch, resolve_hook
from .updates import check_resource, check_software, plan_software_package, update_resource


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def ensure_stopped(profiles: tuple[Profile, ...]) -> None:
    """Fail closed if a target MAA or updater is already running."""
    targets = {
        _path_key(path)
        for profile in profiles
        for path in (profile.exe, profile.path / "MAA.Updater.exe")
    }
    running = []
    for process in psutil.process_iter(["name"]):
        try:
            name = (process.info["name"] or "").lower()
            if name not in {"maa.exe", "maa.updater.exe"}:
                continue
            executable = process.exe()
            if not executable:
                raise RuntimeError(f"无法确认运行中的 {name} 路径，PID {process.pid}")
            if _path_key(Path(executable)) in targets:
                running.append(f"{name} PID {process.pid}")
        except psutil.NoSuchProcess, psutil.ZombieProcess:
            continue
        except psutil.AccessDenied as error:
            raise RuntimeError("无法检查 MAA 进程；请用有权限的终端运行") from error
    if running:
        raise RuntimeError("MAA 已在运行: " + ", ".join(running))


@contextmanager
def _single_run_lock(config_file: Path) -> Iterator[None]:
    path = config_file.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("另一个 desktop planner 正在运行") from error
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("另一个 desktop planner 正在运行") from error
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".maa_desktop_planner.bak")


def _atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=path.name + ".", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _merge(target: dict[str, Any], patch: GuiPatch) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _prepared_config(profile: Profile, settings: Settings) -> bytes:
    current = json.loads(profile.gui_config.read_text(encoding="utf-8-sig"))
    if not isinstance(current, dict):
        raise ValueError(f"无效的 gui.new.json: {profile.gui_config}")
    update = current.get("Update")
    if not isinstance(update, dict):
        raise ValueError(f"缺少 Update 配置: {profile.gui_config}")
    if update.get("UpdatePackage"):
        raise RuntimeError(f"存在待安装软件更新包: {profile.gui_config}")
    current_name = current.get("Current")
    configuration = current.get("Configurations", {}).get(current_name)
    if not isinstance(configuration, dict) or not isinstance(configuration.get("Gui"), dict):
        raise ValueError(f"缺少当前 profile 的 Gui 配置: {profile.gui_config}")

    hook_patch = resolve_hook(settings.hook)(profile, json.loads(json.dumps(current)))
    if not isinstance(hook_patch, dict):
        raise TypeError(f"{profile.name}: planner hook 必须返回 dict")
    _merge(current, hook_patch)

    # Safety settings are applied last, even when a hook returns Update fields.
    _merge(
        current,
        {
            "Update": {
                "CheckOnStartup": False,
                "CheckOnSchedule": False,
                "AutoDownloadUpdatePackage": False,
                "AutoInstallUpdatePackage": False,
                "UpdatePackage": "",
            }
        },
    )
    gui = current["Configurations"][current_name]["Gui"]
    if settings.run_directly:
        gui.setdefault("StartUpSettings", {})["RunDirectly"] = True
    if settings.exit_self:
        gui["PostActions"] = "ExitSelf"
    return (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _restore_one(path: Path) -> None:
    backup = _backup_path(path)
    if backup.exists():
        _atomic_write(path, backup.read_bytes())
        backup.unlink()


def restore_backups(settings: Settings) -> None:
    if os.name != "nt":
        raise OSError("恢复 Windows MAA 配置需要在 Windows Python 中执行")
    with _single_run_lock(settings.config_file):
        ensure_stopped(settings.profiles)
        for profile in settings.profiles:
            _restore_one(profile.gui_config)


def _run_locked(settings: Settings) -> None:
    ensure_stopped(settings.profiles)
    for profile in settings.profiles:
        if not profile.exe.is_file() or not profile.gui_config.is_file():
            raise FileNotFoundError(f"安装不完整: {profile.path}")
        if profile.gui_config.is_symlink():
            raise ValueError(f"gui.new.json 不应是共享链接: {profile.gui_config}")
        if _backup_path(profile.gui_config).exists():
            raise RuntimeError(f"发现未恢复的配置备份，请先运行 restore: {profile.name}")
    for profile in settings.profiles:
        result = check_software(profile.path, profile.channel)
        print(f"{profile.name}: 软件 {result.current} -> {result.latest}")
        if result.available:
            package = plan_software_package(result.latest, profile.channel, profile.architecture)
            print(f"{profile.name}: 可更新，完整包 {package.url}")

    resources = {_path_key(profile.resource): profile.resource for profile in settings.profiles}
    for resource in resources.values():
        result = check_resource(resource)
        print(f"资源 {resource}: {result.current} -> {result.latest}")
        if result.available and settings.update_resource:
            update_resource(resource)

    ensure_stopped(settings.profiles)
    prepared = {profile: _prepared_config(profile, settings) for profile in settings.profiles}
    active: list[Profile] = []
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for profile in settings.profiles:
            config = profile.gui_config
            _atomic_write(_backup_path(config), config.read_bytes())
            active.append(profile)
            _atomic_write(config, prepared[profile])
        for profile in settings.profiles:
            processes.append(subprocess.Popen([str(profile.exe)], cwd=profile.path))
            print(f"{profile.name}: 已启动 PID {processes[-1].pid}")
        failures = [process.wait() for process in processes]
        if any(failures):
            raise RuntimeError(f"MAA 退出码: {failures}")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait()
        for profile in reversed(active):
            _restore_one(profile.gui_config)


def run(settings: Settings) -> None:
    if os.name != "nt":
        raise OSError("运行 Windows MAA.exe 需要在 Windows Python 中执行")
    with _single_run_lock(settings.config_file):
        _run_locked(settings)

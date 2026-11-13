"""Compare installed and remote metadata; download only for an actual update."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from maa_planner.daemon.adapters.mumu import Instance, MuMu, match_profiles
from maa_planner.daemon.adapters.windows import require_windows_host, windows_path
from maa_planner.daemon.config import Profile, Settings
from maa_planner.daemon.constants import (
    BUSINESS_TIMEZONE,
    GAME_APK_HOST,
    GAME_LATEST_URL,
    GAME_PACKAGE,
)
from maa_planner.daemon.tools.apk import ApkInfo, read_apk
from maa_planner.daemon.tools.cache_cleanup import mark_apk_used
from maa_planner.daemon.tools.locks import (
    BusyError,
    Resource,
    acquire,
    emulator_resource,
    identity,
    maa_resource,
)
from maa_planner.daemon.tools.remote_apk import read_remote_apk, validate_url


@dataclass(frozen=True)
class LatestApk:
    url: str
    metadata: ApkInfo
    checked_at: str


@dataclass(frozen=True)
class UnitApkCheck:
    profile: str
    instance_index: int
    status: str
    current: ApkInfo | None = None
    reason: str = ""
    resources: dict | None = None


@dataclass(frozen=True)
class GameUpdateReport:
    latest: LatestApk
    units: tuple[UnitApkCheck, ...]


def resolve_latest(latest_url: str, *, allowed_host: str) -> str:
    # HEAD is unsupported. Close the final GET without consuming the APK body.
    request = urllib.request.Request(latest_url, headers={"User-Agent": "maa-planner"})
    with urllib.request.urlopen(request, timeout=20) as response:
        url = response.geturl()
    validate_url(url, allowed_host=allowed_host)
    return url


def download(url: str, path: Path, *, allowed_host: str) -> None:
    validate_url(url, allowed_host=allowed_host)
    temporary = path.with_suffix(".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "maa-planner"})
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            temporary.open("wb") as output,
        ):
            if response.geturl() != url:
                raise ValueError("下载期间 APK 地址改变，请重新检查")
            expected = response.headers.get("Content-Length")
            if expected and shutil.disk_usage(path.parent).free < int(expected) + 1024**3:
                raise OSError("APK 缓存空间不足（需要额外保留 1 GiB）")
            shutil.copyfileobj(response, output, length=1024 * 1024)
            if expected and output.tell() != int(expected):
                raise OSError("APK 下载不完整")
        read_apk(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def installed_info(output: str, package_name: str) -> ApkInfo:
    code = re.search(r"\bversionCode=(\d+)\b", output)
    name = re.search(r"\bversionName=([^\r\n]+)", output)
    if code is None or name is None:
        raise RuntimeError("目标实例未安装官服客户端，或无法读取版本；拒绝当成新安装")
    return ApkInfo(package_name, int(code[1]), name[1].strip())


def refuse_downgrade(current: ApkInfo, target: ApkInfo) -> None:
    if current.package != target.package:
        raise ValueError("APK 包名不匹配")
    if current.version_code > target.version_code:
        raise RuntimeError(f"拒绝降级: {current} -> {target}")
    if current.version_code == target.version_code:
        if re.fullmatch(r"\d+(\.\d+)*", current.version_name) and re.fullmatch(
            r"\d+(\.\d+)*", target.version_name
        ):
            if tuple(map(int, current.version_name.split("."))) > tuple(
                map(int, target.version_name.split("."))
            ):
                raise RuntimeError(f"拒绝版本名降级: {current} -> {target}")


def comparison(current: ApkInfo, target: ApkInfo) -> str:
    if current == target:
        return "up_to_date"
    try:
        refuse_downgrade(current, target)
    except RuntimeError:
        return "ahead"
    return "update_required"


def cache_resource(folder: Path) -> Resource:
    return Resource("artifact:" + identity(folder))


def latest_apk(latest_url: str, package_name: str, *, allowed_host: str) -> LatestApk:
    url = resolve_latest(latest_url, allowed_host=allowed_host)
    info = read_remote_apk(url, allowed_host=allowed_host)
    if info.package != package_name:
        raise ValueError(f"不是官服 APK: {info.package}")
    return LatestApk(url, info, datetime.now(BUSINESS_TIMEZONE).isoformat())


class _Artifact:
    """Reuse one transient APK across all accounts in an update operation."""

    def __init__(self, folder: Path, latest: LatestApk, allowed_host: str):
        self.folder = folder
        self.latest = latest
        self.allowed_host = allowed_host
        self.error: Exception | None = None

    def get(self) -> Path:
        # Caller holds the artifact lock throughout installation, so the path is stable.
        if self.error is not None:
            raise RuntimeError(f"本次 APK 下载/校验已失败，不重复下载: {self.error}")
        try:
            path = self.folder / "latest.apk"
            try:
                cached_info = read_apk(path) if path.is_file() else None
            except (ValueError, zipfile.BadZipFile, KeyError):
                cached_info = None
            if cached_info != self.latest.metadata:
                self.folder.mkdir(parents=True, exist_ok=True)
                print(f"下载一次供本次更新使用: {path}", flush=True)
                download(self.latest.url, path, allowed_host=self.allowed_host)
                if read_apk(path) != self.latest.metadata:
                    raise ValueError("完整 APK 与刚刚读取的远端 metadata 不符，请重新检查")
            mark_apk_used(path)
            return path
        except Exception as error:
            self.error = error
            raise


def _read_installed(mumu: MuMu, address: str, package_name: str) -> ApkInfo:
    return installed_info(
        mumu.adb(address, "shell", "dumpsys", "package", package_name, timeout=10), package_name
    )


def _inspect_one(
    mumu: MuMu, profile: Profile, instance: Instance, latest: LatestApk
) -> UnitApkCheck:
    if not instance.running or not instance.address:
        return UnitApkCheck(
            profile.name, instance.index, "unavailable", reason="模拟器未启动或 ADB 尚未就绪"
        )
    mumu.connect(instance.address)
    current = _read_installed(mumu, instance.address, latest.metadata.package)
    return UnitApkCheck(profile.name, instance.index, comparison(current, latest.metadata), current)


def _process(
    settings: Settings,
    latest: LatestApk,
    *,
    install: bool,
    cancel: threading.Event | None = None,
    allow_emulator_start: bool = True,
    on_install=None,
) -> GameUpdateReport:
    if not install:
        return _process_units(
            settings,
            latest,
            install=False,
            cancel=cancel,
            allow_emulator_start=allow_emulator_start,
            on_install=on_install,
        )
    # Keep one artifact stable for all accounts, and remove it before releasing the lock.
    with acquire([cache_resource(settings.game_cache)]):
        try:
            return _process_units(
                settings,
                latest,
                install=True,
                cancel=cancel,
                allow_emulator_start=allow_emulator_start,
                on_install=on_install,
            )
        finally:
            (settings.game_cache / "latest.apk").unlink(missing_ok=True)
            (settings.game_cache / "latest.used").unlink(missing_ok=True)


def _process_units(
    settings: Settings,
    latest: LatestApk,
    *,
    install: bool,
    cancel: threading.Event | None = None,
    allow_emulator_start: bool = True,
    on_install=None,
) -> GameUpdateReport:
    assert settings.mumu_path is not None
    mumu = MuMu(settings.mumu_path)
    mapping = match_profiles(settings.profiles, mumu.instances())
    artifact = _Artifact(settings.game_cache, latest, GAME_APK_HOST)
    observations = []
    print(f"官网 APK: {latest.url} ({latest.metadata.version_name})")
    for profile, instance in mapping.items():
        if cancel is not None and cancel.is_set():
            observations.append(
                UnitApkCheck(profile.name, instance.index, "unavailable", reason="检查已取消")
            )
            continue
        if install and not allow_emulator_start and (not instance.running or not instance.address):
            observations.append(
                UnitApkCheck(
                    profile.name,
                    instance.index,
                    "deferred",
                    reason="模拟器未启动或 ADB 未就绪，自动更新不启动模拟器",
                )
            )
            continue
        try:
            emulator = emulator_resource(settings.mumu_path, instance.index)
            locks = (
                [
                    emulator,
                    *([maa_resource(profile.gui_path)] if profile.gui_path is not None else []),
                ]
                if install
                else [Resource(emulator.key, shared=True)]
            )
            with acquire(locks):
                if not install:
                    result = _inspect_one(mumu, profile, instance, latest)
                else:
                    with mumu.running(instance) as ready:
                        assert ready.address is not None
                        current = _read_installed(mumu, ready.address, latest.metadata.package)
                        status = comparison(current, latest.metadata)
                        result = UnitApkCheck(profile.name, instance.index, status, current)
                        if status == "update_required":
                            apk = artifact.get()
                            # Recheck facts immediately before any mutating action.
                            current = _read_installed(mumu, ready.address, latest.metadata.package)
                            status = comparison(current, latest.metadata)
                            if status == "update_required":
                                if cancel is not None and cancel.is_set():
                                    raise InterruptedError("APK 安装前取消")
                                if on_install is not None:
                                    on_install(profile, latest.metadata)
                                # Official client updates are mandatory. An outdated game
                                # cannot run useful tasks; an open MAA GUI is not a blocker.
                                mumu.adb(
                                    ready.address,
                                    "shell",
                                    "am",
                                    "force-stop",
                                    latest.metadata.package,
                                )
                                output = mumu.adb(
                                    ready.address,
                                    "install",
                                    "-r",
                                    windows_path(apk),
                                    timeout=900,
                                )
                                if not re.search(r"(?m)^Success\s*$", output):
                                    raise RuntimeError(f"APK 安装失败: {output}")
                                actual = _read_installed(
                                    mumu, ready.address, latest.metadata.package
                                )
                                if actual != latest.metadata:
                                    raise RuntimeError("安装后的 metadata 与官网不符")
                                result = UnitApkCheck(
                                    profile.name, instance.index, "updated", actual
                                )
                            else:
                                result = UnitApkCheck(profile.name, instance.index, status, current)
        except BusyError as error:
            result = UnitApkCheck(profile.name, instance.index, "busy", reason=str(error))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            result = UnitApkCheck(
                profile.name,
                instance.index,
                "unavailable" if not install else "error",
                reason=str(error),
            )
        observations.append(result)
        print(
            f"{profile.name}: {result.status}" + (f" ({result.reason})" if result.reason else ""),
            flush=True,
        )
    return GameUpdateReport(latest, tuple(observations))


def check_game_updates(
    settings: Settings, *, cancel: threading.Event | None = None
) -> GameUpdateReport:
    require_windows_host()
    if settings.mumu_path is None:
        raise ValueError("请先配置 [mumu].path")
    latest = latest_apk(
        GAME_LATEST_URL,
        GAME_PACKAGE,
        allowed_host=GAME_APK_HOST,
    )
    return _process(settings, latest, install=False, cancel=cancel)


def update_game(
    settings: Settings,
    *,
    check_only: bool = False,
    allow_emulator_start: bool = True,
    cancel: threading.Event | None = None,
    on_install=None,
    raise_on_failure=True,
) -> GameUpdateReport:
    if check_only:
        return check_game_updates(settings)
    require_windows_host()
    if settings.mumu_path is None:
        raise ValueError("请先配置 [mumu].path")
    latest = latest_apk(
        GAME_LATEST_URL,
        GAME_PACKAGE,
        allowed_host=GAME_APK_HOST,
    )
    report = _process(
        settings,
        latest,
        install=True,
        cancel=cancel,
        allow_emulator_start=allow_emulator_start,
        on_install=on_install,
    )
    failures = [
        f"{unit.profile}: {unit.reason}"
        for unit in report.units
        if unit.status in {"busy", "error", "unavailable"}
    ]
    if failures and raise_on_failure:
        raise RuntimeError("部分实例未完成，后续运行将重新读取 metadata：\n" + "\n".join(failures))
    return report

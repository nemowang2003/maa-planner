"""Prepare isolated Core/resources with a native Python host, preserving GUI installs."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from maa_planner.daemon.constants import GITHUB_API_URL
from maa_planner.daemon.execution.runtime import (
    CoreRuntime,
    probe_runtime,
)
from maa_planner.daemon.tools.cache_cleanup import touch_cache
from maa_planner.daemon.tools.locks import acquire, data_resource


def _json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "maa-planner"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _download(asset: dict, destination: Path) -> None:
    request = urllib.request.Request(
        asset["browser_download_url"], headers={"User-Agent": "maa-planner"}
    )
    digest = hashlib.sha256()
    count = 0
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        while block := response.read(1024 * 1024):
            digest.update(block)
            count += len(block)
            output.write(block)
    if asset.get("size") and count != asset["size"]:
        raise ValueError("发布包下载长度不符")
    expected = asset.get("digest")
    if expected and expected != "sha256:" + digest.hexdigest():
        raise ValueError("发布包 SHA256 不符")


def _extract(source: Path, destination: Path) -> None:
    destination.mkdir()
    if not zipfile.is_zipfile(source):
        try:
            with tarfile.open(source) as archive:
                archive.extractall(destination, filter="data")
        except tarfile.TarError as error:
            raise ValueError(f"发布包包含不安全路径或无效 tar: {error}") from error
        return
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in entry.filename
                or ":" in entry.filename
            ):
                raise ValueError("发布包包含不安全路径")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("发布包包含符号链接")
        archive.extractall(destination)


@dataclass(frozen=True)
class LatestReleases:
    core_version: str
    core: dict


def latest_releases(github_api_url: str) -> LatestReleases:
    def release(repo):
        value = _json(
            f"{github_api_url.rstrip('/')}/repos/MaaAssistantArknights/{repo}/releases/latest"
        )
        tag = value.get("tag_name")
        if (
            value.get("draft")
            or value.get("prerelease")
            or not isinstance(tag, str)
            or not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[+.-][A-Za-z0-9.-]+)?", tag)
            or not isinstance(value.get("assets"), list)
        ):
            raise ValueError(f"{repo} 最新稳定发布信息无效")
        return value, tag.removeprefix("v")

    core, core_version = release("MaaAssistantArknights")
    return LatestReleases(core_version, core)


def release_asset(release: dict, name: str) -> dict:
    assets = [asset for asset in release["assets"] if asset.get("name") == name]
    if len(assets) != 1:
        raise ValueError(f"最新发布缺少唯一的 {name}，未执行")
    return assets[0]


def prepare_runtime(
    destination: Path,
    *,
    github_api_url: str = GITHUB_API_URL,
    releases: LatestReleases | None = None,
    platform_name: str | None = None,
    cache_directory: Path | None = None,
) -> CoreRuntime:
    """Download official Core for the native platform into a fresh verified installation."""
    from platformdirs import user_cache_path

    releases = releases or latest_releases(github_api_url)
    platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
    if platform_name != ("windows" if os.name == "nt" else "linux"):
        raise ValueError("不支持跨系统安装或执行 Core")
    architecture = {
        "x86_64": "x86_64",
        "AMD64": "x86_64",
        "aarch64": "aarch64",
        "ARM64": "aarch64",
    }.get(platform.machine())
    if architecture is None or platform_name not in {"windows", "linux"}:
        raise ValueError("不支持的运行平台或架构")
    core_version = releases.core_version
    if platform_name == "windows":
        core_asset = release_asset(
            releases.core,
            f"MAA-v{core_version}-win-{'arm64' if architecture == 'aarch64' else 'x64'}.zip",
        )
    else:
        core_asset = release_asset(
            releases.core, f"MAA-v{core_version}-linux-{architecture}.tar.gz"
        )
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("运行依赖必须准备到新目录；不会覆盖已有目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cache = cache_directory or user_cache_path("maa-planner", appauthor=False)
    with (
        acquire([data_resource(destination)]),
        tempfile.TemporaryDirectory(prefix=".maa-prepare-", dir=destination.parent) as temp,
    ):
        root = Path(temp)
        for name, asset in [("core", core_asset)]:
            archive = cached_asset(asset, cache / "downloads")
            _extract(archive, root / name)
        staged = root / "runtime"
        (staged / "data").mkdir(parents=True)
        shutil.move(root / "core" / "resource", staged / "data" / "resource")
        shutil.move(root / "core", staged / "data" / "lib")
        runtime = CoreRuntime(
            Path(sys.executable),
            staged / "data",
            staged / "navigation",
            destination.parent / "tmp",
            expected_core_version=core_version,
        )
        runtime.validate(check_hot_update=False)
        print(probe_runtime(runtime), flush=True)
        staged.rename(destination)
    return replace(
        runtime,
        data_dir=destination / "data",
        navigation=destination / "navigation",
        library_dir=destination / "data/lib",
        resource_dir=destination / "data/resource",
    )


def cached_asset(asset: dict, cache: Path) -> Path:
    """The archive is disposable; installed binaries live outside the cache."""
    key = hashlib.sha256(asset.get("browser_download_url", asset["name"]).encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / key
    with acquire([data_resource(path)]):
        valid = path.is_file()
        if valid and asset.get("size"):
            valid = path.stat().st_size == asset["size"]
        if valid and asset.get("digest"):
            with path.open("rb") as stream:
                valid = (
                    asset["digest"] == "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                )
        if not valid:
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                print(f"下载 {asset['name']}", flush=True)
                _download(asset, temporary)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    touch_cache(path)
    return path


def runtime_versions(runtime: CoreRuntime) -> str:
    output = probe_runtime(replace(runtime, expected_core_version=None))
    return output.removeprefix("MaaCore v").strip()


def ensure_latest_runtime(settings) -> CoreRuntime:
    """Observe the latest Core release once and prepare daemon-owned immutable binaries."""
    current = settings.runtime_template()
    latest = latest_releases(GITHUB_API_URL)
    wanted = latest.core_version
    platform_name = settings.runtime_platform
    architecture = platform.machine().lower()
    destination = settings.runtime_directory / f"core-{wanted}-{platform_name}-{architecture}"
    with acquire([data_resource(settings.runtime_directory)]):
        if destination.exists():
            candidate = CoreRuntime(
                Path(sys.executable),
                destination / "data",
                destination / "navigation",
                settings.runtime_directory / "tmp",
                library_dir=destination / "data/lib",
                resource_dir=destination / "data/resource",
                expected_core_version=wanted,
            )
        else:
            candidate = prepare_runtime(
                destination,
                releases=latest,
                platform_name=platform_name,
                cache_directory=settings.cache_directory,
            )
        candidate.validate(check_hot_update=False)
        if runtime_versions(candidate) != wanted:
            raise ValueError("安装目录的实际版本与最新发布不符；未执行")
        selected = replace(
            candidate,
            work_dir=current.work_dir,
            resource_dir=settings.shared_resource_dir or candidate.resources,
            adb_path=current.adb_path,
            connection_preset=current.connection_preset,
            touch_mode=current.touch_mode,
            game_package=current.game_package,
        )
        selected.validate(check_hot_update=False)
        if runtime_versions(selected) != wanted:
            raise ValueError("Core 最新版加载验证失败；未执行")
        return selected

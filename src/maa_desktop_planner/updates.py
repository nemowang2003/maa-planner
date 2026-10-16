"""Read-only software checks and incremental MaaResource updates.

The desktop software installer is deliberately separate: replacing a complete MAA
installation requires preserving each profile's private config and data.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

MAA_API = "https://api.maa.plus/MaaAssistantArknights/api/version/summary.json"
RESOURCE_VERSION_URL = (
    "https://raw.githubusercontent.com/MaaAssistantArknights/MaaResource/main/resource/version.json"
)
RESOURCE_ARCHIVE_URL = (
    "https://github.com/MaaAssistantArknights/MaaResource/archive/refs/heads/main.zip"
)
RESOURCE_PREFIX = PurePosixPath("MaaResource-main/resource")
CHANNELS = ("stable", "beta", "alpha")
_SEMVER = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "maa_desktop_planner"})
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object from {url}")
    return value


def installed_version(profile: Path) -> str:
    """Use the same MaaCore export that the WPF updater uses."""
    if os.name != "nt":
        raise OSError("Reading MaaCore.dll requires Windows; pass --current-version instead")
    profile = profile.resolve()
    core = profile / "MaaCore.dll"
    if not core.is_file():
        raise FileNotFoundError(core)
    # Python 3.8+ does not search the current directory for DLL dependencies.
    with os.add_dll_directory(str(profile)), os.add_dll_directory(str(profile / "externals")):
        dll = ctypes.CDLL(str(core))
        dll.AsstGetVersion.restype = ctypes.c_char_p
        version = dll.AsstGetVersion()
    if not version:
        raise ValueError(f"AsstGetVersion returned no version: {core}")
    return version.decode("utf-8")


def _version_key(value: str) -> tuple:
    match = _SEMVER.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid MAA version: {value}")
    numbers = tuple(int(match[name]) for name in ("major", "minor", "patch"))
    prerelease = match["prerelease"]
    if prerelease is None:
        return (*numbers, 1, ())
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split(".")
    )
    return (*numbers, 0, identifiers)


@dataclass(frozen=True)
class SoftwareUpdate:
    current: str
    latest: str
    channel: str
    available: bool


def check_software(
    profile: Path, channel: str = "stable", current_version: str | None = None
) -> SoftwareUpdate:
    if channel not in CHANNELS:
        raise ValueError(f"Unsupported channel: {channel}")
    current = current_version or installed_version(profile)
    summary = _get_json(MAA_API)
    latest = summary[channel]["version"]
    return SoftwareUpdate(current, latest, channel, _version_key(current) < _version_key(latest))


@dataclass(frozen=True)
class SoftwarePackage:
    version: str
    architecture: str
    name: str
    url: str


def plan_software_package(version: str, channel: str, architecture: str) -> SoftwarePackage:
    """Resolve the exact full Windows package; downloading/installing is a later step."""
    if channel not in CHANNELS:
        raise ValueError(f"Unsupported channel: {channel}")
    if architecture not in ("x64", "arm64"):
        raise ValueError(f"Unsupported architecture: {architecture}")
    details = _get_json(f"https://api.maa.plus/MaaAssistantArknights/api/version/{channel}.json")
    if details["version"] != version:
        raise ValueError(f"Release changed while checking: {version} -> {details['version']}")
    name = f"MAA-{version}-win-{architecture}.zip"
    assets = details["details"]["assets"]
    matching = [asset for asset in assets if asset.get("name") == name]
    # Prefer the main repository when the API also lists MaaRelease mirrors.
    matching.sort(
        key=lambda asset: (
            "MaaAssistantArknights/MaaAssistantArknights"
            not in asset.get("browser_download_url", "")
        )
    )
    if not matching:
        raise ValueError(f"Full package not found: {name}")
    return SoftwarePackage(version, architecture, name, matching[0]["browser_download_url"])


def _resource_time(value: dict) -> datetime:
    return datetime.strptime(value["last_updated"], "%Y-%m-%d %H:%M:%S.%f")


@dataclass(frozen=True)
class ResourceUpdate:
    current: datetime
    latest: datetime
    available: bool


def check_resource(resource_dir: Path) -> ResourceUpdate:
    current = _resource_time(
        json.loads((resource_dir / "version.json").read_text(encoding="utf-8"))
    )
    latest = _resource_time(_get_json(RESOURCE_VERSION_URL))
    return ResourceUpdate(current, latest, latest > current)


def update_resource(resource_dir: Path) -> ResourceUpdate:
    """Merge only archive/resource files; keep old files required by incremental packs."""
    resource_dir = resource_dir.resolve(strict=True)
    if not resource_dir.is_dir():
        raise NotADirectoryError(resource_dir)
    status = check_resource(resource_dir)
    if not status.available:
        return status

    with tempfile.TemporaryDirectory(prefix="maa-resource-") as temporary:
        archive_path = Path(temporary) / "resource.zip"
        request = urllib.request.Request(
            RESOURCE_ARCHIVE_URL, headers={"User-Agent": "maa_desktop_planner"}
        )
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            archive_path.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)

        with zipfile.ZipFile(archive_path) as archive:
            version_name = "MaaResource-main/resource/version.json"
            archive_time = _resource_time(json.loads(archive.read(version_name)))
            if archive_time <= status.current:
                return ResourceUpdate(status.current, archive_time, False)

            entries = []
            for info in archive.infolist():
                if "\\" in info.filename or ":" in info.filename:
                    raise ValueError(f"Unsafe archive path: {info.filename}")
                path = PurePosixPath(info.filename)
                if info.is_dir() or path == RESOURCE_PREFIX:
                    continue
                if not path.is_relative_to(RESOURCE_PREFIX):
                    continue
                relative = path.relative_to(RESOURCE_PREFIX)
                if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
                    raise ValueError(f"Unsafe archive path: {info.filename}")
                # Reject Unix symlinks; the archive must contain regular files only.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Symlink in archive: {info.filename}")
                entries.append((info, relative))

            # Write version.json last so a failed merge does not claim success.
            entries.sort(key=lambda item: item[1] == PurePosixPath("version.json"))
            for info, relative in entries:
                destination = resource_dir.joinpath(*relative.parts)
                for parent in (destination, *destination.parents):
                    if parent == resource_dir:
                        break
                    if parent.is_symlink():
                        raise ValueError(f"Symlink inside resource tree: {parent}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                    temp_path = Path(output.name)
                    try:
                        with archive.open(info) as source:
                            shutil.copyfileobj(source, output)
                    except BaseException:
                        temp_path.unlink(missing_ok=True)
                        raise
                try:
                    os.replace(temp_path, destination)
                except BaseException:
                    temp_path.unlink(missing_ok=True)
                    raise
    return ResourceUpdate(status.current, archive_time, True)

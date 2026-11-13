"""Explicit shared MaaResource maintenance, outside account runners."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from maa_planner.daemon.constants import BUSINESS_TIMEZONE


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "maa_planner.runtime"})
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object from {url}")
    return value


def _resource_time(value: dict) -> datetime:
    return datetime.strptime(value["last_updated"], "%Y-%m-%d %H:%M:%S.%f").replace(
        tzinfo=BUSINESS_TIMEZONE
    )


@dataclass(frozen=True)
class ResourceUpdate:
    current: datetime
    latest: datetime
    available: bool


def check_resource(resource_dir: Path, *, version_url: str) -> ResourceUpdate:
    current = _resource_time(
        json.loads((resource_dir / "version.json").read_text(encoding="utf-8"))
    )
    latest = _resource_time(_get_json(version_url))
    return ResourceUpdate(current, latest, latest > current)


def _update_resource(
    resource_dir: Path,
    *,
    version_url: str,
    archive_url: str,
    archive_prefix: str,
    cache_directory: Path | None = None,
    observed: ResourceUpdate | None = None,
) -> ResourceUpdate:
    """Merge only archive/resource files; keep old files required by incremental packs."""
    resource_dir = resource_dir.resolve(strict=True)
    if not resource_dir.is_dir():
        raise NotADirectoryError(resource_dir)
    prefix = PurePosixPath(archive_prefix)
    status = observed or check_resource(resource_dir, version_url=version_url)
    if (
        observed is not None
        and _resource_time(json.loads((resource_dir / "version.json").read_text(encoding="utf-8")))
        != observed.current
    ):
        raise ValueError("资源版本在检查后改变，拒绝使用旧的更新观察")
    if not status.available:
        return status

    from platformdirs import user_cache_path

    downloads = (cache_directory or user_cache_path("maa-planner", appauthor=False)) / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="maa-resource-", dir=downloads) as temporary:
        archive_path = Path(temporary) / "resource.zip"
        backup_root = Path(temporary) / "backup"
        request = urllib.request.Request(archive_url, headers={"User-Agent": "maa_planner.runtime"})
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            archive_path.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)

        with zipfile.ZipFile(archive_path) as archive:
            version_name = str(prefix / "version.json")
            archive_time = _resource_time(json.loads(archive.read(version_name)))
            if archive_time <= status.current:
                return ResourceUpdate(status.current, archive_time, False)

            entries = []
            seen: set[PurePosixPath] = set()
            for info in archive.infolist():
                if "\\" in info.filename or ":" in info.filename:
                    raise ValueError(f"Unsafe archive path: {info.filename}")
                path = PurePosixPath(info.filename)
                if info.is_dir() or path == prefix:
                    continue
                if not path.is_relative_to(prefix):
                    continue
                relative = path.relative_to(prefix)
                if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
                    raise ValueError(f"Unsafe archive path: {info.filename}")
                if relative in seen:
                    raise ValueError(f"Duplicate archive path: {info.filename}")
                seen.add(relative)
                # Reject Unix symlinks; the archive must contain regular files only.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Symlink in archive: {info.filename}")
                entries.append((info, relative))

            # Write version.json last so a failed merge does not claim success.
            entries.sort(key=lambda item: item[1] == PurePosixPath("version.json"))
            changes: list[tuple[Path, Path | None]] = []
            staged: list[Path] = []
            created_directories: list[Path] = []
            try:
                for info, relative in entries:
                    destination = resource_dir.joinpath(*relative.parts)
                    if destination.is_symlink():
                        raise ValueError(f"Symlink inside resource tree: {destination}")
                    for parent in (destination, *destination.parents):
                        if parent == resource_dir:
                            break
                        if parent.is_symlink():
                            raise ValueError(f"Symlink inside resource tree: {parent}")

                    missing = []
                    parent = destination.parent
                    while parent != resource_dir and not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    created_directories.extend(reversed(missing))

                    backup = None
                    if destination.exists():
                        backup = backup_root.joinpath(*relative.parts)
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(destination, backup)

                    with tempfile.NamedTemporaryFile(
                        prefix=f".{destination.name}.", dir=destination.parent, delete=False
                    ) as output:
                        temp_path = Path(output.name)
                    staged.append(temp_path)
                    with archive.open(info) as source, temp_path.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    os.replace(temp_path, destination)
                    staged.remove(temp_path)
                    changes.append((destination, backup))
            except BaseException:
                for temp_path in staged:
                    temp_path.unlink(missing_ok=True)
                _rollback_resource(changes, created_directories)
                raise
    return ResourceUpdate(status.current, archive_time, True)


def _rollback_resource(changes: list[tuple[Path, Path | None]], created: list[Path]) -> None:
    """Restore every replaced file and remove files/directories created by this update."""
    rollback_errors: list[Exception] = []
    for destination, backup in reversed(changes):
        try:
            if backup is None:
                destination.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile(
                    prefix=f".{destination.name}.", dir=destination.parent, delete=False
                ) as output:
                    restore = Path(output.name)
                try:
                    shutil.copy2(backup, restore)
                    os.replace(restore, destination)
                except BaseException:
                    restore.unlink(missing_ok=True)
                    raise
        except Exception as error:
            rollback_errors.append(error)
    for directory in reversed(created):
        try:
            directory.rmdir()
        except OSError as error:
            # Existing non-empty directories belong to the prior tree and must be retained.
            if directory.exists() and any(directory.iterdir()):
                continue
            rollback_errors.append(error)
    if rollback_errors:
        raise RuntimeError("资源更新失败，且旧文件未能完整恢复") from rollback_errors[0]


def update_resource(
    resource_dir: Path,
    *,
    version_url: str,
    archive_url: str,
    archive_prefix: str,
    cache_directory: Path | None = None,
    observed: ResourceUpdate | None = None,
) -> ResourceUpdate:
    from maa_planner.daemon.tools.locks import acquire, data_resource

    with acquire([data_resource(resource_dir)]):
        return _update_resource(
            resource_dir,
            version_url=version_url,
            archive_url=archive_url,
            archive_prefix=archive_prefix,
            cache_directory=cache_directory,
            observed=observed,
        )

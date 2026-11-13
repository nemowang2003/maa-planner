"""Bounded retention for recognized, rebuildable daemon caches."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from maa_planner.daemon.tools.locks import BusyError, acquire, data_resource

DAY = 86400
HEX = r"[0-9a-f]"
RETIRED = re.compile(r"\.cleanup-[0-9a-f]{32}")
CORE = re.compile(r"core-(\d+)\.(\d+)\.(\d+)-(windows|linux)-(amd64|x86_64|arm64|aarch64)")


class CleanupReport(TypedDict):
    removed_entries: int
    freed_bytes: int
    busy_entries: int
    errors: list[str]


@dataclass(frozen=True)
class CleanupPolicy:
    enabled: bool = True
    download_days: int = 3
    cache_days: int = 3
    core_keep: int = 2
    apk_days: int = 1

    @classmethod
    def parse(cls, value):
        fields = {"enabled", "download_days", "cache_days", "core_keep", "apk_days"}
        if not isinstance(value, dict) or value.keys() - fields:
            raise ValueError("[cleanup] 包含未知字段")
        policy = cls(**value)
        if (
            type(policy.enabled) is not bool
            or any(
                type(v) is not int or not 1 <= v <= 3650
                for v in (policy.download_days, policy.cache_days, policy.apk_days)
            )
            or type(policy.core_keep) is not int
            or not 1 <= policy.core_keep <= 100
        ):
            raise ValueError("cleanup 天数须为 1–3650 的整数，core_keep 须为 1–100 的整数")
        return policy


def touch_cache(path: Path) -> None:
    """Record successful reuse without relying on the host's optional access-time updates."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def mark_apk_used(path: Path) -> None:
    # APK mtime is part of the immutable source descriptor; track reuse separately.
    from maa_planner.daemon.tools.storage import atomic_write

    try:
        atomic_write(path.with_suffix(".used"), b"")
    except OSError:
        pass


def linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def tree_info(path: Path):
    """Refuse reparse points anywhere in a candidate, including its descendants."""
    if linked(path):
        return None
    stat = path.stat()
    size = stat.st_size if path.is_file() else 0
    modified = stat.st_mtime

    def failed(error):
        raise error

    if path.is_dir():
        for root, directories, files in os.walk(path, followlinks=False, onerror=failed):
            for name in (*directories, *files):
                child = Path(root) / name
                if linked(child):
                    return None
                stat = child.stat()
                modified = max(modified, stat.st_mtime)
                if child.is_file():
                    size += stat.st_size
    return modified, size


def recovery_pending(root: Path) -> bool:
    """A fence may outlive SQL admission; unreadable evidence is never disposable."""
    try:
        for name in ("core-execution.json", "cli-execution.json"):
            for path in root.rglob(name):
                if (
                    linked(path)
                    or json.loads(path.read_text(encoding="utf-8")).get("phase") != "finished"
                ):
                    return True
    except (OSError, ValueError, AttributeError):
        return True
    return False


def clean_cache(settings, *, protected=(), now=None):
    policy = settings.cleanup
    report: CleanupReport = {
        "removed_entries": 0,
        "freed_bytes": 0,
        "busy_entries": 0,
        "errors": [],
    }
    if not policy.enabled:
        return report
    now = time.time() if now is None else now
    cache = settings.cache_directory
    template = settings.runtime_template()
    protected = tuple(
        Path(p).resolve()
        for p in (
            *protected,
            settings.config_file,
            settings.skland_auth_dir,
            template.data_dir,
            template.library,
            template.resources,
            template.navigation,
        )
    )

    def remove(path, root, *, days=None, resources=(), stamp=None, retired=False):
        try:
            if linked(root) or linked(path):
                return
            resolved = path.resolve()
            if not resolved.is_relative_to(root.resolve()) or any(
                p == resolved or p.is_relative_to(resolved) or resolved.is_relative_to(p)
                for p in protected
            ):
                return
            with acquire([data_resource(root), data_resource(path), *resources]):
                info = tree_info(path)
                if stamp is not None and stamp.exists():
                    if linked(stamp):
                        return
                    if info is not None:
                        info = (max(info[0], stamp.stat().st_mtime), info[1])
                if info is None or (days is not None and info[0] >= now - days * DAY):
                    return
                if path.is_dir():
                    if not retired:
                        destination = path.with_name(".cleanup-" + uuid.uuid4().hex)
                        path.rename(destination)
                        path = destination
                    # Readers either see the intact cache or a cache miss, never a half-deleted entry.
                    shutil.rmtree(path)
                else:
                    path.unlink()
                if stamp is not None:
                    stamp.unlink(missing_ok=True)
                report["removed_entries"] += 1
                report["freed_bytes"] += info[1]
        except BusyError:
            report["busy_entries"] += 1
        except FileNotFoundError:
            pass
        except OSError as error:
            report["errors"].append(f"{path.name}: {error}")

    # Exact layouts only: no blanket rglob deletion of a user-selected directory.
    groups = [
        ("apk", r"latest\.(apk|part)", False, policy.apk_days),
        ("downloads", HEX + "{64}", False, policy.download_days),
        ("hot-update", "(?:" + HEX + "{64}|\\.fetch-[a-z0-9_]{8})", True, policy.cache_days),
        ("stage-catalog", HEX + "{40}\\.json", False, policy.cache_days),
        ("campaign-catalog", HEX + "{40}\\.json", False, policy.cache_days),
        ("processing-catalog", HEX + "{40}\\.json", False, policy.cache_days),
        ("copilot", HEX + "{64}\\.json", False, policy.cache_days),
        (
            "activity-assets/bundles",
            HEX + "{32}(?:" + HEX + "{32})?\\.ab",
            False,
            policy.cache_days,
        ),
        ("activity-assets/indices", HEX + "{32}\\.idx", False, policy.cache_days),
        ("activity-assets/exporter-archives", HEX + "{64}\\.zip", False, policy.download_days),
        ("activity-assets/exporters", HEX + "{64}", True, policy.cache_days),
        ("activity-assets/exports", HEX + "{64}", True, policy.cache_days),
        ("activity-assets/jobs", HEX + "{32}", True, policy.cache_days),
    ]
    for relative, pattern, directory, days in groups:
        root = cache / relative
        try:
            # Check every intermediate component, especially activity-assets.
            if any(
                linked(cache.joinpath(*Path(relative).parts[:i]))
                for i in range(len(Path(relative).parts) + 1)
            ):
                continue
            for entry in list(root.iterdir()) if root.is_dir() else ():
                retired = directory and bool(RETIRED.fullmatch(entry.name))
                if (retired or re.fullmatch(pattern, entry.name)) and (
                    entry.is_dir() if directory else entry.is_file()
                ):
                    parent = (
                        cache / "activity-assets"
                        if relative.startswith("activity-assets/")
                        else root
                    )
                    resources = [data_resource(parent)]
                    stamp = None
                    if relative == "apk":
                        from maa_planner.daemon.tools.game_update import cache_resource

                        resources.append(cache_resource(root))
                        if entry.suffix == ".apk":
                            stamp = entry.with_suffix(".used")
                    remove(
                        entry,
                        root,
                        days=None if retired else days,
                        resources=resources,
                        stamp=stamp,
                        retired=retired,
                    )
        except OSError as error:
            report["errors"].append(f"{relative}: {error}")
    runtime = settings.runtime_directory
    if not linked(runtime) and runtime.is_dir():
        entries = []
        for entry in list(runtime.iterdir()):
            if RETIRED.fullmatch(entry.name) and entry.is_dir():
                remove(entry, runtime, retired=True)
                continue
            match = CORE.fullmatch(entry.name)
            if (
                match
                and not linked(entry)
                and (entry / "data/lib").is_dir()
                and (entry / "data/resource").is_dir()
            ):
                entries.append((tuple(map(int, match.groups()[:3])), match.groups()[3:], entry))
        for group in {v[1] for v in entries}:
            ordered = sorted((v for v in entries if v[1] == group), reverse=True)
            for _, _, entry in ordered[policy.core_keep :]:
                remove(
                    entry,
                    runtime,
                    resources=[
                        data_resource(entry / "data"),
                        data_resource(entry / "data/lib"),
                        data_resource(entry / "data/resource"),
                        data_resource(entry / "navigation"),
                    ],
                )
    return report

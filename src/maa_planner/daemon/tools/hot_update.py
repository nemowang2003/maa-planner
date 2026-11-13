"""One daemon-owned hot-update document set for planning and Core consumers."""

import hashlib
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from maa_planner.daemon.constants import BUSINESS_TIMEZONE, NAVIGATION_API_URL
from maa_planner.daemon.execution.runtime import fetch_navigation, navigation_files
from maa_planner.daemon.planning.activity import (
    ActivitySnapshot,
    parse_activity_data,
    validate_active_recommendations,
)
from maa_planner.daemon.tools.cache_cleanup import touch_cache
from maa_planner.daemon.tools.locks import acquire, data_resource


class HotUpdateError(ValueError):
    """The shared public hot-update document set is unavailable."""


class ActivitySourceError(HotUpdateError):
    """The public activity recommendations are unusable."""


@dataclass(frozen=True)
class HotUpdateSnapshot:
    directory: Path
    activity: ActivitySnapshot


def prepare_hot_update(cache: Path) -> HotUpdateSnapshot:
    try:
        return _prepare_hot_update(cache)
    except HotUpdateError:
        raise
    except Exception as error:
        raise HotUpdateError(f"公共热更新数据不可用: {error}") from error


def _prepare_hot_update(cache: Path) -> HotUpdateSnapshot:
    root = cache / "hot-update"
    root.mkdir(parents=True, exist_ok=True)
    with (
        acquire([data_resource(root)]),
        tempfile.TemporaryDirectory(prefix=".fetch-", dir=root) as temporary,
    ):
        staging = Path(temporary) / "documents"
        fetch_navigation(staging, api_url=NAVIGATION_API_URL)
        files = navigation_files(staging)
        now = datetime.now(BUSINESS_TIMEZONE)
        try:
            data = parse_activity_data(json.loads(files["gui/StageActivityV2.json"]))
            validate_active_recommendations(data, now)
        except Exception as error:
            raise ActivitySourceError(f"公共活动数据不可用: {error}") from error
        digest = hashlib.sha256()
        for name, content in sorted(files.items()):
            digest.update(name.encode() + b"\0" + content + b"\0")
        destination = root / digest.hexdigest()
        if not destination.exists():
            staging.rename(destination)
        else:
            # Do not trust directory names after a user/cache cleaner modified the content.
            if navigation_files(destination) != files:
                raise ValueError("热更新缓存内容损坏")
        touch_cache(destination)
    return HotUpdateSnapshot(
        destination,
        ActivitySnapshot(
            data=data,
            source_url=NAVIGATION_API_URL + "/gui/StageActivityV2.json",
            fetched_at=now,
            checked_at=now,
            expires_at=now + timedelta(minutes=5),
        ),
    )

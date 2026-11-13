import os

from maa_planner.daemon.config import Settings
from maa_planner.daemon.tools.cache_cleanup import CleanupPolicy

DAY = 86400
NOW = 2_000_000_000


def settings(tmp_path):
    return Settings(
        tmp_path / "config.toml",
        (),
        cache_directory=tmp_path / "cache",
        runtime_directory=tmp_path / "runtime",
        cleanup=CleanupPolicy(enabled=True, download_days=3, cache_days=3, core_keep=2),
    )


def old_file(path, *, age=40):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cache")
    os.utime(path, (NOW - age * DAY, NOW - age * DAY))
    return path

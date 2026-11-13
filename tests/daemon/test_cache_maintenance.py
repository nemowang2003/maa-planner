import asyncio

from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.state import StateStore
from tests.helpers.cache_cleanup import settings


def test_idle_daemon_runs_retention_exclusively_and_waits_on_shutdown(tmp_path, monkeypatch):
    from maa_planner.daemon import cache_maintenance as module

    async def scenario():
        daemon = Daemon(
            tmp_path / "config.toml", tmp_path / "control.json", root=tmp_path / "state"
        )
        daemon._settings = settings(tmp_path)
        daemon.store = StateStore(tmp_path / "state.sqlite3")
        cleaner = daemon.cache_maintenance
        began = asyncio.Event()
        release = asyncio.Event()

        async def execute(*args, **kwargs):
            assert daemon.maintenance == "cache-cleanup"
            began.set()
            await release.wait()
            return {"removed_entries": 1}

        monkeypatch.setattr(module.asyncio, "to_thread", execute)
        daemon.reserve_resources("preparation")
        cleaner.next_at = 0
        assert cleaner.tick() is not None and cleaner.task is None
        daemon.release_resources("preparation")
        cleaner.next_at = 0
        assert cleaner.tick() is None
        await began.wait()
        assert daemon.resources_busy
        close = asyncio.create_task(cleaner.close())
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        await close
        assert not daemon.resources_busy
        assert cleaner.tick() is not None  # No repeated sweep before the daily deadline.
        daemon.store.close()

    asyncio.run(scenario())


def test_frozen_or_recovery_required_attempt_blocks_retention(tmp_path):
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control.json", root=tmp_path / "state")
    daemon._settings = settings(tmp_path)
    daemon.store = StateStore(tmp_path / "state.sqlite3")
    with daemon.store.db:
        daemon.store.db.execute(
            "INSERT INTO attempts VALUES ('unknown','{}','recovery_required',NULL,'v1')"
        )
    daemon.cache_maintenance.next_at = 0
    assert daemon.cache_maintenance.tick() is not None
    assert daemon.cache_maintenance.task is None
    daemon.store.close()


def test_cleanup_config_is_validated(tmp_path):
    import pytest

    from maa_planner.daemon.config import settings_from_bytes
    from maa_planner.daemon.tools.cache_cleanup import CleanupPolicy

    defaults = settings_from_bytes(b'[[profiles]]\nname="nemo"\n', tmp_path / "config.toml").cleanup
    assert (defaults.apk_days, defaults.download_days, defaults.cache_days, defaults.core_keep) == (
        1,
        3,
        3,
        2,
    )
    source = b'[[profiles]]\nname="nemo"\n[cleanup]\nenabled=false\ndownload_days=9\ncache_days=40\ncore_keep=3\n'
    assert settings_from_bytes(source, tmp_path / "config.toml").cleanup == CleanupPolicy(
        False, 9, 40, 3
    )
    for invalid in (
        "enabled=1",
        "download_days=true",
        "cache_days=0",
        "core_keep=0",
        "apk_days=-1",
        "other=3",
    ):
        with pytest.raises(ValueError, match="cleanup"):
            settings_from_bytes(
                ('[[profiles]]\nname="nemo"\n[cleanup]\n' + invalid).encode(),
                tmp_path / "config.toml",
            )


def test_daemon_reconciliation_automatically_cleans_expired_cache(tmp_path, monkeypatch):
    import os

    from tests.helpers.async_tools import eventually
    from tests.helpers.daemon import running

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, _, _):
            daemon._settings = settings(tmp_path)
            archive = daemon._settings.cache_directory / "downloads" / ("a" * 64)
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"expired archive")
            os.utime(archive, (0, 0))
            daemon.cache_maintenance.next_at = 0
            daemon.wakeup.set()
            await eventually(lambda: not archive.exists())
            await daemon.cache_maintenance.close()
            assert not daemon.resources_busy

    asyncio.run(scenario())

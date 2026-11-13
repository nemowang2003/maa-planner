import os
from dataclasses import replace

from maa_planner.daemon.tools.cache_cleanup import clean_cache
from tests.helpers.cache_cleanup import DAY, NOW, old_file, settings


def test_only_expired_recognized_cache_is_removed(tmp_path):
    config = settings(tmp_path)
    cache = config.cache_directory
    expired = old_file(cache / "downloads" / ("a" * 64), age=4)
    recent = old_file(cache / "downloads" / ("b" * 64), age=2)
    catalog = old_file(cache / "stage-catalog" / ("c" * 40 + ".json"))
    fresh_catalog = old_file(cache / "stage-catalog" / ("d" * 40 + ".json"), age=2)
    unknown = old_file(cache / "downloads" / "keep-my-file")
    evidence = old_file(cache / "official-news" / "responses" / ("e" * 64 + ".bin"))
    snapshot = old_file(cache / "activity-assets" / "snapshots" / ("f" * 64) / "READY")
    report = clean_cache(config, now=NOW)
    assert not expired.exists() and not catalog.exists()
    assert all(p.exists() for p in (recent, fresh_catalog, unknown, evidence, snapshot))
    assert report["removed_entries"] == 2
    assert report["freed_bytes"] == 10
    assert (
        clean_cache(replace(config, cleanup=replace(config.cleanup, enabled=False)), now=NOW)[
            "removed_entries"
        ]
        == 0
    )


def old_tree(path, *, age=40):
    old_file(path / "payload", age=age)
    os.utime(path, (NOW - age * DAY, NOW - age * DAY))
    return path


def test_hot_snapshot_with_recent_reuse_is_preserved_and_pins_protect_ancestors(tmp_path):
    config = settings(tmp_path)
    expired = old_tree(config.cache_directory / "hot-update" / ("a" * 64))
    pinned = old_tree(config.cache_directory / "hot-update" / ("b" * 64))
    reused = old_tree(config.cache_directory / "hot-update" / ("c" * 64))
    os.utime(reused, (NOW, NOW))
    clean_cache(config, protected=[pinned / "payload"], now=NOW)
    assert not expired.exists() and pinned.exists() and reused.exists()


def test_symlink_and_linked_descendants_never_escape_managed_cache(tmp_path):
    config = settings(tmp_path)
    outside = old_tree(tmp_path / "external")
    root = config.cache_directory / "hot-update"
    root.mkdir(parents=True)
    (root / ("a" * 64)).symlink_to(outside, target_is_directory=True)
    nested = old_tree(root / ("b" * 64))
    (nested / "escape").symlink_to(outside, target_is_directory=True)
    os.utime(nested, (NOW - 40 * DAY, NOW - 40 * DAY))
    clean_cache(config, now=NOW)
    assert (outside / "payload").exists() and (root / ("a" * 64)).is_symlink() and nested.exists()


def test_keeps_two_newest_core_versions_and_protected_older_installation(tmp_path):
    config = settings(tmp_path)
    cores = []
    for version in ("6.9.0", "6.10.0", "6.21.0", "6.22.0"):
        path = config.runtime_directory / f"core-{version}-windows-amd64"
        (path / "data/lib").mkdir(parents=True)
        (path / "data/resource").mkdir()
        cores.append(path)
    unknown = old_tree(config.runtime_directory / "core-custom")
    clean_cache(config, protected=[cores[1] / "data/lib"], now=NOW)
    assert not cores[0].exists()
    assert all(p.exists() for p in (*cores[1:], unknown))


def test_held_resource_lock_prevents_cleanup(tmp_path):
    from maa_planner.daemon.tools.locks import acquire, data_resource

    config = settings(tmp_path)
    target = old_file(config.cache_directory / "downloads" / ("a" * 64))
    with acquire([data_resource(target)], root=None):
        result = clean_cache(config, now=NOW)
    assert target.exists() and result["busy_entries"] == 1


def test_recovery_fence_survives_sql_loss_and_unknown_metadata_is_protected(tmp_path):
    import json

    from maa_planner.daemon.tools.cache_cleanup import recovery_pending

    root = tmp_path / "recovery"
    fence = root / "core" / "unknown" / "core-execution.json"
    fence.parent.mkdir(parents=True)
    fence.write_text(json.dumps({"phase": "conflict"}))
    assert recovery_pending(root)
    fence.write_text("{broken")
    assert recovery_pending(root)
    fence.write_text(json.dumps({"phase": "finished"}))
    assert not recovery_pending(root)


def test_removal_error_keeps_processing_other_candidates(tmp_path, monkeypatch):
    from pathlib import Path

    config = settings(tmp_path)
    denied = old_file(config.cache_directory / "downloads" / ("a" * 64))
    removable = old_file(config.cache_directory / "downloads" / ("b" * 64))
    unlink = Path.unlink

    def delete(path, *args, **kwargs):
        if path == denied:
            raise PermissionError("in use")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", delete)
    report = clean_cache(config, now=NOW)
    assert denied.exists() and not removable.exists()
    assert report["removed_entries"] == 1 and len(report["errors"]) == 1


def test_apk_idle_one_day_and_active_artifact_lock(tmp_path):
    from maa_planner.daemon.tools.game_update import cache_resource
    from maa_planner.daemon.tools.locks import acquire

    config = settings(tmp_path)
    apk = old_file(config.game_cache / "latest.apk", age=2)
    with acquire([cache_resource(config.game_cache)]):
        assert clean_cache(config, now=NOW)["busy_entries"] == 1
    assert apk.exists()
    clean_cache(config, now=NOW)
    assert not apk.exists()


def test_recent_apk_reuse_preserves_apk_without_changing_source_identity(tmp_path):
    from maa_planner.daemon.tools.cache_cleanup import mark_apk_used

    config = settings(tmp_path)
    apk = old_file(config.game_cache / "latest.apk", age=2)
    original = apk.stat().st_mtime_ns
    mark_apk_used(apk)
    stamp = apk.with_suffix(".used")
    assert apk.stat().st_mtime_ns == original
    os.utime(stamp, (NOW, NOW))
    clean_cache(config, now=NOW)
    assert apk.exists()
    clean_cache(config, now=NOW + 2 * DAY)
    assert not apk.exists() and not stamp.exists()


def test_expired_asset_jobs_and_abandoned_hot_fetch_are_disposable(tmp_path):
    config = settings(tmp_path)
    job = old_tree(config.cache_directory / "activity-assets/jobs" / ("a" * 32))
    fetch = old_tree(config.cache_directory / "hot-update" / ".fetch-abc12345")
    snapshot = old_tree(config.cache_directory / "activity-assets/snapshots" / ("b" * 64))
    report = clean_cache(config, now=NOW)
    assert not job.exists() and not fetch.exists() and snapshot.exists()
    assert report["removed_entries"] == 2


def test_partial_directory_deletion_cannot_leave_a_reusable_broken_cache(tmp_path, monkeypatch):
    import shutil

    config = settings(tmp_path)
    snapshot = old_tree(config.cache_directory / "hot-update" / ("a" * 64))
    old_file(snapshot / "remaining")
    delete = shutil.rmtree

    def partial(path, *args, **kwargs):
        (path / "payload").unlink()
        raise PermissionError("temporary Windows file lock")

    monkeypatch.setattr(shutil, "rmtree", partial)
    report = clean_cache(config, now=NOW)
    assert not snapshot.exists()
    assert report["errors"]
    assert any(snapshot.parent.glob(".cleanup-*"))
    monkeypatch.setattr(shutil, "rmtree", delete)
    clean_cache(config, now=NOW)
    assert not list(snapshot.parent.glob(".cleanup-*"))

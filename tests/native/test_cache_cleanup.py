import os

from maa_planner.daemon.tools.cache_cleanup import clean_cache, mark_apk_used
from tests.helpers.cache_cleanup import settings


def test_native_apk_usage_stamp_preserves_identity_and_expires_with_artifact(tmp_path):
    config = settings(tmp_path)
    apk = config.game_cache / "latest.apk"
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b"isolated test artifact")
    os.utime(apk, (0, 0))
    identity = apk.stat().st_mtime_ns
    mark_apk_used(apk)
    stamp = apk.with_suffix(".used")
    assert apk.stat().st_mtime_ns == identity and stamp.is_file()
    assert clean_cache(config)["removed_entries"] == 0
    os.utime(stamp, (0, 0))
    assert clean_cache(config)["removed_entries"] == 1
    assert not apk.exists() and not stamp.exists()


def test_native_directory_retirement_keeps_open_handle_failures_out_of_reusable_cache(tmp_path):
    config = settings(tmp_path)
    snapshot = config.cache_directory / "hot-update" / ("a" * 64)
    snapshot.mkdir(parents=True)
    payload = snapshot / "data"
    payload.write_bytes(b"isolated snapshot")
    os.utime(payload, (0, 0))
    os.utime(snapshot, (0, 0))
    with payload.open("rb"):
        report = clean_cache(config)
        if snapshot.exists():
            assert payload.read_bytes() == b"isolated snapshot"
        else:
            assert report["removed_entries"] == 1 or list(snapshot.parent.glob(".cleanup-*"))
    clean_cache(config)
    assert not snapshot.exists() and not list(snapshot.parent.glob(".cleanup-*"))

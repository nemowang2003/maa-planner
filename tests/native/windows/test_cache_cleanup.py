import os

from maa_planner.daemon.tools.cache_cleanup import clean_cache
from tests.helpers.cache_cleanup import DAY, NOW, old_file, settings
from tests.helpers.filesystem import windows_junction


def test_native_junction_does_not_expose_external_files_to_cache_cleanup(tmp_path):
    config = settings(tmp_path)
    outside = tmp_path / "outside"
    target = old_file(outside / "payload")
    root = config.cache_directory / "hot-update" / ("a" * 64)
    root.mkdir(parents=True)
    junction = root / "external"

    with windows_junction(junction, outside):
        os.utime(root, (NOW - 40 * DAY, NOW - 40 * DAY))
        report = clean_cache(config, now=NOW)
        assert junction.is_junction() and target.exists()
        assert report["removed_entries"] == 0

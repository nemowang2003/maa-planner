import os
import subprocess

from maa_planner.daemon.tools.cache_cleanup import clean_cache
from tests.helpers.cache_cleanup import DAY, NOW, old_file, settings


def test_native_junction_does_not_expose_external_files_to_cache_cleanup(tmp_path):
    config = settings(tmp_path)
    outside = tmp_path / "outside"
    target = old_file(outside / "payload")
    root = config.cache_directory / "hot-update" / ("a" * 64)
    root.mkdir(parents=True)
    junction = root / "external"

    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"

    subprocess.run(
        [
            "powershell.exe",
            "-Command",
            f"New-Item -ItemType Junction -Path {quote(junction)} -Target {quote(outside)} | Out-Null",
        ],
        check=True,
        capture_output=True,
    )
    try:
        os.utime(root, (NOW - 40 * DAY, NOW - 40 * DAY))
        report = clean_cache(config, now=NOW)
        assert junction.is_junction() and target.exists()
        assert report["removed_entries"] == 0
    finally:
        # Remove the junction itself; never recurse into its target.
        junction.rmdir()

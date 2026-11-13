"""Real isolated child lifecycle for the asset exporter; no .NET or game needed."""

import sys
import threading

import psutil
import pytest

from maa_planner.daemon.tools.activity_assets import run_export


def test_export_timeout_terminates_its_process_and_preserves_private_log(tmp_path, monkeypatch):
    child = tmp_path / "exporter.dll"
    child.write_text(
        'import os, pathlib, time\npathlib.Path("pid").write_text(str(os.getpid()))\nprint("private exporter started", flush=True)\ntime.sleep(60)\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "maa_planner.daemon.tools.activity_assets.shutil.which", lambda name: sys.executable
    )
    log = tmp_path / "export.log"
    with pytest.raises(TimeoutError):
        run_export(
            child,
            tmp_path / "input",
            tmp_path / "output",
            log,
            1,
            threading.Event(),
            max_output_bytes=1024,
        )
    pid = int((tmp_path / "pid").read_text())
    assert not psutil.pid_exists(pid)
    assert "private exporter started" in log.read_text(encoding="utf-8")


def test_export_output_budget_stops_child_before_publication(tmp_path, monkeypatch):
    child = tmp_path / "exporter.dll"
    child.write_text(
        'import pathlib, time\nroot = pathlib.Path("output")\nroot.mkdir()\n(root / "large.png").write_bytes(b"x" * 2048)\ntime.sleep(60)\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "maa_planner.daemon.tools.activity_assets.shutil.which", lambda name: sys.executable
    )
    with pytest.raises(ValueError, match="输出预算"):
        run_export(
            child,
            tmp_path / "input",
            tmp_path / "output",
            tmp_path / "export.log",
            10,
            threading.Event(),
            max_output_bytes=1024,
        )
    assert not (tmp_path / "READY").exists()


def test_asset_snapshot_remains_readable_after_hash_directory_publication(tmp_path, monkeypatch):
    import json
    import os
    from pathlib import Path

    from maa_planner.daemon.tools.activity_assets import prepare_assets
    from tests.helpers.activity_assets import ARGUMENTS, scenario
    from tests.helpers.png import PNG

    # Each component is legal. Publishing under 64-character content hashes
    # pushes child PNG paths beyond Win32 MAX_PATH on an otherwise valid root.
    directory = tmp_path / ("activity-cache-" + "x" * 70)
    platform = "windows" if os.name == "nt" else "linux"
    settings, get, export, _, exports, _ = scenario(directory, monkeypatch, platform)
    first = prepare_assets(
        settings, ARGUMENTS, threading.Event(), get=get, export=export, platform=platform
    )
    folder = Path(first["snapshot"])
    manifest = json.loads((folder / "manifest.json").read_text())
    assert (folder / manifest["assets"][0]["path"]).read_bytes() == PNG
    second = prepare_assets(
        settings, ARGUMENTS, threading.Event(), get=get, export=export, platform=platform
    )
    assert second["identity"] == first["identity"] and second["reused"]
    assert len(exports) == 1

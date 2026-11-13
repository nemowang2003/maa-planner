from pathlib import Path

import pytest

from maa_planner.daemon.adapters import processes as process_adapter
from maa_planner.daemon.adapters import windows
from maa_planner.daemon.config import Profile


def test_paths_are_not_translated_between_operating_systems():
    assert windows.windows_path(Path("/mnt/c/example")) == str(Path("/mnt/c/example"))
    assert windows.local_path("relative") == Path("relative")


def test_external_gui_is_checked_by_native_executable_path(tmp_path, monkeypatch):
    profile = Profile("nemo", tmp_path / "nemo")
    assert profile.gui_path is not None

    class Process:
        info = {"name": "MAA.exe"}
        pid = 42
        path = str(profile.gui_path / "MAA.exe")

        def exe(self):
            return self.path

    process = Process()
    monkeypatch.setattr(process_adapter.psutil, "process_iter", lambda attrs: [process])
    with pytest.raises(RuntimeError, match="PID 42"):
        process_adapter.ensure_stopped((profile,))
    process.path = str(tmp_path / "other/MAA.exe")
    process_adapter.ensure_stopped((profile,))

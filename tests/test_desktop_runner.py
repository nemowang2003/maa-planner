from __future__ import annotations

import json
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from maa_desktop_planner import config, runner, updates


def _settings(tmp_path: Path) -> tuple[config.Settings, dict[str, bytes]]:
    profiles = []
    originals = {}
    shared = tmp_path / "shared-resource"
    shared.mkdir()
    for name in ("first", "second"):
        folder = tmp_path / name
        (folder / "config").mkdir(parents=True)
        (folder / "MAA.exe").touch()
        (folder / "resource").symlink_to(shared, target_is_directory=True)
        gui = folder / "config" / "gui.new.json"
        original = (
            '{"Current":"Default","Configurations":{"Default":{"Gui":'
            '{"StartUpSettings":{"RunDirectly":false},"PostActions":"None"}}},'
            '"Update":{"UpdatePackage":"","CheckOnStartup":true,'
            '"CheckOnSchedule":true,"AutoDownloadUpdatePackage":true,'
            '"AutoInstallUpdatePackage":true}}\r\n'
        ).encode()
        gui.write_bytes(original)
        originals[name] = original
        profiles.append(config.Profile(name, folder))
    return config.Settings(tmp_path / "config.toml", tuple(profiles)), originals


def test_example_config_is_valid_toml() -> None:
    raw = tomllib.loads(config.example_config())
    assert raw["profiles"][0]["path"].endswith("profiles\\nemo")


def test_parallel_launch_uses_temporary_config_and_restores(tmp_path, monkeypatch) -> None:
    settings, originals = _settings(tmp_path)
    checked = []
    monkeypatch.setattr(runner, "ensure_stopped", lambda profiles: checked.append(tuple(profiles)))
    monkeypatch.setattr(
        runner,
        "check_software",
        lambda path, channel: updates.SoftwareUpdate("v6.18.0", "v6.18.0", channel, False),
    )
    monkeypatch.setattr(
        runner,
        "check_resource",
        lambda path: updates.ResourceUpdate(datetime(2026, 9, 1), datetime(2026, 9, 1), False),
    )
    launched = []

    class Process:
        pid = 123

        def poll(self):
            return 0

        def wait(self):
            return 0

    def launch(command, cwd):
        for profile in settings.profiles:
            temporary = json.loads(profile.gui_config.read_text())
            assert temporary["Update"]["CheckOnStartup"] is False
            assert temporary["Update"]["AutoInstallUpdatePackage"] is False
            gui = temporary["Configurations"]["Default"]["Gui"]
            assert gui["StartUpSettings"]["RunDirectly"] is True
            assert gui["PostActions"] == "ExitSelf"
        launched.append((command, cwd))
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    runner._run_locked(settings)
    assert len(launched) == 2
    assert len(checked) == 2
    for profile in settings.profiles:
        assert profile.gui_config.read_bytes() == originals[profile.name]
        assert not runner._backup_path(profile.gui_config).exists()


def test_real_subprocesses_overlap_and_configs_are_restored(tmp_path, monkeypatch) -> None:
    settings, originals = _settings(tmp_path)
    monkeypatch.setattr(runner, "ensure_stopped", lambda profiles: None)
    monkeypatch.setattr(
        runner,
        "check_software",
        lambda path, channel: updates.SoftwareUpdate("v6.18.0", "v6.18.0", channel, False),
    )
    monkeypatch.setattr(
        runner,
        "check_resource",
        lambda path: updates.ResourceUpdate(datetime(2026, 9, 1), datetime(2026, 9, 1), False),
    )
    script = """#!/usr/bin/env python3
import json
import pathlib
import time
folder = pathlib.Path(__file__).parent
gui = json.loads((folder / "config" / "gui.new.json").read_text())
assert gui["Update"]["AutoInstallUpdatePackage"] is False
(folder / "started").touch()
other = folder.parent / ("second" if folder.name == "first" else "first") / "started"
deadline = time.monotonic() + 5
while not other.exists():
    if time.monotonic() > deadline:
        raise RuntimeError("other MAA did not start concurrently")
    time.sleep(0.01)
"""
    for profile in settings.profiles:
        profile.exe.write_text(script)
        profile.exe.chmod(0o755)

    runner._run_locked(settings)
    for profile in settings.profiles:
        assert (profile.path / "started").exists()
        assert profile.gui_config.read_bytes() == originals[profile.name]


def test_failed_launch_restores_all_configs(tmp_path, monkeypatch) -> None:
    settings, originals = _settings(tmp_path)
    monkeypatch.setattr(runner, "ensure_stopped", lambda profiles: None)
    monkeypatch.setattr(
        runner,
        "check_software",
        lambda path, channel: updates.SoftwareUpdate("v6.18.0", "v6.18.0", channel, False),
    )
    monkeypatch.setattr(
        runner,
        "check_resource",
        lambda path: updates.ResourceUpdate(datetime(2026, 9, 1), datetime(2026, 9, 1), False),
    )
    state = {"count": 0, "terminated": False}

    class Process:
        pid = 123

        def poll(self):
            return None

        def terminate(self):
            state["terminated"] = True

        def wait(self):
            return 0

    def launch(command, cwd):
        state["count"] += 1
        if state["count"] == 2:
            raise OSError("launch failed")
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    with pytest.raises(OSError, match="launch failed"):
        runner._run_locked(settings)
    assert state["terminated"]
    for profile in settings.profiles:
        assert profile.gui_config.read_bytes() == originals[profile.name]
        assert not runner._backup_path(profile.gui_config).exists()


def test_running_maa_is_rejected(tmp_path, monkeypatch) -> None:
    settings, _ = _settings(tmp_path)

    class Process:
        pid = 42
        info = {"name": "MAA.exe"}

        def exe(self):
            return str(settings.profiles[0].exe)

    monkeypatch.setattr(runner.psutil, "process_iter", lambda attrs: iter([Process()]))
    with pytest.raises(RuntimeError, match="MAA 已在运行"):
        runner.ensure_stopped(settings.profiles)


def test_existing_backup_blocks_run_and_can_be_restored(tmp_path, monkeypatch) -> None:
    settings, originals = _settings(tmp_path)
    profile = settings.profiles[0]
    backup = runner._backup_path(profile.gui_config)
    backup.write_bytes(originals[profile.name])
    profile.gui_config.write_text('{"temporary":true}')
    monkeypatch.setattr(runner, "ensure_stopped", lambda profiles: None)

    with pytest.raises(RuntimeError, match="未恢复的配置备份"):
        runner._run_locked(settings)
    runner._restore_one(profile.gui_config)
    assert profile.gui_config.read_bytes() == originals[profile.name]
    assert not backup.exists()


def test_planner_hook_is_merged_but_cannot_enable_internal_updater(tmp_path, monkeypatch) -> None:
    settings, _ = _settings(tmp_path)
    profile = settings.profiles[0]
    monkeypatch.setattr(
        runner,
        "resolve_hook",
        lambda spec: (
            lambda profile, gui: {
                "Update": {"AutoInstallUpdatePackage": True},
                "Configurations": {"Default": {"Gui": {"WindowTitlePrefix": "planned"}}},
            }
        ),
    )
    temporary = json.loads(runner._prepared_config(profile, settings))
    assert temporary["Update"]["AutoInstallUpdatePackage"] is False
    assert temporary["Configurations"]["Default"]["Gui"]["WindowTitlePrefix"] == "planned"

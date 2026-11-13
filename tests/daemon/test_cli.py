import json
import subprocess
import sys
from pathlib import Path

import pytest

from maa_planner import main
from maa_planner.daemon.adapters import processes
from maa_planner.daemon.config import Profile, example_config, load_settings
from maa_planner.daemon.execution.context import RunRequest


def test_config_needs_no_gui_installation(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[[profiles]]\nname="nemo"\nemulator_name="Nemo MuMu"\n')
    settings = load_settings(path)
    assert settings.profiles == (Profile("nemo", emulator_name="Nemo MuMu"),)
    monkeypatch.setattr(
        processes.psutil, "process_iter", lambda *args: pytest.fail("no GUI query expected")
    )
    processes.ensure_stopped(settings.profiles)


def test_admin_alert_channel_cannot_be_used_as_an_account_name(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[profiles]]\nname="admin"\n')
    with pytest.raises(ValueError, match="保留名称"):
        load_settings(path)


@pytest.mark.parametrize(
    "content",
    [
        '[unknown]\noption=true\n[[profiles]]\nname="nemo"\n',
        '[[profiles]]\nname="nemo"\nunknown=true\n',
    ],
    ids=["root-field", "profile-field"],
)
def test_configuration_rejects_unknown_fields(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(ValueError, match="未知"):
        load_settings(path)


def test_config_helper_uses_single_default_file(tmp_path, monkeypatch, capsys):
    from maa_planner.daemon import cli

    path = tmp_path / "settings/config.toml"
    monkeypatch.setattr(cli, "default_config_file", lambda: path)
    path.parent.mkdir(parents=True)
    path.write_text(example_config())
    main(["daemon", "config", "dump"])
    result = json.loads(capsys.readouterr().out)
    assert result["config_file"] == str(path)
    assert result["profiles"][0]["name"] == "nemo"
    assert "fallback_stage" not in result
    main(["daemon", "config", "validate"])
    assert "配置有效" in capsys.readouterr().out


def test_root_entry_exposes_daemon_and_independent_wecom_client():
    result = subprocess.run(
        [sys.executable, "-c", "from maa_planner import main; main()", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "{daemon,wecom,serverchan}" in result.stdout
    assert "copilot" not in result.stdout and "activities" not in result.stdout


def test_cli_request_serialization_rejects_gui_payload(tmp_path):
    from tests.helpers.execution import request

    payload = request(tmp_path).to_dict()
    payload.update(backend="gui", core=None)
    with pytest.raises(ValueError, match="GUI"):
        RunRequest.from_dict(payload)


def test_managed_paths_are_derived_from_one_root(tmp_path, monkeypatch):
    from maa_planner.daemon.config import settings_from_bytes

    settings = settings_from_bytes(
        b'[[profiles]]\nname="nemo"\n[maa_core]\ndirectory="runtime"\n', tmp_path / "config.toml"
    )
    runtime = settings.runtime_template()
    assert settings.runtime_directory == tmp_path / "runtime"
    assert runtime.executable == Path(sys.executable)
    assert runtime.work_dir == settings.runtime_directory / "tmp"
    assert runtime.resources == settings.runtime_directory / "unprepared/data/resource"
    assert runtime.expected_core_version is None
    assert runtime.game_package == "com.hypergryph.arknights"


@pytest.mark.parametrize(
    "section,key",
    [
        ("maa_core", "expected_cli_version"),
        ("maa_core", "expected_core_version"),
        ("runtime_install", "cli_version"),
        ("runtime_install", "core_version"),
    ],
)
def test_version_pins_are_rejected_in_user_configuration(tmp_path, section, key):
    from maa_planner.daemon.config import settings_from_bytes

    document = (
        example_config() + f'\n[{section}]\n{key} = "1.2.3"\n'
        if section == "runtime_install"
        else example_config().replace(f"[{section}]", f'[{section}]\n{key} = "1.2.3"')
    )
    with pytest.raises(ValueError):
        settings_from_bytes(document.encode(), tmp_path / "config.toml")

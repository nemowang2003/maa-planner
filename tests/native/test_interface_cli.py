"""The installed independent executable supports offline setup on each native host."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.serverchan import config_file as serverchan_config
from tests.helpers.wecom import config_file as wecom_config


@pytest.mark.parametrize(
    "name,config_factory,keys",
    [
        ("wecom", wecom_config, {"bot_id", "secret", "admins", "routes"}),
        ("serverchan", serverchan_config, {"nemo", "wyf"}),
    ],
)
def test_installed_interface_entrypoints_validate_without_a_daemon_or_external_service(
    tmp_path, name, config_factory, keys
):
    executable = Path(sys.executable).parent / (
        f"maa-planner-{name}-client.exe" if os.name == "nt" else f"maa-planner-{name}-client"
    )
    assert executable.is_file()
    path = config_factory(tmp_path)
    result = subprocess.run(
        [
            str(executable),
            "--config",
            str(path),
            "--endpoint",
            str(tmp_path / "absent.json"),
            "--check",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "test-secret" not in result.stdout + result.stderr
    assert "PRIVATE_KEY" not in result.stdout + result.stderr
    assert result.stdout.strip() and not result.stderr
    example = subprocess.run(
        [str(executable), "--example"], capture_output=True, text=True, check=True, timeout=10
    )
    document = json.loads(example.stdout)
    assert set(document) == keys

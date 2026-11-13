"""Native task-schema and hosted-process checks; never register or launch a task."""

import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

from maa_planner.daemon.adapters.console_control import ConsoleControl
from maa_planner.daemon.adapters.windows import powershell, ps_quote
from maa_planner.daemon.cli import wait_lifecycle
from maa_planner.daemon.service import current_sid, task_xml
from tests.helpers.control import ApiPeer


def test_generated_task_xml_is_accepted_without_registration(tmp_path):
    config = tmp_path / "中文 & ' spaces.toml"
    config.write_text('[[profiles]]\nname="isolated"\n')
    args = SimpleNamespace(
        config=config, endpoint=tmp_path / "control.json", state_dir=tmp_path / "state"
    )
    xml = task_xml(args, current_sid())
    result = powershell(f"""
$ErrorActionPreference = 'Stop'
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$definition = $scheduler.NewTask(0)
$definition.XmlText = {ps_quote(xml)}
@{{ command=$definition.Actions.Item(1).Path; restart_count=$definition.Settings.RestartCount; logon_type=$definition.Principal.LogonType }} | ConvertTo-Json -Compress
""")
    value = json.loads(result)
    assert value["command"] == sys.executable
    assert value["restart_count"] == 3
    assert value["logon_type"] == 3  # TASK_LOGON_INTERACTIVE_TOKEN


def test_native_console_handler_registration_and_release():
    with ConsoleControl(lambda reason: None) as control:
        assert control.callback is not None
    assert control.finished.is_set()


def test_hosted_restart_keeps_daemon_pid_and_stops_cleanly(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[[profiles]]\nname="isolated"\n')
    endpoint = tmp_path / "control.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "maa_planner.daemon",
            "--config",
            str(config),
            "service",
            "run",
            "--endpoint",
            str(endpoint),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        env={
            **os.environ,
            "XDG_STATE_HOME": str(tmp_path / "user"),
            "LOCALAPPDATA": str(tmp_path / "user"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    async def scenario():
        async with asyncio.timeout(15):
            while True:
                try:
                    initial = await ApiPeer(endpoint).call("daemon.status")
                    break
                except OSError:
                    assert process.poll() is None
                    await asyncio.sleep(0.05)
        assert initial["hosting"] == "windows-task"
        restarted = await wait_lifecycle(endpoint, "restart", timeout=15)
        assert restarted["pid"] == initial["pid"]
        assert restarted["started_at"] != initial["started_at"]
        await wait_lifecycle(endpoint, "stop", timeout=15)

    try:
        asyncio.run(scenario())
        out, err = process.communicate(timeout=10)
        assert process.returncode == 0, (out, err)
        assert not endpoint.exists()
        assert (tmp_path / "state/daemon.log").is_file()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)


def test_management_status_script_parses_and_reports_no_task(tmp_path, monkeypatch):
    from maa_planner.daemon import service

    # Exercise the actual PowerShell adapter and COM connection while hiding
    # the user's registered tasks; this test cannot mutate Task Scheduler.
    real_powershell = powershell

    def isolated(script):
        return real_powershell(
            "function Get-ScheduledTask { param($TaskPath, $ErrorAction) }\n" + script
        )

    monkeypatch.setattr(service, "powershell", isolated)
    args = SimpleNamespace(
        config=tmp_path / "config.toml",
        endpoint=tmp_path / "control.json",
        state_dir=tmp_path / "state",
        action="status",
    )
    result = service.manage(args)
    assert not result["installed"]
    assert result["active_instances"] == 0

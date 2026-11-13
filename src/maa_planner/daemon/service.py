"""Interactive-user Windows hosting; Task Scheduler owns the foreground process."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from maa_planner.daemon.adapters.windows import powershell, ps_quote, require_windows_host

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DESCRIPTION = "maa-planner interactive daemon: "


def task_xml(args, sid: str) -> str:
    """No shell or uv launcher between Task Scheduler and the hosted interpreter."""
    root = ET.Element("Task", {"version": "1.2", "xmlns": NAMESPACE})

    def add(parent, name, value=None):
        element = ET.SubElement(parent, name)
        element.text = value
        return element

    registration = add(root, "RegistrationInfo")
    add(registration, "Description", DESCRIPTION + str(args.endpoint.resolve()))
    triggers = add(root, "Triggers")
    trigger = add(triggers, "LogonTrigger")
    add(trigger, "Enabled", "true")
    add(trigger, "UserId", sid)
    principals = add(root, "Principals")
    principal = ET.SubElement(principals, "Principal", {"id": "User"})
    add(principal, "UserId", sid)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(root, "Settings")
    for key, value in {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "AllowHardTerminate": "false",
        "StartWhenAvailable": "true",
        "ExecutionTimeLimit": "PT0S",
    }.items():
        add(settings, key, value)
    restart = add(settings, "RestartOnFailure")
    add(restart, "Interval", "PT1M")
    add(restart, "Count", "3")
    actions = ET.SubElement(root, "Actions", {"Context": "User"})
    action = add(actions, "Exec")
    # Use the venv's real interpreter path, preserving site-packages.
    add(action, "Command", str(Path(sys.executable).absolute()))
    add(
        action,
        "Arguments",
        subprocess.list2cmdline(
            [
                "-m",
                "maa_planner.daemon",
                "--config",
                str(args.config.resolve()),
                "service",
                "run",
                "--endpoint",
                str(args.endpoint.resolve()),
                "--state-dir",
                str(args.state_dir.resolve()),
            ]
        ),
    )
    add(action, "WorkingDirectory", str(args.config.resolve().parent))
    return ET.tostring(root, encoding="unicode")


def current_sid():
    return powershell("[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value")


def manage(args):
    require_windows_host()
    sid = current_sid()
    name = "maa-planner-" + sid
    xml = task_xml(args, sid)
    if args.action == "plan":
        return {"task_name": name, "xml": xml}
    marker = DESCRIPTION + str(args.endpoint.resolve())
    script = f"""
$ErrorActionPreference = 'Stop'
$name = {ps_quote(name)}
$task = Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop | Where-Object {{ $_.TaskName -eq $name }}
if ($task -and $task.Description -ne {ps_quote(marker)}) {{ throw '同名任务不属于此控制端点' }}
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$active = if ($task) {{ $scheduler.GetFolder('\\').GetTask($name).GetInstances(0).Count }} else {{ 0 }}
"""
    if args.action == "install":
        from maa_planner.daemon.config import load_settings

        load_settings(args.config)
        script += f"""
if ($active -gt 0) {{ throw '请先安全停止托管任务再重新安装' }}
$task = Register-ScheduledTask -TaskName $name -TaskPath '\\' -Xml {ps_quote(xml)} -Force
"""
    elif args.action == "remove":
        script += """
if ($active -gt 0) { throw '请先通过 service stop 安全停止' }
if ($task) { Unregister-ScheduledTask -TaskName $name -TaskPath '\\' -Confirm:$false }
$task = $null
"""
    elif args.action == "start":
        script += """
if (-not $task) { throw '尚未安装托管任务' }
Enable-ScheduledTask -TaskName $name -TaskPath '\\' | Out-Null
Start-ScheduledTask -TaskName $name -TaskPath '\\'
$task = Get-ScheduledTask -TaskName $name -TaskPath '\\'
"""
    elif args.action == "disable":
        script += """
if ($task) { $task = Disable-ScheduledTask -TaskName $name -TaskPath '\\' }
"""
    elif args.action != "status":
        raise ValueError("未知托管操作")
    script += """
$active = if ($task) { $scheduler.GetFolder('\\').GetTask($name).GetInstances(0).Count } else { 0 }
$info = if ($task) { Get-ScheduledTaskInfo -TaskName $name -TaskPath '\\' } else { $null }
@{ task_name=$name; installed=[bool]$task; active_instances=$active; state=if ($task) { [string]$task.State } else { $null }; last_result=if ($info) { $info.LastTaskResult } else { $null } } | ConvertTo-Json -Compress
"""
    try:
        return json.loads(powershell(script))
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "Windows 任务管理失败：" + (error.stderr or str(error)).strip()
        ) from error


async def host(args):
    from maa_planner.daemon.cli import serve

    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.FileHandler(args.state_dir / "daemon.log", encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    try:
        while await serve(args, managed=True):
            # A requested restart stays in this tracked process. Unexpected
            # failures escape with a nonzero exit for bounded scheduler recovery.
            await asyncio.sleep(0)
    except BaseException:
        logging.exception("Windows 托管 daemon 退出")
        raise
    finally:
        handler.close()


async def lifecycle(args):
    from copy import copy

    from maa_planner.daemon.cli import call, wait_lifecycle

    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        raise ValueError("timeout 必须为正秒数")
    if args.action == "start":
        try:
            await call(args.endpoint, "daemon.status")
        except (OSError, RuntimeError, ValueError, TimeoutError):
            pass
        else:
            raise RuntimeError("daemon 已在运行")
        registration = await asyncio.to_thread(manage, args)
        async with asyncio.timeout(args.timeout if args.timeout is not None else 15):
            while True:
                try:
                    status = await call(args.endpoint, "daemon.status")
                except (OSError, RuntimeError, ValueError, TimeoutError):
                    status = None
                if status is not None:
                    if status.get("hosting") != "windows-task":
                        raise RuntimeError("控制端点被非托管 daemon 占用")
                    if status.get("configuration_revision") is not None:
                        return {**registration, "daemon": status}
                await asyncio.sleep(0.1)
    if args.action == "stop":
        disable = copy(args)
        disable.action = "disable"
        registration = await asyncio.to_thread(manage, disable)
    else:
        registration = None
    try:
        status = await call(args.endpoint, "daemon.status")
    except (OSError, RuntimeError, ValueError, TimeoutError):
        if registration and registration["active_instances"] == 0:
            return {**registration, "stopped": True}
        raise RuntimeError("daemon 控制端点不可用，未强制终止托管进程") from None
    if status.get("hosting") != "windows-task":
        raise RuntimeError("控制端点属于非托管 daemon，请使用 daemon stop／restart")
    return await wait_lifecycle(args.endpoint, args.action, force=args.force, timeout=args.timeout)

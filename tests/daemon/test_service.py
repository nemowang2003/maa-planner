import asyncio
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from maa_planner.daemon import cli, service
from maa_planner.daemon.adapters.console_control import ConsoleControl


def arguments(tmp_path, action="plan"):
    return SimpleNamespace(
        config=tmp_path / "含 空格 ' $x & config.toml",
        endpoint=tmp_path / "endpoint/control.json",
        state_dir=tmp_path / "state",
        action=action,
        force=False,
        timeout=1,
    )


def test_task_tracks_foreground_host_with_bounded_recovery(tmp_path):
    args = arguments(tmp_path)
    xml = service.task_xml(args, "S-1-5-21-1000")
    root = ET.fromstring(xml)
    ns = {"t": service.NAMESPACE}

    def value(path):
        element = root.find(path, ns)
        assert element is not None
        return element.text

    assert value("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert value("t:Settings/t:RestartOnFailure/t:Count") == "3"
    assert value("t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert value("t:Settings/t:AllowHardTerminate") == "false"
    assert "service run" in value("t:Actions/t:Exec/t:Arguments")
    assert str(args.config) in value("t:Actions/t:Exec/t:Arguments")


def test_task_plan_never_registers_or_launches_and_foreign_task_is_guarded(tmp_path, monkeypatch):
    scripts = []
    monkeypatch.setattr(service, "require_windows_host", lambda: None)
    monkeypatch.setattr(service, "current_sid", lambda: "S-1-5-21-1000")
    monkeypatch.setattr(service, "powershell", lambda script: scripts.append(script) or "{}")
    args = arguments(tmp_path)
    assert "xml" in service.manage(args)
    assert not scripts
    args.action = "remove"
    service.manage(args)
    script = scripts[-1]
    assert script.index("$task.Description -ne") < script.index("Unregister-ScheduledTask")
    assert "$active -gt 0" in script
    assert "Stop-ScheduledTask" not in script


def test_host_restarts_only_when_requested_and_propagates_crash(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    calls = []

    async def serve(args, *, managed):
        calls.append(managed)
        return len(calls) == 1

    monkeypatch.setattr(cli, "serve", serve)
    # Avoid changing the test runner's logging handlers.
    monkeypatch.setattr(service.logging, "basicConfig", lambda **kw: None)
    asyncio.run(service.host(args))
    assert calls == [True, True]

    async def crash(args, *, managed):
        raise OSError("injected crash")

    monkeypatch.setattr(cli, "serve", crash)
    with pytest.raises(OSError, match="injected crash"):
        asyncio.run(service.host(args))


def test_console_events_request_cleanup_but_do_not_swallow_interrupts():
    reasons = []
    control = ConsoleControl(reasons.append)
    control.finished.set()
    assert control.handle(2)
    assert control.handle(5)
    assert control.handle(6)
    assert not control.handle(0)
    assert not control.handle(1)
    assert reasons == ["WINDOWS_CONSOLE_CLOSE", "WINDOWS_LOGOFF", "WINDOWS_SHUTDOWN"]


def test_stop_disables_relaunch_before_rpc_and_does_not_stop_manual_daemon(tmp_path, monkeypatch):
    order = []

    def manage(args):
        order.append(args.action)
        return {"state": "Running", "active_instances": 1}

    async def call(*args, **kwargs):
        order.append("rpc")
        return {"hosting": "manual"}

    monkeypatch.setattr(service, "manage", manage)
    monkeypatch.setattr(cli, "call", call)
    with pytest.raises(RuntimeError, match="非托管"):
        asyncio.run(service.lifecycle(arguments(tmp_path, "stop")))
    assert order == ["disable", "rpc"]


def test_stop_with_unavailable_endpoint_never_claims_live_host_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service, "manage", lambda args: {"state": "Disabled", "active_instances": 1}
    )

    async def unavailable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(cli, "call", unavailable)
    with pytest.raises(RuntimeError, match="未强制终止"):
        asyncio.run(service.lifecycle(arguments(tmp_path, "stop")))

import asyncio
from pathlib import Path

import pytest

from maa_planner.daemon import cli


@pytest.mark.parametrize("restart", [False, True])
def test_lifecycle_waits_for_exit_or_new_ready_generation(monkeypatch, restart):
    async def scenario():
        finish = asyncio.Event()
        requested = asyncio.Event()
        calls = []
        old = {
            "pid": 42,
            "started_at": 1,
            "configuration_revision": "a",
            "draining": True,
            "stopping": False,
            "restart_requested": restart,
        }

        async def call(path, method, **params):
            calls.append((method, params))
            requested.set()
            if method != "daemon.status" or not finish.is_set():
                return old
            if restart:
                return {**old, "started_at": 2, "draining": False, "restart_requested": False}
            raise ConnectionError("closed")

        monkeypatch.setattr(cli, "call", call)
        monkeypatch.setattr(cli, "process_identity", lambda pid: 1)
        monkeypatch.setattr(
            cli, "process_alive", lambda pid, created: not finish.is_set() or restart
        )
        task = asyncio.create_task(
            cli.wait_lifecycle(
                Path("unused"), "restart" if restart else "stop", force=True, timeout=2
            )
        )
        await requested.wait()
        assert not task.done()
        finish.set()
        result = await task
        assert result["restarted" if restart else "stopped"]
        assert calls[0][1] == {"force": True}
        assert sum(method != "daemon.status" for method, _ in calls) == 1

    asyncio.run(scenario())


def test_wait_timeout_does_not_resend_or_force_cancel(monkeypatch):
    calls = []

    async def call(path, method, **params):
        calls.append((method, params))
        return {"pid": 42, "started_at": 1}

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "process_identity", lambda pid: 1)
    monkeypatch.setattr(cli, "process_alive", lambda *args: True)
    with pytest.raises(TimeoutError):
        asyncio.run(cli.wait_lifecycle(Path("unused"), "stop", timeout=0.01))
    assert [method for method, _ in calls].count("daemon.stop") == 1


def test_restart_rejection_is_not_reported_as_success(monkeypatch):
    async def call(*args, **kw):
        return {
            "pid": 42,
            "started_at": 1,
            "draining": False,
            "stopping": False,
            "restart_requested": False,
            "configuration_error": "invalid config",
        }

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "process_identity", lambda pid: 1)
    monkeypatch.setattr(cli, "process_alive", lambda *args: True)
    with pytest.raises(RuntimeError, match="invalid config"):
        asyncio.run(cli.wait_lifecycle(Path("unused"), "restart", timeout=1))

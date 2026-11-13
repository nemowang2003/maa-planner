import asyncio
import threading

import pytest

from maa_planner.client import ControlClient
from maa_planner.daemon import observations as api_module
from maa_planner.daemon.transport import QUEUE_MESSAGES, ControlServer
from tests.helpers.async_tools import eventually
from tests.helpers.daemon import running


def test_concurrent_requests_match_replies_while_notifications_arrive(tmp_path):
    async def scenario():
        gate = asyncio.Event()

        async def handle(sid, message):
            if message["method"] == "slow":
                await gate.wait()
            return {"result": message["method"]}

        server = ControlServer(tmp_path / "control.json", handle)
        await server.start()
        try:
            async with ControlClient(server.path) as client:
                slow = asyncio.create_task(client.call("slow"))
                await eventually(lambda: bool(server.requests))
                server.notifications.publish({"kind": "alert.raised", "profile": "测试账号"})
                assert await client.call("fast") == "fast"
                assert (await client.receive())["profile"] == "测试账号"
                assert not slow.done()
                gate.set()
                assert await slow == "slow"
        finally:
            gate.set()
            await server.close()

    asyncio.run(scenario())


def test_alerts_fan_out_without_replay_and_failed_transactions_never_publish(tmp_path, monkeypatch):
    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, _, _):
            path = daemon.server.path
            async with ControlClient(path) as a, ControlClient(path) as b:
                store = daemon.store
                assert store is not None
                store.observe_alert("nemo", "test", "item", {"reason": "库存不足"}, now=1)
                first, second = await asyncio.gather(a.receive(), b.receive())
                assert first == second
                assert first["kind"] == "alert.raised" and first["profile"] == "nemo"
                assert first["data"]["details"]["reason"] == "库存不足"
                assert "nemo" in first["message"]
                await a.close()
                await eventually(lambda: len(daemon.server.sessions) == 1)
                store.observe_alert("nemo", "test", "item", None, now=2)
                assert (await b.receive())["kind"] == "alert.resolved"
                async with ControlClient(path) as c:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(c.receive(), 0.05)
                    store.db.execute(
                        "CREATE TRIGGER reject_alert BEFORE INSERT ON alerts BEGIN SELECT RAISE(ABORT,'rejected'); END"
                    )
                    import sqlite3

                    with pytest.raises(sqlite3.IntegrityError):
                        store.observe_alert("other", "test", "item", {}, now=3)
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(c.receive(), 0.05)

    asyncio.run(scenario())


def test_operation_completion_targets_origin_and_survives_client_disconnect(tmp_path, monkeypatch):
    gate = threading.Event()
    calls = []

    def execute(action, settings, cancel, root):
        calls.append(action)
        assert gate.wait(5)
        assert not cancel.is_set()
        return {"instances": 4}

    monkeypatch.setattr(api_module, "execute_sync", execute)

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, _, _):
            async with (
                ControlClient(daemon.server.path) as a,
                ControlClient(daemon.server.path) as b,
            ):
                result = await a.call("emulator.list", idempotency_key="origin")
                assert result["operation_id"] != "origin" and result["idempotency_key"] == "origin"
                await eventually(lambda: bool(calls))
                gate.set()
                async with asyncio.timeout(3):
                    while True:
                        event = await a.receive()
                        if event["data"]["status"] == "succeeded":
                            assert event["data"]["result"] == {"instances": 4}
                            break
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(b.receive(), 0.05)
                gate.clear()
                await a.call("emulator.list", idempotency_key="offline")
                await a.close()
                gate.set()
                await eventually(lambda: daemon.operations.find("offline")["status"] == "succeeded")
                async with ControlClient(daemon.server.path) as c:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(c.receive(), 0.05)
                    assert (await c.call("operation.find", idempotency_key="offline"))[
                        "status"
                    ] == "succeeded"
            assert calls == ["emulators", "emulators"]

    try:
        asyncio.run(scenario())
    finally:
        gate.set()


def test_request_timeout_does_not_retry_or_cancel_handler(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        calls = []

        async def handle(sid, message):
            calls.append(message)
            await gate.wait()
            return {"result": "done"}

        server = ControlServer(tmp_path / "control.json", handle)
        await server.start()
        try:
            async with ControlClient(server.path) as client:
                with pytest.raises(TimeoutError):
                    await client.call("execute", timeout=0.05)
                assert len(calls) == 1 and len(server.requests) == 1
                gate.set()
                await eventually(lambda: not server.requests)
                assert await client.call("next") == "done"
        finally:
            gate.set()
            await server.close()

    asyncio.run(scenario())


def test_slow_session_disconnect_does_not_block_others(tmp_path, monkeypatch):
    async def scenario():
        async def handle(sid, message):
            return {"result": "ok"}

        server = ControlServer(tmp_path / "control.json", handle)
        await server.start()
        try:
            async with ControlClient(server.path), ControlClient(server.path) as b:
                slow_sid, fast_sid = tuple(server.sessions)
                original = server.sio.emit
                gate = asyncio.Event()

                async def emit(event, data, *, to):
                    if to == slow_sid:
                        await gate.wait()
                    await original(event, data, to=to)

                monkeypatch.setattr(server.sio, "emit", emit)
                # Fill only the stalled session's bounded queue.
                for index in range(QUEUE_MESSAGES + 1):
                    server.notify(slow_sid, {"index": index})
                assert slow_sid not in server.sessions and fast_sid in server.sessions
                server.notifications.publish({"kind": "still_online"})
                assert await asyncio.wait_for(b.receive(), 2) == {"kind": "still_online"}
                assert await b.call("status") == "ok"
                assert not server.sessions[fast_sid].queue.full()
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [False, True])
def test_batch_parent_finishes_without_a_query_and_only_notifies_origin(
    tmp_path, monkeypatch, failed
):
    from maa_planner.daemon.coordinator import Daemon

    async def scenario():
        gate = asyncio.Event()

        async def submit_run(self, identifier, settings, target, *args, **kwargs):
            async def finish():
                try:
                    await gate.wait()
                    self.store.operation_update(
                        identifier, "failed" if failed and target == "nemo" else "succeeded"
                    )
                finally:
                    self.operations.tasks.pop(identifier, None)
                    self.wakeup.set()

            self.operations.tasks[identifier] = asyncio.create_task(finish())

        monkeypatch.setattr(Daemon, "submit_run", submit_run)
        async with running(tmp_path, monkeypatch) as (daemon, _, _):
            daemon.config.write_text('[[profiles]]\nname="nemo"\n[[profiles]]\nname="other"\n')
            await eventually(lambda: len(daemon._settings.profiles) == 2)
            async with (
                ControlClient(daemon.server.path) as a,
                ControlClient(daemon.server.path) as b,
            ):
                batch = await a.call(
                    "execution.batch",
                    idempotency_key="batch",
                    jobs=[
                        {
                            "method": "execution.run",
                            "profile": target,
                            "plan": {"tasks": [{"type": "Award"}]},
                        }
                        for target in ("nemo", "other")
                    ],
                )
                gate.set()
                terminals = set()
                async with asyncio.timeout(3):
                    while True:
                        event = await a.receive()
                        operation = event["data"]
                        if operation["status"] in {"succeeded", "failed"}:
                            terminals.add(operation["operation_id"])
                            if operation["operation_id"] == batch["operation_id"]:
                                assert operation["status"] == ("failed" if failed else "succeeded")
                                break
                assert terminals == {batch["operation_id"], *batch["result"]["operations"]}
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(b.receive(), 0.05)

    asyncio.run(scenario())


def test_disconnect_fails_pending_call_without_cancelling_daemon_handler(tmp_path):
    async def scenario():
        gate = asyncio.Event()

        async def handle(sid, message):
            await gate.wait()
            return {"result": "finished"}

        server = ControlServer(tmp_path / "control.json", handle)
        await server.start()
        try:
            async with ControlClient(server.path) as client:
                pending = asyncio.create_task(client.call("execute"))
                await eventually(lambda: bool(server.requests))
                await client.close()
                with pytest.raises(ConnectionError):
                    await asyncio.wait_for(pending, 1)
                assert server.requests
                gate.set()
                await eventually(lambda: not server.requests)
        finally:
            gate.set()
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_or_cancelled_connection_closes_http_session(tmp_path, monkeypatch, cancelled):
    import json

    import aiohttp
    import socketio.exceptions

    async def scenario():
        path = tmp_path / "control.json"
        path.write_text(json.dumps({"url": "http://127.0.0.1:1", "token": "x" * 32}))
        client = ControlClient(path)
        http = aiohttp.ClientSession()

        async def connect(*args, **kwargs):
            client.sio.eio.http = http
            if cancelled:
                raise asyncio.CancelledError()
            raise socketio.exceptions.ConnectionError("failed")

        monkeypatch.setattr(client.sio, "connect", connect)
        with pytest.raises(asyncio.CancelledError if cancelled else ConnectionError):
            await client.connect()
        assert http.closed

    asyncio.run(scenario())


def test_endpoint_sharing_retry_only_handles_transient_windows_sharing_errors():
    from maa_planner.daemon.transport import retry_file_sharing

    class SharingViolation(PermissionError):
        winerror = 32

    async def scenario():
        calls = []

        def write():
            calls.append(True)
            if len(calls) < 3:
                raise SharingViolation()
            return "published"

        assert await retry_file_sharing(write) == "published"
        assert len(calls) == 3

        def denied():
            raise PermissionError("do not retry permissions")

        with pytest.raises(PermissionError, match="do not retry"):
            await retry_file_sharing(denied)

    asyncio.run(scenario())


def test_batch_sized_notification_burst_keeps_clients_connected(tmp_path):
    async def scenario():
        async def handle(sid, message):
            return {"result": "ok"}

        server = ControlServer(tmp_path / "control.json", handle)
        await server.start()
        try:
            async with ControlClient(server.path) as client:
                # One parent and the 100 members allowed by batch.submit.
                for index in range(101):
                    server.notifications.publish(
                        {"kind": "operation.updated", "data": {"index": index}}
                    )
                await eventually(lambda: client.notifications.qsize() == 101)
                assert [(await client.receive())["data"]["index"] for _ in range(101)] == list(
                    range(101)
                )
                assert await client.call("status") == "ok"
        finally:
            await server.close()

    asyncio.run(scenario())

import asyncio
import json
from typing import cast

import pytest

from maa_planner.client import ControlClient
from maa_planner.daemon.transport import ControlServer
from maa_planner.interfaces.wecom import bot as module
from maa_planner.interfaces.wecom.bot import WeComBot
from maa_planner.interfaces.wecom.connection import WeComConnection
from tests.helpers.async_tools import eventually
from tests.helpers.wecom import Peer, callback, config, wecom_server


def test_real_mock_wecom_and_socketio_routes_permissions_deduplication_and_shutdown(
    tmp_path, monkeypatch
):
    async def scenario():
        calls = []

        async def handle(sid, message):
            calls.append(message)
            if message["method"] == "alert.list":
                result = [{"profile": message["params"]["profile"]}]
            else:
                result = {"status": "ok"}
            return {"result": result}

        server = ControlServer(tmp_path / "control.json", handle)
        await server.start()
        stop = asyncio.Event()
        task = None
        try:
            async with wecom_server() as upstream:
                monkeypatch.setattr(
                    module,
                    "WeComConnection",
                    lambda bot, secret: WeComConnection(bot, secret, url=upstream.url),
                )
                bot = WeComBot(config(tmp_path), server.path)
                task = asyncio.create_task(bot.run(stop))
                await eventually(lambda: bot.wecom is not None and bot.daemon is not None)
                peer = upstream.peers[-1]
                await peer.send_json(callback("alice", "alert.list", "/alerts"))
                await peer.send_json(callback("alice", "alert.list", "/alerts"))
                await eventually(lambda: "alert.list" in bot.completed and len(upstream.sent) == 1)
                await peer.send_json(callback("alice", "alert.list", "/alerts"))
                assert calls == [{"method": "alert.list", "params": {"profile": "nemo"}}]
                await peer.send_json(
                    callback("stranger", "bad", '/api {"method": "daemon.stop", "params": {}}')
                )
                await peer.send_json(callback("bob", "deny", "/status"))
                await peer.send_json(callback("admin", "status", "/status"))
                await eventually(lambda: len(upstream.sent) == 3)
                assert calls[-1]["method"] == "daemon.status"
                assert len(calls) == 2
                assert any(
                    (
                        m["chatid"] == "bob" and "管理员" in m["markdown"]["content"]
                        for m in upstream.sent
                    )
                )
                assert all((m["chatid"] in {"alice", "bob", "admin"} for m in upstream.sent))
                for profile in ("nemo", "wyf", "unrouted", None):
                    server.notifications.publish(
                        {
                            "kind": "alert.raised",
                            "profile": profile,
                            "message": f"alert:{profile}",
                            "data": {"reason": "材料不足"},
                        }
                    )
                await eventually(lambda: len(upstream.sent) == 7)
                targets = {
                    (m["chatid"], json.loads(m["markdown"]["content"].split("\n", 2)[2])["reason"])
                    for m in upstream.sent[3:]
                }
                assert targets == {
                    ("alice", "材料不足"),
                    ("bob", "材料不足"),
                    ("admin", "材料不足"),
                }
                assert not any(("unrouted" in m["markdown"]["content"] for m in upstream.sent))
                stop.set()
                await asyncio.wait_for(task, 3)
                assert bot.wecom is None and bot.daemon is None
                assert not server.sessions
        finally:
            stop.set()
            if task is not None:
                await asyncio.wait_for(task, 3)
            await server.close()

    asyncio.run(scenario())


def test_completion_before_ack_is_private_and_ordered_after_acceptance(tmp_path):
    async def scenario():
        bot = WeComBot(config(tmp_path), tmp_path / "control.json")
        bot.wecom = cast(WeComConnection, object())

        class Immediate(Peer):
            async def request(self, message):
                self.calls.append(message)
                key = message["params"]["idempotency_key"]
                identifier = "server-generated-operation"
                await bot.notification(
                    {
                        "kind": "operation.updated",
                        "data": {
                            "operation_id": identifier,
                            "idempotency_key": key,
                            "method": "execution.run",
                            "profile": "nemo",
                            "status": "succeeded",
                        },
                    }
                )
                assert bot._messages.empty()
                return {
                    "result": {
                        "operation_id": identifier,
                        "idempotency_key": key,
                        "status": "preparing",
                    }
                }

        peer = Immediate()
        bot.daemon = cast(ControlClient, peer)
        await bot.handle(
            "alice",
            "one",
            '/api {"method": "execution.run", "params": {"plan": {"tasks": [{"type": "Award"}]}, "profile": "nemo"}}',
        )
        first, second = (bot._messages.get_nowait(), bot._messages.get_nowait())
        assert first[0] == second[0] == "alice"
        assert "preparing" in first[1] and "succeeded" in second[1]
        assert not bot.operations
        assert len(peer.calls) == 1

    asyncio.run(scenario())


def test_offline_notifications_are_dropped_and_unknown_requests_are_not_retried(tmp_path):
    async def scenario():
        bot = WeComBot(config(tmp_path), tmp_path / "control.json")
        await bot.notification({"kind": "alert.raised", "profile": "nemo", "data": {}})
        assert bot._messages.empty()
        bot.wecom = cast(WeComConnection, object())

        class Timeout(Peer):
            async def request(self, message):
                self.calls.append(message)
                raise TimeoutError()

        peer = Timeout()
        bot.daemon = cast(ControlClient, peer)
        await bot.handle(
            "alice",
            "uncertain",
            '/api {"method": "execution.run", "params": {"profile": "nemo", "plan": {"tasks": [{"type": "Award"}]}}}',
        )
        assert len(peer.calls) == 1
        reply = bot._messages.get_nowait()
        assert "结果可能未知" in reply[1] and "idempotency_key=wecom-" in reply[1]
        assert len(bot.operations) == 1
        bot.wecom = None
        identifier = next(iter(bot.operations))
        await bot.notification(
            {
                "kind": "operation.updated",
                "data": {"operation_id": identifier, "status": "succeeded"},
            }
        )
        assert bot._messages.empty() and (not bot.operations)

    asyncio.run(scenario())


def test_wecom_reconnection_drops_offline_messages_without_replaying_commands(
    tmp_path, monkeypatch
):
    async def scenario():
        async with wecom_server() as upstream:
            monkeypatch.setattr(
                module,
                "WeComConnection",
                lambda bot, secret: WeComConnection(bot, secret, url=upstream.url),
            )
            bot = WeComBot(config(tmp_path), tmp_path / "unused")
            task = asyncio.create_task(bot._wecom_loop())
            try:
                await eventually(lambda: bot.wecom is not None)
                await upstream.peers[-1].send_json(callback("alice", "help", "/help"))
                await eventually(lambda: len(upstream.sent) == 1)
                await upstream.peers[-1].close()
                await eventually(lambda: bot.wecom is None)
                await bot.notification(
                    {"kind": "alert.raised", "profile": "nemo", "message": "offline", "data": {}}
                )
                assert bot._messages.empty()
                await eventually(lambda: len(upstream.subscriptions) == 2 and bot.wecom is not None)
                await upstream.peers[-1].send_json(callback("alice", "help", "/help"))
                await bot.notification(
                    {"kind": "alert.raised", "profile": "nemo", "message": "fresh", "data": {}}
                )
                await eventually(lambda: len(upstream.sent) == 3)
                assert not any("offline" in m["markdown"]["content"] for m in upstream.sent)
                assert (
                    len(
                        [m for m in upstream.sent if "maa-planner 命令" in m["markdown"]["content"]]
                    )
                    == 1
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert bot.wecom is None

    asyncio.run(scenario())


def test_wecom_superseded_session_exits_instead_of_fighting_another_client(tmp_path, monkeypatch):
    from maa_planner.interfaces.wecom.connection import ConnectionReplacedError

    async def scenario():
        async with wecom_server() as upstream:
            monkeypatch.setattr(
                module,
                "WeComConnection",
                lambda bot, secret: WeComConnection(bot, secret, url=upstream.url),
            )
            bot = WeComBot(config(tmp_path), tmp_path / "unused")
            task = asyncio.create_task(bot._wecom_loop())
            await eventually(lambda: bot.wecom is not None)
            await upstream.peers[-1].send_json(
                {
                    "cmd": "aibot_event_callback",
                    "body": {"event": {"eventtype": "disconnected_event"}},
                }
            )
            with pytest.raises(ConnectionReplacedError):
                await asyncio.wait_for(task, 2)
            assert bot.wecom is None and len(upstream.subscriptions) == 1

    asyncio.run(scenario())


def test_admin_does_not_implicitly_receive_account_notifications(tmp_path):
    from dataclasses import replace

    async def scenario():
        settings = replace(config(tmp_path), routes={"nemo": frozenset({"alice"})})
        bot = WeComBot(settings, tmp_path / "unused")
        bot.wecom = cast(WeComConnection, object())
        await bot.notification({"kind": "alert.raised", "profile": "nemo", "data": {}})
        assert bot._messages.get_nowait()[0] == "alice"
        assert bot._messages.empty()
        await bot.notification({"kind": "alert.raised", "profile": None, "data": {}})
        assert bot._messages.get_nowait()[0] == "admin"
        assert bot._messages.empty()
        await bot.notification({"kind": "alert.raised", "profile": "admin", "data": {}})
        assert bot._messages.get_nowait()[0] == "admin"
        assert bot._messages.empty()

    asyncio.run(scenario())


def test_private_command_replies_use_callback_but_notifications_and_group_results_are_private(
    tmp_path, monkeypatch
):
    async def scenario():
        async with wecom_server() as upstream:
            monkeypatch.setattr(
                module,
                "WeComConnection",
                lambda bot, secret: WeComConnection(bot, secret, url=upstream.url),
            )
            bot = WeComBot(config(tmp_path), tmp_path / "unused-control.json")
            task = asyncio.create_task(bot._wecom_loop())
            try:
                await eventually(lambda: bot.wecom is not None)
                message = callback("alice", "private-help", "/help")
                message["body"]["chattype"] = "single"
                await upstream.peers[-1].send_json(message)
                await eventually(lambda: len(upstream.replies) == 1)
                reply = upstream.replies[0]
                assert reply["headers"] == message["headers"]
                assert reply["body"]["msgtype"] == "stream"
                assert reply["body"]["stream"]["finish"] is True
                assert "/help" in reply["body"]["stream"]["content"]
                assert not upstream.sent
                await upstream.peers[-1].send_json(callback("alice", "group-help", "/help"))
                await eventually(lambda: len(upstream.sent) == 1)
                assert upstream.sent[0]["chatid"] == "alice"
                await bot.notification(
                    {"kind": "alert.raised", "profile": "wyf", "message": "test", "data": {}}
                )
                await eventually(lambda: len(upstream.sent) == 2)
                assert upstream.sent[1]["chatid"] == "bob"
                assert len(upstream.replies) == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

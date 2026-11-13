import asyncio
import json

import pytest
from aiohttp import web

from maa_planner.interfaces.wecom import connection
from maa_planner.interfaces.wecom.connection import (
    AuthenticationError,
    WeComConnection,
    WeComError,
    split_content,
)


def test_utf8_parts_reassemble_and_obey_the_protocol_limit():
    text = "中文🙂\n" * 10000
    parts = split_content(text)
    assert len(parts) > 1
    assert "".join(parts) == text
    assert all(len(part.encode()) <= 20480 for part in parts)


@pytest.mark.parametrize("rejected", [False, True])
def test_authentication_ack_matching_callbacks_and_proactive_messages(monkeypatch, rejected):
    async def scenario():
        received = []
        peers = []

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            peers.append(ws)
            async for raw in ws:
                message = json.loads(raw.data)
                received.append(message)
                await ws.send_json(
                    {
                        "headers": message["headers"],
                        "errcode": 400 if rejected else 0,
                        "errmsg": "secret must not appear in errors",
                    }
                )
                if message["cmd"] == "aibot_subscribe" and not rejected:
                    await ws.send_json(
                        {
                            "cmd": "aibot_msg_callback",
                            "headers": {"req_id": "callback"},
                            "body": {"msgid": "one"},
                        }
                    )
            return ws

        app = web.Application()
        app.router.add_get("/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        monkeypatch.setattr(connection, "PING_INTERVAL", 0.01)
        client = WeComConnection("bot", "secret", url=url)
        try:
            if rejected:
                with pytest.raises(AuthenticationError) as error:
                    await client.connect()
                assert "secret" not in str(error.value)
                assert client.http is not None and client.http.closed
            else:
                async with client:
                    assert (await asyncio.wait_for(client.receive(), 1))["body"]["msgid"] == "one"
                    await client.send("group", "中文🙂" * 4000)
                    await asyncio.sleep(0.03)
                    sent = [m for m in received if m["cmd"] == "aibot_send_msg"]
                    assert len(sent) == 2
                    assert all(m["body"]["chatid"] == "group" for m in sent)
                    assert all(
                        len(m["body"]["markdown"]["content"].encode()) <= 20480 for m in sent
                    )
                    assert any(m["cmd"] == "ping" for m in received)
                assert client.http is not None and client.http.closed
            assert received[0]["body"] == {"bot_id": "bot", "secret": "secret"}
        finally:
            await client.close()
            for peer in peers:
                await peer.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_outbound_error_only_reports_code():
    assert "123" in str(WeComError("aibot_send_msg", 123))


def test_callback_reply_chunks_preserve_request_id_and_content():
    from tests.helpers.wecom import callback, wecom_server

    async def scenario():
        async with wecom_server() as upstream:
            async with WeComConnection("bot", "secret", url=upstream.url) as client:
                text = "中文🙂\n" * 10000
                message = callback("alice", "original-callback", "/help")
                await client.reply(message, text)
                replies = upstream.replies
                assert len(replies) > 1
                assert all(m["headers"] == message["headers"] for m in replies)
                streams = [m["body"]["stream"] for m in replies]
                assert all(
                    s["finish"] is True and len(s["content"].encode()) <= 20480 for s in streams
                )
                assert len({s["id"] for s in streams}) == len(streams)
                assert "".join(s["content"] for s in streams) == text
                assert not upstream.sent
                with pytest.raises(ValueError, match="回复编号"):
                    await client.reply({}, "bad callback")

    asyncio.run(scenario())

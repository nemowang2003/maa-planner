"""WeCom configuration and public-protocol peers without external services."""

import json
from contextlib import asynccontextmanager

from aiohttp import web

from maa_planner.interfaces.wecom.config import WeComConfig


def config_document():
    return {
        "bot_id": "test-bot",
        "secret": "test-secret",
        "admins": ["admin"],
        "routes": {"nemo": ["alice", "admin"], "wyf": ["bob"]},
    }


def config_file(tmp_path):
    path = tmp_path / "wecom.json"
    path.write_text(json.dumps(config_document()), encoding="utf-8")
    return path


def config(tmp_path):
    return WeComConfig.load(config_file(tmp_path))


class Peer:
    def __init__(self, operations=None):
        self.calls = []
        self.operations = operations or {}

    async def request(self, message):
        self.calls.append(message)
        if message["method"] == "operation.get":
            value = self.operations[message["params"]["operation_id"]]
        elif message["method"] == "operation.find":
            value = next(
                (
                    v
                    for v in self.operations.values()
                    if v.get("idempotency_key") == message["params"]["idempotency_key"]
                ),
                None,
            )
        else:
            value = {"ok": True}
        return {"result": value}


@asynccontextmanager
async def wecom_server():
    from types import SimpleNamespace

    state = SimpleNamespace(peers=[], sent=[], replies=[], subscriptions=[])

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state.peers.append(ws)
        async for frame in ws:
            message = json.loads(frame.data)
            if message["cmd"] == "aibot_send_msg":
                state.sent.append(message["body"])
            elif message["cmd"] == "aibot_respond_msg":
                state.replies.append(message)
            elif message["cmd"] == "aibot_subscribe":
                state.subscriptions.append(message["body"])
            await ws.send_json({"headers": message["headers"], "errcode": 0})
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        yield state
    finally:
        for peer in state.peers:
            await peer.close()
        await runner.cleanup()


def callback(user, msgid, content):
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": msgid},
        "body": {
            "msgid": msgid,
            "aibotid": "test-bot",
            "from": {"userid": user},
            "chattype": "group",
            "chatid": "never-send-to-group",
            "msgtype": "text",
            "text": {"content": content},
        },
    }

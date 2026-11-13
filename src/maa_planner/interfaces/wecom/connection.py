"""WeCom smart-bot WebSocket frames, acknowledgements and application heartbeat."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress

import aiohttp

WSS_URL = "wss://openws.work.weixin.qq.com"
CONTENT_BYTES = 20480
ACK_TIMEOUT = 15
PING_INTERVAL = 30


class WeComError(RuntimeError):
    def __init__(self, command: str, code: int):
        # Never include remote error messages; they may echo credentials or user text.
        super().__init__(f"企业微信拒绝 {command}，错误码 {code}")
        self.code = code


class AuthenticationError(WeComError):
    pass


class ConnectionReplacedError(RuntimeError):
    pass


def split_content(content: str, limit: int = CONTENT_BYTES) -> list[str]:
    """Split by UTF-8 bytes without damaging Unicode code points."""
    parts, current, size = [], [], 0
    for char in content:
        length = len(char.encode("utf-8"))
        if size + length > limit:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += length
    if current:
        parts.append("".join(current))
    return parts


class WeComConnection:
    def __init__(self, bot_id: str, secret: str, *, url: str = WSS_URL):
        self.bot_id, self.secret, self.url = bot_id, secret, url
        self.http: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.callbacks: asyncio.Queue = asyncio.Queue(128)
        self.reader: asyncio.Task | None = None
        self.heartbeat: asyncio.Task | None = None
        self._write = asyncio.Lock()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def connect(self) -> None:
        # Use a direct connection like codex-wecom-relay; no environment proxy,
        # compression negotiation or unsolicited WebSocket heartbeat pings.
        self.http = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=20))
        try:
            self.ws = await self.http.ws_connect(
                self.url, heartbeat=None, compress=0, max_msg_size=1024 * 1024
            )
            self.reader = asyncio.create_task(self._read())
            await self.command("aibot_subscribe", {"bot_id": self.bot_id, "secret": self.secret})
            self.heartbeat = asyncio.create_task(self._heartbeat())
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        tasks = [task for task in (self.heartbeat, self.reader) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.ws is not None:
            await self.ws.close()
        if self.http is not None:
            await self.http.close()
        self._fail_pending()

    def _fail_pending(self) -> None:
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("企业微信连接已断开，发送结果可能未知"))

    async def _read(self) -> None:
        assert self.ws is not None
        try:
            async for frame in self.ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    if frame.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError("企业微信 WebSocket 错误")
                    continue
                try:
                    message = json.loads(frame.data)
                    if not isinstance(message, dict):
                        raise ValueError()
                    headers = message.get("headers", {})
                    if not isinstance(headers, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ConnectionError("企业微信返回无效消息") from None
                request_id = headers.get("req_id")
                if isinstance(request_id, str) and request_id in self.pending:
                    future = self.pending[request_id]
                    if not future.done():
                        future.set_result(message)
                    continue
                if message.get("cmd") == "aibot_msg_callback":
                    if self.callbacks.full():
                        raise ConnectionError("企业微信消息处理积压，断开当前连接")
                    self.callbacks.put_nowait(message)
                elif message.get("cmd") == "aibot_event_callback":
                    body = message.get("body", {})
                    if isinstance(body, dict) and isinstance(body.get("event"), dict):
                        if body["event"].get("eventtype") == "disconnected_event":
                            raise ConnectionReplacedError(
                                "企业微信连接被另一实例替代，请只运行一个使用此 BotID 的客户端"
                            )
            raise ConnectionError("企业微信连接已关闭")
        finally:
            self._fail_pending()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await self.command("ping")

    async def receive(self) -> dict:
        assert self.reader is not None and self.heartbeat is not None
        callback = asyncio.create_task(self.callbacks.get())
        try:
            done, _ = await asyncio.wait(
                [callback, self.reader, self.heartbeat], return_when=asyncio.FIRST_COMPLETED
            )
            for task in (self.reader, self.heartbeat):
                if task in done:
                    await task
                    raise ConnectionError("企业微信连接已结束")
            return callback.result()
        finally:
            callback.cancel()
            with suppress(asyncio.CancelledError):
                await callback

    async def command(
        self, command: str, body: dict | None = None, *, request_id: str | None = None
    ) -> dict:
        if self.ws is None or self.ws.closed:
            raise ConnectionError("企业微信尚未连接")
        request_id = request_id or "maa-" + uuid.uuid4().hex
        if request_id in self.pending:
            raise ConnectionError("企业微信回复编号正在使用")
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(ACK_TIMEOUT):
                async with self._write:
                    await self.ws.send_json(
                        {"cmd": command, "headers": {"req_id": request_id}, "body": body or {}}
                    )
                response = await future
            if type(response.get("errcode")) is not int:
                raise ConnectionError("企业微信回执缺少有效错误码")
            if response["errcode"] != 0:
                error = AuthenticationError if command == "aibot_subscribe" else WeComError
                raise error(command, response["errcode"])
            return response
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def send(self, chat_id: str, content: str) -> None:
        for part in split_content(content):
            await self.command(
                "aibot_send_msg",
                {
                    "chatid": chat_id,
                    "msgtype": "markdown",
                    "markdown": {"content": part},
                },
            )

    async def reply(self, callback: dict, content: str) -> None:
        request_id = callback.get("headers", {}).get("req_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("企业微信回调缺少回复编号")
        for part in split_content(content):
            await self.command(
                "aibot_respond_msg",
                {
                    "msgtype": "stream",
                    "stream": {"id": uuid.uuid4().hex, "finish": True, "content": part},
                },
                request_id=request_id,
            )

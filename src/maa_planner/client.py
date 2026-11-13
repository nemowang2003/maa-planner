"""Small Socket.IO peer; independent of daemon and interface implementations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import socketio
import socketio.exceptions

from maa_planner.endpoint import default_endpoint, read_endpoint
from maa_planner.schema import (
    MAX_MESSAGE,
    MAX_PENDING_NOTIFICATIONS,
    Request,
    Response,
)


class ControlClient:
    def __init__(self, endpoint: Path | None = None):
        self.endpoint = endpoint or default_endpoint()
        self.sio = socketio.AsyncClient(reconnection=False)
        self.sio.on("notification", self._notification)
        self.sio.on("disconnect", self._disconnect)
        self.notifications: asyncio.Queue = asyncio.Queue(MAX_PENDING_NOTIFICATIONS)
        self.pending: set[asyncio.Future] = set()
        self._write = asyncio.Lock()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def connect(self) -> None:
        while not self.notifications.empty():
            self.notifications.get_nowait()
        endpoint = read_endpoint(self.endpoint)
        try:
            await self.sio.connect(
                endpoint.url,
                auth={"token": endpoint.token},
                transports=["websocket"],
                wait_timeout=5,
            )
        except BaseException as error:
            await self.close()
            if isinstance(error, (socketio.exceptions.ConnectionError, OSError)):
                raise ConnectionError("无法连接本机 daemon") from error
            raise

    async def close(self) -> None:
        await self.sio.disconnect()
        await self.sio.shutdown()

    async def _notification(self, value):
        if self.notifications.full():
            # Application handlers aren't keeping up; end this session without replay.
            await self.sio.disconnect()
            return
        self.notifications.put_nowait(value)

    async def _disconnect(self, reason=None):
        for future in tuple(self.pending):
            if not future.done():
                future.set_exception(ConnectionError("daemon 连接已断开，请求结果可能未知"))
        while not self.notifications.empty():
            self.notifications.get_nowait()
        self.notifications.put_nowait(None)

    async def receive(self) -> dict:
        if not self.sio.connected and self.notifications.empty():
            raise ConnectionError("daemon 连接已断开")
        value = await self.notifications.get()
        if value is None:
            raise ConnectionError("daemon 连接已断开")
        return value

    async def request(self, message: dict, *, timeout: float = 30) -> dict:
        if not self.sio.connected:
            raise ConnectionError("daemon 尚未连接")
        if len(json.dumps(message, ensure_ascii=False, allow_nan=False).encode()) > MAX_MESSAGE:
            raise ValueError("请求超过长度限制")
        future = asyncio.get_running_loop().create_future()
        self.pending.add(future)

        def reply(value):
            if not future.done():
                future.set_result(value)

        try:
            async with asyncio.timeout(timeout):
                # Serialize packet emission only; requests can wait for ACKs concurrently.
                async with self._write:
                    await self.sio.emit("request", message, callback=reply)
                return await future
        finally:
            self.pending.discard(future)
            if not future.done():
                future.cancel()

    async def call(self, method: str, *, timeout: float = 30, **params):
        return Response.from_dict(
            await self.request(Request(method, params).to_dict(), timeout=timeout)
        ).unwrap()

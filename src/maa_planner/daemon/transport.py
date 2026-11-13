"""Online-only Socket.IO sessions over authenticated loopback WebSockets."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import engineio
import socketio
from aiohttp import web

from maa_planner.daemon.adapters.security import private_directory, write_private
from maa_planner.daemon.notifications import NotificationHub
from maa_planner.daemon.tools.locks import lock_file
from maa_planner.endpoint import read_endpoint
from maa_planner.schema import MAX_MESSAGE, MAX_PENDING_NOTIFICATIONS, Response

Handler = Callable[[str, dict], Awaitable[dict]]
QUEUE_MESSAGES = MAX_PENDING_NOTIFICATIONS
QUEUE_BYTES = 4 * MAX_MESSAGE
SEND_TIMEOUT = 5


async def retry_file_sharing(action):
    """Windows readers may briefly deny rename/delete of the discovery file."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    while True:
        try:
            return action()
        except OSError as error:
            if getattr(error, "winerror", None) not in {32, 33} or loop.time() >= deadline:
                raise
            await asyncio.sleep(0.02)


class BoundedEngine(engineio.AsyncServer):
    def create_queue(self, *args, **kwargs):
        # Also bound the library queue, beyond our per-session application queue.
        return asyncio.Queue(maxsize=QUEUE_MESSAGES)


class SessionServer(socketio.AsyncServer):
    def _engineio_server_class(self):
        return BoundedEngine


@dataclass
class Session:
    transport: asyncio.Transport
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_MESSAGES))
    queued_bytes: int = 0
    sender: asyncio.Task | None = None
    requests: int = 0


class ControlServer:
    def __init__(
        self, path: Path, handler: Handler, *, notifications: NotificationHub | None = None
    ):
        self.path = path
        self.handler = handler
        self.notifications = notifications or NotificationHub()
        self.sio = SessionServer(
            async_mode="aiohttp",
            transports=["websocket"],
            cors_allowed_origins=[],
            max_http_buffer_size=MAX_MESSAGE,
            ping_interval=20,
            ping_timeout=20,
            logger=False,
            engineio_logger=False,
        )
        self.app = web.Application(client_max_size=MAX_MESSAGE)
        self.sio.attach(self.app)
        self.sio.on("connect", self._connect)
        self.sio.on("disconnect", self._disconnect)
        self.sio.on("request", self._request)
        self.sessions: dict[str, Session] = {}
        self.requests: set[asyncio.Task] = set()
        self.runner: web.AppRunner | None = None
        self._lock = None
        self._serving = False
        self.token = secrets.token_urlsafe(32)

    def is_serving(self) -> bool:
        return self._serving

    async def start(self) -> None:
        private_directory(self.path.parent)
        self._lock = lock_file(self.path.with_suffix(".lock"))
        self._lock.__enter__()
        try:
            if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
                raise ValueError("端点必须为普通文件")
            if self.path.exists():
                # Only replace a stale descriptor produced by this implementation.

                read_endpoint(self.path)
            self.runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=2)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            port = self.runner.addresses[0][1]
            content = json.dumps(
                {
                    "url": f"http://127.0.0.1:{port}",
                    "token": self.token,
                }
            ).encode()
            await retry_file_sharing(lambda: write_private(self.path, content))
            self._serving = True
        except BaseException:
            if self.runner is not None:
                await self.runner.cleanup()
            self._lock.__exit__(None, None, None)
            self._lock = None
            raise

    async def _connect(self, sid, environ, auth):
        if (
            not self._serving
            or len(self.sessions) >= 64
            or not isinstance(auth, dict)
            or set(auth) != {"token"}
            or not isinstance(auth["token"], str)
            or not auth["token"].isascii()
            or not hmac.compare_digest(auth["token"], self.token)
        ):
            return False
        transport = environ["aiohttp.request"].transport
        if transport is None:
            return False
        session = Session(transport)
        self.sessions[sid] = session
        self.notifications.connect(sid, lambda value: self.notify(sid, value))
        session.sender = asyncio.create_task(self._send(sid, session))
        return True

    async def _disconnect(self, sid, reason=None):
        self.drop(sid)

    def drop(self, sid: str) -> None:
        session = self.sessions.pop(sid, None)
        if session is None:
            return
        if session.sender is not None and session.sender is not asyncio.current_task():
            session.sender.cancel()
        session.transport.close()
        self.notifications.disconnect(sid)

    async def _request(self, sid, message):
        session = self.sessions.get(sid)
        if session is None:
            return Response(error={"code": "disconnected", "message": "会话已关闭"}).to_dict()
        if session.requests >= 32:
            return Response(error={"code": "busy", "message": "会话并发请求过多"}).to_dict()
        task = asyncio.current_task()
        assert task is not None
        self.requests.add(task)
        session.requests += 1
        try:
            if not isinstance(message, dict):
                raise ValueError("请求必须为对象")
            self._size(message)
            response = await self.handler(sid, message)
            self._size(response)
            # Socket.IO supplies the callback identifier and matches the ACK itself.
            return response
        except (ValueError, TypeError) as error:
            return Response(error={"code": "invalid_argument", "message": str(error)}).to_dict()
        finally:
            session.requests -= 1
            self.requests.discard(task)

    @staticmethod
    def _size(value: dict) -> int:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
        if size > MAX_MESSAGE:
            raise ValueError("消息超过长度限制")
        return size

    def notify(self, sid: str, value: dict) -> None:
        session = self.sessions.get(sid)
        if session is None:
            return
        try:
            size = self._size(value)
        except ValueError:
            self.drop(sid)
            return
        if session.queue.full() or session.queued_bytes + size > QUEUE_BYTES:
            self.drop(sid)
            return
        session.queue.put_nowait((value, size))
        session.queued_bytes += size

    async def _send(self, sid: str, session: Session) -> None:
        try:
            while True:
                value, size = await session.queue.get()
                try:
                    await asyncio.wait_for(
                        self.sio.emit("notification", value, to=sid), SEND_TIMEOUT
                    )
                finally:
                    session.queued_bytes -= size
        except (TimeoutError, ConnectionError):
            self.drop(sid)

    async def close(self) -> None:
        self._serving = False
        # Requests already dispatched belong to the daemon, even after disconnection.
        if self.requests:
            await asyncio.gather(*tuple(self.requests), return_exceptions=True)
        senders = [s.sender for s in self.sessions.values() if s.sender is not None]
        for sid in tuple(self.sessions):
            self.drop(sid)
        await asyncio.gather(*senders, return_exceptions=True)
        await self.sio.shutdown()
        if self.runner is not None:
            await self.runner.cleanup()
        if self._lock is not None:
            try:
                await retry_file_sharing(lambda: self.path.unlink(missing_ok=True))
            finally:
                self._lock.__exit__(None, None, None)
                self._lock = None

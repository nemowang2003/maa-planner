"""Independent WeCom and daemon sessions; online-only private-user delivery."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from maa_planner.client import ControlClient
from maa_planner.schema import ADMIN_CHANNEL, TERMINAL, Request

from .access import AccessPolicy
from .commands import HELP, parse_command, render_alert, render_operation, render_result
from .config import WeComConfig
from .connection import AuthenticationError, ConnectionReplacedError, WeComConnection, WeComError

log = logging.getLogger(__name__)


@dataclass
class OperationRoute:
    user: str
    acknowledged: bool = False
    terminal: dict | None = None


class WeComBot:
    def __init__(self, config: WeComConfig, endpoint: Path):
        self.config, self.endpoint = config, endpoint
        self.access = AccessPolicy(config)
        self.daemon: ControlClient | None = None
        self.wecom: WeComConnection | None = None
        self.completed: OrderedDict[str, None] = OrderedDict()
        self.active: set[str] = set()
        self._user_locks = {user: asyncio.Lock() for user in config.allowed_users}
        self.operations: dict[str, OperationRoute] = {}
        self._messages: asyncio.Queue = asyncio.Queue(256)

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [
            asyncio.create_task(self._daemon_loop()),
            asyncio.create_task(self._wecom_loop()),
            asyncio.create_task(stop.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _daemon_loop(self) -> None:
        delay = 1
        while True:
            try:
                async with ControlClient(self.endpoint) as client:
                    self.daemon = client
                    self.operations.clear()
                    delay = 1
                    log.info("daemon 已连接")
                    while True:
                        await self.notification(await client.receive())
            except (OSError, RuntimeError, ValueError, TimeoutError):
                log.warning("daemon 不可用，%s 秒后重新连接", delay)
            finally:
                self.daemon = None
                self.operations.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _wecom_loop(self) -> None:
        delay = 1
        while True:
            try:
                async with WeComConnection(self.config.bot_id, self.config.secret) as connection:
                    self.wecom = connection
                    delay = 1
                    log.info("企业微信已认证")
                    callbacks = asyncio.Queue(128)
                    workers = [asyncio.create_task(self._commands(callbacks)) for _ in range(8)]
                    sender = asyncio.create_task(self._send_messages(connection))
                    receiver = asyncio.create_task(self._callbacks(connection, callbacks))
                    tasks = [*workers, sender, receiver]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            await task
                    finally:
                        self.wecom = None
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        while not callbacks.empty():
                            user, msgid, text, callback = callbacks.get_nowait()
                            self.active.discard(msgid)
            except (AuthenticationError, ConnectionReplacedError):
                raise
            except (OSError, RuntimeError, ValueError, TimeoutError, aiohttp.ClientError):
                log.warning("企业微信连接不可用，%s 秒后重新连接", delay)
            finally:
                self.wecom = None
                while not self._messages.empty():
                    self._messages.get_nowait()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _callbacks(self, connection: WeComConnection, queue: asyncio.Queue) -> None:
        while True:
            callback = await connection.receive()
            body = callback.get("body", {})
            sender = body.get("from", {}) if isinstance(body, dict) else {}
            user = sender.get("userid") if isinstance(sender, dict) else None
            if not isinstance(user, str) or user not in self.config.allowed_users:
                continue
            msgid = body.get("msgid")
            if not isinstance(msgid, str) or not msgid or len(msgid) > 512:
                continue
            if msgid in self.active or msgid in self.completed:
                continue
            if body.get("aibotid", self.config.bot_id) != self.config.bot_id:
                continue
            if body.get("msgtype") != "text" or not isinstance(body.get("text"), dict):
                self.enqueue(user, "仅支持文本命令，请发送 /help", callback=callback)
                continue
            text = body["text"].get("content")
            if not isinstance(text, str):
                continue
            if queue.full():
                self.enqueue(user, "命令处理繁忙，请稍后用新消息重试", callback=callback)
                continue
            self.active.add(msgid)
            queue.put_nowait((user, msgid, text, callback))

    async def _commands(self, queue: asyncio.Queue) -> None:
        while True:
            user, msgid, text, callback = await queue.get()
            try:
                async with self._user_locks[user]:
                    await self.handle(user, msgid, text, callback=callback)
            finally:
                self.active.discard(msgid)
                self.completed[msgid] = None
                while len(self.completed) > 4096:
                    self.completed.popitem(last=False)

    async def handle(
        self, user: str, msgid: str, text: str, *, callback: dict | None = None
    ) -> None:
        if user not in self.config.allowed_users:
            return

        def respond(content: str) -> None:
            self.enqueue(user, content, callback=callback)

        identifier, claimed = None, False
        try:
            request = parse_command(text, bot_id=self.config.bot_id, message_id=msgid)
            if request is None:
                respond(HELP)
                return
            client = self.daemon
            if client is None:
                respond("daemon 未连接；本条命令未转发，也不会自动重试")
                return
            if (
                request.method == "alert.list"
                and not request.params
                and user not in self.config.admins
            ):
                results = []
                for profile in sorted(self.config.profiles(user)):
                    results.extend(
                        await self.access.execute(
                            user, Request("alert.list", {"profile": profile}), client
                        )
                    )
                respond(render_result(results))
                return
            if request.params.get("idempotency_key"):
                identifier = request.params["idempotency_key"]

            def dispatch():
                nonlocal claimed
                if isinstance(identifier, str) and identifier not in self.operations:
                    if len(self.operations) >= 4096:
                        raise ValueError("待完成操作过多，请稍后重试")
                    self.operations[identifier] = OperationRoute(user)
                    claimed = True

            result = await self.access.execute(user, request, client, dispatch=dispatch)
            respond(render_result(result))
        except (ConnectionError, TimeoutError):
            respond(
                "连接中断或请求超时，操作结果可能未知，不会自动重发"
                + (
                    f"；可通过 operation.find 按 idempotency_key={identifier} 找回"
                    if isinstance(identifier, str)
                    else ""
                ),
            )
        except (ValueError, RuntimeError, OSError) as error:
            if claimed:
                self.operations.pop(identifier, None)
            message = str(error).replace(self.config.secret, "[已隐藏]")
            respond("请求未完成：" + message)
        finally:
            if claimed and (route := self.operations.get(identifier)) is not None:
                route.acknowledged = True
                if route.terminal is not None:
                    self.operations.pop(identifier, None)
                    self.enqueue(user, render_operation(route.terminal))

    def enqueue(self, user: str, content: str, *, callback: dict | None = None) -> None:
        if self.wecom is None:
            return
        if self._messages.full():
            raise ConnectionError("企业微信输出积压")
        # Group commands and independent notifications must remain private.
        if callback is not None:
            body = callback.get("body", {})
            if body.get("chattype") != "single" or body.get("from", {}).get("userid") != user:
                callback = None
        self._messages.put_nowait((user, content, callback))

    async def _send_messages(self, connection: WeComConnection) -> None:
        while True:
            user, content, callback = await self._messages.get()
            try:
                if callback is not None:
                    await connection.reply(callback, content)
                else:
                    await connection.send(user, content)
            except WeComError as error:
                # A definitive platform rejection isn't repaired by repeating a game action.
                log.warning("企业微信消息被拒绝，错误码 %s", error.code)

    async def notification(self, notification: dict) -> None:
        kind = notification.get("kind")
        if kind in {"alert.raised", "alert.resolved"}:
            profile = notification.get("profile")
            recipients = (
                self.config.admins
                if profile == ADMIN_CHANNEL or not isinstance(profile, str)
                else self.config.routes.get(profile, frozenset())
            )
            for user in sorted(recipients):
                self.enqueue(user, render_alert(notification))
        elif kind == "operation.updated":
            operation = notification.get("data", {})
            if operation.get("status") not in TERMINAL:
                return
            identifier = operation.get("idempotency_key") or operation.get("operation_id")
            if route := self.operations.get(identifier):
                if route.acknowledged:
                    self.operations.pop(identifier, None)
                    self.enqueue(route.user, render_operation(operation))
                else:
                    route.terminal = operation

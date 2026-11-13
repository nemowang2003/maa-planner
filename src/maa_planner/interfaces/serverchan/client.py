"""Receive daemon notifications and POST each account's configured ServerChan3 URLs."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from maa_planner.client import ControlClient
from maa_planner.schema import MAX_PENDING_NOTIFICATIONS

from .config import ServerChanConfig

log = logging.getLogger(__name__)


def payload(notification: dict) -> dict:
    profile = notification["profile"]
    title = f"{profile} · {notification.get('message') or notification.get('kind') or '通知'}"
    return {
        "title": title,
        "desp": json.dumps(notification, ensure_ascii=False, indent=2, allow_nan=False),
    }


async def post(session: aiohttp.ClientSession, url: str, message: dict) -> None:
    async with session.post(url, json=message, allow_redirects=False) as response:
        if not 200 <= response.status < 300:
            raise ValueError(f"HTTP {response.status}")
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(8192):
            size += len(chunk)
            if size > 65536:
                raise ValueError("响应超过长度限制")
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            value = json.loads(raw)
        except ValueError:
            raise ValueError("响应不是 JSON") from None
        if not isinstance(value, dict) or type(value.get("code")) is not int or value["code"] != 0:
            raise ValueError("服务未确认成功")


class ServerChanClient:
    def __init__(self, config: ServerChanConfig, endpoint: Path):
        self.config, self.endpoint = config, endpoint
        urls = dict.fromkeys(url for urls in config.routes.values() for url in urls)
        self.queues = {url: asyncio.Queue(MAX_PENDING_NOTIFICATIONS) for url in urls}

    def receive(self, notification: dict) -> None:
        profile = notification.get("profile")
        if not isinstance(profile, str) or profile not in self.config.routes:
            return
        message = payload(notification)
        for url in self.config.routes[profile]:
            queue = self.queues[url]
            if queue.full():
                # An overloaded destination must not block other URLs or grow memory.
                log.warning("Server酱目的地积压，丢弃本条通知")
                continue
            queue.put_nowait(message)

    async def _deliver(self, session: aiohttp.ClientSession, url: str) -> None:
        while True:
            message = await self.queues[url].get()
            try:
                await post(session, url, message)
            except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
                # URL, exception text and remote response may all contain the SendKey.
                log.warning("Server酱投递未确认成功，不自动重试")

    async def _listen(self) -> None:
        delay = 1
        while True:
            try:
                async with ControlClient(self.endpoint) as client:
                    delay = 1
                    log.info("daemon 已连接，只接收通知")
                    while True:
                        self.receive(await client.receive())
            except (OSError, RuntimeError, ValueError, TimeoutError):
                log.warning("daemon 不可用，%s 秒后重新连接", delay)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    async def run(self, stop: asyncio.Event) -> None:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            tasks = [
                asyncio.create_task(self._listen()),
                asyncio.create_task(stop.wait()),
                *(asyncio.create_task(self._deliver(session, url)) for url in self.queues),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

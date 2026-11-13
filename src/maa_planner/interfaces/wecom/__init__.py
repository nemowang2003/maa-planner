"""Independent enterprise WeChat client for the public daemon API."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from maa_planner.endpoint import default_endpoint

from .bot import WeComBot
from .config import EXAMPLE, WeComConfig, default_config_file


async def serve(config: WeComConfig, endpoint: Path) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = [signal.SIGINT, signal.SIGTERM]
    if os.name == "nt":
        signals.append(signal.SIGBREAK)
    previous = {}
    for sig in signals:
        if os.name == "nt":
            previous[sig] = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        else:
            loop.add_signal_handler(sig, stop.set)
    try:
        await WeComBot(config, endpoint).run(stop)
    finally:
        for sig in signals:
            if os.name == "nt":
                signal.signal(sig, previous[sig])
            else:
                loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="maa-planner-wecom-client")
    parser.add_argument("--config", type=Path, default=default_config_file())
    parser.add_argument("--endpoint", type=Path, default=default_endpoint())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验配置，不连接企业微信或 daemon")
    mode.add_argument("--example", action="store_true", help="输出未启用的配置模板")
    args = parser.parse_args(argv)
    if args.example:
        print(EXAMPLE, end="")
        return
    try:
        config = WeComConfig.load(args.config)
        if args.check:
            print("企业微信配置有效；未建立任何连接")
            return
        logging.basicConfig(level=logging.INFO)
        asyncio.run(serve(config, args.endpoint))
    except (ValueError, OSError, RuntimeError) as error:
        # No traceback or raw remote frame; BotID/Secret and message text stay out of logs.
        parser.error(str(error))

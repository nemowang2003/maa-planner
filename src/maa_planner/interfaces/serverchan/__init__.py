"""Independent receive-only daemon client forwarding notifications to ServerChan3."""

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from maa_planner.endpoint import default_endpoint

from .client import ServerChanClient
from .config import EXAMPLE, ServerChanConfig, default_config_file


async def serve(config: ServerChanConfig, endpoint: Path) -> None:
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
        await ServerChanClient(config, endpoint).run(stop)
    finally:
        for sig in signals:
            if os.name == "nt":
                signal.signal(sig, previous[sig])
            else:
                loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="maa-planner-serverchan-client")
    parser.add_argument("--config", type=Path, default=default_config_file())
    parser.add_argument("--endpoint", type=Path, default=default_endpoint())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验配置，不连接 daemon 或发送推送")
    mode.add_argument("--example", action="store_true", help="输出配置模板，不建立连接")
    args = parser.parse_args(argv)
    if args.example:
        print(EXAMPLE, end="")
        return
    try:
        config = ServerChanConfig.load(args.config)
        if args.check:
            print("Server酱配置有效；未建立任何连接或发送推送")
            return
        logging.basicConfig(level=logging.INFO)
        asyncio.run(serve(config, args.endpoint))
    except ValueError as error:
        parser.error(str(error))
    except (OSError, RuntimeError):
        parser.error("Server酱客户端启动失败，请检查配置及本机端点；推送 URL 不写入日志")

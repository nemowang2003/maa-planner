"""maa-planner command dispatch. Importing schema does not load the daemon."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="maa-planner")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("daemon", add_help=False, help="运行和管理后台 daemon")
    commands.add_parser("wecom", add_help=False, help="运行独立企业微信客户端")
    commands.add_parser("serverchan", add_help=False, help="运行只接收通知的 Server酱客户端")
    args, remaining = parser.parse_known_args(argv)
    if args.command == "daemon":
        from .daemon import main as daemon_main

        daemon_main(remaining)

    elif args.command == "wecom":
        from .interfaces.wecom import main as wecom_main

        wecom_main(remaining)

    elif args.command == "serverchan":
        from .interfaces.serverchan import main as serverchan_main

        serverchan_main(remaining)

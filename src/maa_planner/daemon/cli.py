"""Daemon lifecycle and offline configuration helpers only."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from maa_planner.client import ControlClient
from maa_planner.daemon.config import default_config_file, example_config, load_settings
from maa_planner.endpoint import default_endpoint


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="maa-planner daemon")
    parser.add_argument("--config", type=Path, default=default_config_file())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "run", "status", "stop", "restart"):
        item = commands.add_parser(name)
        item.add_argument("--endpoint", type=Path, default=default_endpoint())
        if name in {"start", "run"}:
            from maa_planner.daemon.tools.locks import state_directory

            item.add_argument("--state-dir", type=Path, default=state_directory())
        if name in {"stop", "restart"}:
            item.add_argument(
                "--force", action="store_true", help="取消执行任务；维护仍等待安全完成"
            )
            item.add_argument(
                "--timeout", type=float, help="等待完成的秒数，默认持续等待；超时不撤销已接纳请求"
            )
    service = commands.add_parser("service", help="Windows 登录会话托管与自启管理")
    service.add_argument(
        "action", choices=("plan", "install", "remove", "status", "start", "stop", "restart", "run")
    )
    service.add_argument("--endpoint", type=Path, default=default_endpoint())
    from maa_planner.daemon.tools.locks import state_directory

    service.add_argument("--state-dir", type=Path, default=state_directory())
    service.add_argument("--force", action="store_true", help="停止／重启时取消执行，保留模拟器")
    service.add_argument("--timeout", type=float, help="停止／重启等待秒数")
    config = commands.add_parser("config", help="离线检查或展示配置，不启动 daemon")
    helpers = config.add_subparsers(dest="action", required=True)
    helpers.add_parser("example", help="输出最小配置；完整 expert 示例见 docs/example.toml")
    for name in ("validate", "dump"):
        helpers.add_parser(name)
    args = parser.parse_args(argv)
    try:
        if args.command == "config":
            if args.action == "example":
                print(example_config(), end="")
            else:
                settings = load_settings(args.config)
                if args.action == "validate":
                    print(f"配置有效: {settings.config_file}")
                else:
                    print(json.dumps(asdict(settings), ensure_ascii=False, indent=2, default=str))
            return
        from maa_planner.daemon.adapters.host import require_supported_host

        require_supported_host()
        if args.command == "service":
            from maa_planner.daemon.service import host, lifecycle, manage

            if args.action == "run":
                load_settings(args.config)
                asyncio.run(host(args))
            elif args.action in {"start", "stop", "restart"}:
                print(json.dumps(asyncio.run(lifecycle(args)), ensure_ascii=False))
            else:
                print(json.dumps(manage(args), ensure_ascii=False))
            return
        if args.command == "start":
            load_settings(args.config)
            print(json.dumps(asyncio.run(start(args)), ensure_ascii=False))
            return
        if args.command != "run":
            if args.command in {"stop", "restart"}:
                result = asyncio.run(
                    wait_lifecycle(
                        args.endpoint, args.command, force=args.force, timeout=args.timeout
                    )
                )
            else:
                result = asyncio.run(call(args.endpoint, "daemon.status"))
            print(
                json.dumps(
                    result,
                    ensure_ascii=False,
                )
            )
            return
        load_settings(args.config)
        asyncio.run(serve(args))
    except (OSError, RuntimeError, ValueError, TimeoutError) as error:
        parser.error(str(error))


async def call(path: Path, method: str, **params):
    async with ControlClient(path) as client:
        return await client.call(method, **params)


def process_identity(pid):
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


def process_alive(pid, created):
    import psutil

    if created is None:
        return False
    try:
        process = psutil.Process(pid)
        return (
            process.create_time() == created
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


async def wait_lifecycle(path: Path, command: str, *, force=False, timeout=None):
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout 必须为正秒数")
    async with asyncio.timeout(timeout):
        accepted = await call(path, "daemon." + command, force=force)
        pid, generation = accepted["pid"], accepted["started_at"]
        created = process_identity(pid)
        exited_at = None
        while True:
            try:
                current = await call(path, "daemon.status")
            except (OSError, RuntimeError, ValueError, TimeoutError):
                current = None
            alive = process_alive(pid, created)
            if command == "stop" and not alive:
                return {"pid": pid, "started_at": generation, "stopped": True}
            if command == "restart":
                if (
                    current
                    and current["started_at"] != generation
                    and current.get("configuration_revision") is not None
                    and not current["stopping"]
                ):
                    return {**current, "restarted": True, "previous_pid": pid}
                if (
                    current
                    and current["started_at"] == generation
                    and not current["draining"]
                    and not current["restart_requested"]
                    and not current["stopping"]
                ):
                    raise RuntimeError(
                        "重启已被拒绝："
                        + str(current.get("configuration_error") or "daemon 未进入重启状态")
                    )
                if not alive:
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at > 15:
                        raise RuntimeError("旧 daemon 已退出，但新实例未就绪")
            await asyncio.sleep(0.05)


async def start(args) -> dict:
    try:
        await call(args.endpoint, "daemon.status")
    except (OSError, RuntimeError, ValueError, TimeoutError):
        pass
    else:
        raise RuntimeError("daemon 已在运行")
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = args.state_dir / "daemon.log"
    with log_path.open("ab") as log:
        log_path.chmod(0o600)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "maa_planner.daemon",
                "--config",
                str(args.config.resolve()),
                "run",
                "--endpoint",
                str(args.endpoint.resolve()),
                "--state-dir",
                str(args.state_dir.resolve()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=os.name != "nt",
            creationflags=0x208 if os.name == "nt" else 0,
            close_fds=True,
        )
    try:
        async with asyncio.timeout(15):
            while process.poll() is None:
                try:
                    status = await call(args.endpoint, "daemon.status")
                    if (
                        status["pid"] == process.pid or _spawned_child(status["pid"], process.pid)
                    ) and status["configuration_revision"] is not None:
                        return {**status, "log": str(log_path)}
                except (OSError, RuntimeError, ValueError, TimeoutError):
                    pass
                await asyncio.sleep(0.05)
            raise RuntimeError(f"daemon 启动失败，退出码 {process.returncode}；日志：{log_path}")
    except BaseException as error:
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        if isinstance(error, TimeoutError):
            raise RuntimeError(f"daemon 就绪超时；日志：{log_path}") from error
        raise


def _spawned_child(pid: int, parent: int) -> bool:
    # Windows venv python.exe is a redirector; the actual interpreter has its
    # own PID. Verify ancestry rather than timing out after a healthy startup.
    if os.name != "nt":
        return False
    import psutil

    try:
        return any(process.pid == parent for process in psutil.Process(pid).parents())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


async def serve(args, *, managed=False) -> bool:
    import logging

    from maa_planner.daemon.coordinator import Daemon

    logging.basicConfig(level=logging.INFO)
    coordinator = Daemon(args.config.resolve(), args.endpoint, root=args.state_dir)
    coordinator.hosted = managed
    loop = asyncio.get_running_loop()

    def stop(sig):
        coordinator.restart_requested = False
        coordinator.request_stop(reason=signal.Signals(sig).name)

    signals = [signal.SIGTERM, signal.SIGINT]
    if os.name == "nt":
        signals.append(signal.SIGBREAK)
    for sig in signals:
        if os.name == "nt":
            signal.signal(sig, lambda number, _: loop.call_soon_threadsafe(stop, number))
        else:
            loop.add_signal_handler(sig, stop, sig)
    from contextlib import ExitStack

    with ExitStack() as controls:
        if os.name == "nt":
            from maa_planner.daemon.adapters.console_control import ConsoleControl

            def system_stop(reason):
                def request():
                    coordinator.restart_requested = False
                    coordinator.request_stop(reason=reason)

                loop.call_soon_threadsafe(request)

            controls.enter_context(ConsoleControl(system_stop))
        try:
            await coordinator.run()
        finally:
            for sig in signals:
                if os.name == "nt":
                    signal.signal(sig, signal.SIG_DFL)
                else:
                    loop.remove_signal_handler(sig)
    if managed:
        return coordinator.restart_requested
    if coordinator.restart_requested:
        # Replace this daemon process only after workers, maintenance, control service and SQL close.
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "maa_planner.daemon",
                "--config",
                str(args.config.resolve()),
                "run",
                "--endpoint",
                str(args.endpoint),
                "--state-dir",
                str(args.state_dir),
            ],
        )

    return False

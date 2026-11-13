"""Private worker admission protocol, separate from the versioned public API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path

from maa_planner.daemon.execution.context import RunRequest, RunResult
from maa_planner.daemon.execution.runner import run_core
from maa_planner.daemon.tools.storage import write_json

STOP_GRACE = 15


class WorkerControl:
    def __init__(self, connection: socket.socket | None, *, output=None, input_stream=None):
        self.connection = connection
        self.output = output or sys.stdout.buffer
        self.input_stream = input_stream
        self.stopped = threading.Event()
        self.admission = threading.Event()
        self.permitted = False
        self.granted_at = 0.0
        self.launched = False
        self.write_lock = threading.Lock()
        self.shop_reply = threading.Event()
        self.shop_status: str | None = None
        self.shop_query_id = 0
        self.orundum_reply = threading.Event()
        self.orundum_allowed = False
        self.orundum_query_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            stream = (
                self.connection.makefile("rb")
                if self.connection is not None
                else self.input_stream or os.fdopen(os.dup(0), "rb", buffering=0)
            )
            try:
                while line := stream.readline(65537):
                    if len(line) > 65536:
                        break
                    value = json.loads(line)
                    if value.get("type") == "admit":
                        self.permitted = bool(value["allowed"])
                        self.granted_at = time.monotonic()
                        self.admission.set()
                    elif value.get("type") == "cancel":
                        break
                    elif value.get("type") == "orundum_start":
                        if value.get("query_id") == self.orundum_query_id:
                            self.orundum_allowed = value.get("allowed") is True
                            self.orundum_reply.set()
                    elif value.get("type") == "shop_permission":
                        if value.get("query_id") == self.shop_query_id:
                            self.shop_status = value.get("status")
                            self.shop_reply.set()
            finally:
                stream.close()
        except (OSError, ValueError):
            pass
        self.stopped.set()
        self.admission.set()
        self.shop_reply.set()
        self.orundum_reply.set()

    def send(self, value: dict) -> None:
        try:
            with self.write_lock:
                encoded = json.dumps(value).encode() + b"\n"
                if self.connection is not None:
                    self.connection.sendall(encoded)
                else:
                    self.output.write(encoded)
                    self.output.flush()
        except OSError:
            self.stopped.set()
            self.admission.set()
            self.shop_reply.set()
            self.orundum_reply.set()

    def cancelled(self) -> bool:
        return self.stopped.is_set() or (
            self.permitted and not self.launched and time.monotonic() - self.granted_at > 5
        )

    def ready(self) -> bool:
        self.send({"type": "ready"})
        return self.admission.wait(30) and self.permitted and not self.cancelled()

    def report(self, phase: str) -> None:
        if phase == "launching":
            self.launched = True
        elif phase != "ready":
            self.send({"type": "phase", "phase": phase})

    def login(self, report: dict) -> None:
        self.send({"type": "login_success", "report": report})

    def clearance(self, report: dict) -> None:
        self.send({"type": "activity_copilot", "report": report})

    def shop(self, report: dict) -> None:
        self.send({"type": "activity_shop", "report": report})

    def orundum(self, report: dict) -> None:
        self.send({"type": "orundum_applied", "report": report})

    def orundum_start(self, query: dict) -> bool:
        self.orundum_allowed = False
        self.orundum_query_id += 1
        self.orundum_reply.clear()
        self.send({"type": "orundum_start", "query_id": self.orundum_query_id, "query": query})
        return self.orundum_reply.wait(30) and self.orundum_allowed and not self.stopped.is_set()

    def shop_permission(self, query: dict) -> str | None:
        self.shop_status = None
        self.shop_query_id += 1
        self.shop_reply.clear()
        self.send({"type": "shop_permission", "query_id": self.shop_query_id, "query": query})
        if self.shop_reply.wait(30) and not self.stopped.is_set():
            return self.shop_status
        return None


class WorkerClient:
    def __init__(self, request: RunRequest, root: Path):
        self.request = request
        self.root = root
        self.writer: asyncio.StreamWriter | None = None
        self.cancel_requested = False
        self.process: asyncio.subprocess.Process | None = None
        self.owned = False
        self.job = None
        self.identity: dict | None = None
        self.cancel_at: float | None = None
        self.login_callback: Callable[[dict], Awaitable[None]] | None = None
        self.clearance_callback: Callable[[dict], Awaitable[None]] | None = None
        self.shop_callback: Callable[[dict], Awaitable[None]] | None = None
        self.shop_permission_callback: Callable[[dict], Awaitable[str]] | None = None
        self.orundum_start_callback: Callable[[dict], Awaitable[bool]] | None = None
        self.orundum_callback: Callable[[dict], Awaitable[None]] | None = None

    async def cancel(self) -> None:
        self.cancel_requested = True
        if self.cancel_at is None:
            self.cancel_at = time.monotonic()
        if self.writer is not None and self.owned:
            self.writer.write(b'{"type":"cancel"}\n')
            try:
                await self.writer.drain()
            except ConnectionError:
                pass

    async def run(
        self, admit: Callable[[], Awaitable[bool]], phase: Callable[[str], Awaitable[None]]
    ) -> RunResult:
        record = self.root / "workers" / self.request.attempt_id
        record.mkdir(parents=True, exist_ok=False, mode=0o700)
        write_json(record / "request.json", self.request.to_dict())
        try:
            with (record / "worker.log").open("ab") as log:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "maa_planner.daemon.worker",
                    "--request",
                    str(record / "request.json"),
                    "--state-dir",
                    str(self.root),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=log,
                    limit=8 * 1024 * 1024,
                )
            reader, self.writer = self.process.stdout, self.process.stdin
            assert reader is not None and self.writer is not None
            if self.cancel_requested:
                await self.cancel()
            result = None
            deadline = time.monotonic() + self.request.options.startup_timeout + 120
            owned = False
            while True:
                if self.cancel_at is not None and time.monotonic() - self.cancel_at > STOP_GRACE:
                    raise TimeoutError("worker 取消后未退出，保留恢复隔离")
                if time.monotonic() > deadline:
                    if not self.cancel_requested:
                        await self.cancel()
                try:
                    line = await asyncio.wait_for(reader.readline(), 1)
                except TimeoutError:
                    if self.process.returncode is not None:
                        break
                    continue
                if not line:
                    break
                value = json.loads(line)
                if value["type"] == "identity":
                    if owned:
                        raise ValueError("worker 重复报告身份")
                    import psutil

                    actual = psutil.Process(value["pid"])
                    if abs(actual.create_time() - value["created"]) >= 0.01 or (
                        actual.pid != self.process.pid
                        and not any(p.pid == self.process.pid for p in actual.parents())
                    ):
                        raise ValueError("worker 身份不属于本次启动")
                    self.identity = {"pid": actual.pid, "created": actual.create_time()}
                    write_json(record / "identity.json", self.identity)
                    if os.name == "nt":
                        from maa_planner.daemon.adapters.windows import ProcessJob

                        self.job = ProcessJob(actual.pid)
                    self.writer.write(b'{"type":"owned"}\n')
                    await self.writer.drain()
                    owned = self.owned = True
                    if self.cancel_requested:
                        await self.cancel()
                elif not owned:
                    raise ValueError("worker 未获得进程所有权")
                elif value["type"] == "ready":
                    allowed = not self.cancel_requested and await admit()
                    self.writer.write(
                        json.dumps({"type": "admit", "allowed": allowed}).encode() + b"\n"
                    )
                    await self.writer.drain()
                elif value["type"] == "phase":
                    if value["phase"] == "running":
                        deadline = time.monotonic() + self.request.options.execution_timeout + 10
                    await phase(value["phase"])
                elif value["type"] == "login_success":
                    if self.login_callback is not None:
                        await self.login_callback(value["report"])
                elif value["type"] == "activity_copilot":
                    if self.clearance_callback is not None:
                        await self.clearance_callback(value["report"])
                elif value["type"] == "activity_shop":
                    if self.shop_callback is not None:
                        await self.shop_callback(value["report"])
                elif value["type"] == "shop_permission":
                    status = (
                        await self.shop_permission_callback(value["query"])
                        if self.shop_permission_callback is not None
                        else None
                    )
                    self.writer.write(
                        json.dumps(
                            {
                                "type": "shop_permission",
                                "query_id": value["query_id"],
                                "status": status,
                            }
                        ).encode()
                        + b"\n"
                    )
                    await self.writer.drain()
                elif value["type"] == "orundum_applied":
                    if self.orundum_callback is not None:
                        await self.orundum_callback(value["report"])
                elif value["type"] == "orundum_start":
                    allowed = (
                        await self.orundum_start_callback(value["query"])
                        if self.orundum_start_callback is not None
                        else False
                    )
                    self.writer.write(
                        json.dumps(
                            {
                                "type": "orundum_start",
                                "query_id": value["query_id"],
                                "allowed": allowed,
                            }
                        ).encode()
                        + b"\n"
                    )
                    await self.writer.drain()
                elif value["type"] == "result":
                    result = RunResult.from_dict(value["result"])
                    if result.attempt_id != self.request.attempt_id:
                        raise ValueError("worker 返回了其他 attempt 的结果")
            code = await asyncio.wait_for(self.process.wait(), 30)
            if result is None or code != 0:
                return RunResult(
                    self.request.attempt_id, "recovery_required", f"worker 异常退出: {code}"
                )
            return result
        except (OSError, ValueError, TimeoutError) as error:
            return RunResult(self.request.attempt_id, "recovery_required", str(error))
        finally:
            if self.writer is not None:
                self.writer.close()
                try:
                    await self.writer.wait_closed()
                except (OSError, ConnectionError):
                    pass
            if self.process is not None and self.process.returncode is None:
                # EOF requests cooperative Core stop and emulator cleanup.
                try:
                    remaining = STOP_GRACE
                    if self.cancel_at is not None:
                        remaining = max(0, STOP_GRACE - (time.monotonic() - self.cancel_at))
                    await asyncio.wait_for(self.process.wait(), remaining)
                except TimeoutError:
                    if self.job is not None:
                        self.job.close()
                    if self.process.returncode is None:
                        try:
                            self.process.terminate()
                        except ProcessLookupError:
                            pass
                    await asyncio.wait_for(self.process.wait(), 5)
            if self.job is not None:
                self.job.close()
            if self.identity is not None:
                import psutil

                try:
                    actual = psutil.Process(self.identity["pid"])
                    if abs(actual.create_time() - self.identity["created"]) < 0.01:
                        await asyncio.to_thread(actual.wait, timeout=5)
                except psutil.NoSuchProcess:
                    pass
            (record / "request.json").unlink(missing_ok=True)


def main() -> None:
    from maa_planner.daemon.adapters.host import require_supported_host

    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    if args.probe:
        from maa_planner.daemon.execution.core import CoreLibrary

        print(json.dumps({"core_version": CoreLibrary(args.probe).version()}))
        return
    require_supported_host()
    if args.request is None or args.state_dir is None:
        parser.error("需要 --request 和 --state-dir")
    import psutil

    identity = {"type": "identity", "pid": os.getpid(), "created": psutil.Process().create_time()}
    sys.stdout.buffer.write(json.dumps(identity).encode() + b"\n")
    sys.stdout.buffer.flush()
    input_stream = os.fdopen(os.dup(0), "rb", buffering=0)
    if json.loads(input_stream.readline()) != {"type": "owned"}:
        raise RuntimeError("worker 未获得进程所有权许可")
    request = RunRequest.from_dict(json.loads(args.request.read_text(encoding="utf-8")))
    # Keep the protocol pipe private. Native Core/third-party stdout goes to
    # worker.log rather than corrupting the JSONL stream.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    # Native subprocesses must not inherit/read the private control channel.
    # On Windows even inspecting an inherited synchronous stdin handle can
    # block behind the control reader's pending ReadFile.
    with open(os.devnull, "rb") as null_input:
        os.dup2(null_input.fileno(), sys.stdin.fileno())
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        # Also redirect Win32's standard handle, used by native DLLs and
        # inherited by subprocesses outside Python's CRT descriptor table.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        kernel.SetStdHandle.restype = wintypes.BOOL
        for identifier, descriptor in (
            (0xFFFFFFF5, sys.stdout.fileno()),
            (0xFFFFFFF6, sys.stdin.fileno()),
        ):
            if not kernel.SetStdHandle(identifier, msvcrt.get_osfhandle(descriptor)):
                raise ctypes.WinError(ctypes.get_last_error())
    control = WorkerControl(None, output=output, input_stream=input_stream)
    result = run_core(request, control=control, recovery_root=args.state_dir / "recovery")
    control.send({"type": "result", "result": asdict(result)})


if __name__ == "__main__":
    main()

"""Resolve named MuMu instances and manage Android/ADB lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from maa_planner.daemon.config import Profile


@dataclass(frozen=True)
class Instance:
    index: int
    name: str
    running: bool
    address: str | None


def match_profiles(
    profiles: tuple[Profile, ...], instances: list[Instance]
) -> dict[Profile, Instance]:
    result = {}
    for profile in profiles:
        name = profile.emulator_name or profile.name
        matches = [
            instance for instance in instances if instance.name.casefold() == name.casefold()
        ]
        if len(matches) != 1:
            raise ValueError(
                f"{profile.name}: 需要唯一的 MuMu 实例 {name!r}，实际找到 {len(matches)} 个"
            )
        result[profile] = matches[0]
    if len({value.index for value in result.values()}) != len(result):
        raise ValueError("多个 MAA profile 不应指向同一个 MuMu 实例")
    return result


class MuMu:
    def __init__(self, root: Path):
        self.root = root
        self.manager = root / "nx_main" / "MuMuManager.exe"
        self.adb_path = root / "nx_main" / "adb.exe"
        if not self.manager.is_file() or not self.adb_path.is_file():
            raise FileNotFoundError(f"找不到 MuMuManager.exe 或 adb.exe: {root}")

    def command(self, *args: str) -> str:
        result = subprocess.run(
            [str(self.manager), *args],
            creationflags=0x01000000 if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=60,
            check=True,
        )
        return result.stdout.strip()

    def instances(self) -> list[Instance]:
        raw = json.loads(self.command("info", "-v", "all"))
        if not isinstance(raw, dict):
            raise ValueError("MuMu info 没有返回实例映射")
        instances = []
        for index, value in raw.items():
            if value.get("error_code", 0):
                raise RuntimeError(f"MuMu 实例 {index}: {value}")
            host, port = value.get("adb_host_ip"), value.get("adb_port")
            instances.append(
                Instance(
                    int(index),
                    value["name"],
                    bool(value.get("is_process_started")),
                    f"{host}:{int(port)}" if host and port else None,
                )
            )
        return instances

    def adb(self, address: str, *args: str, timeout: int = 60) -> str:
        result = subprocess.run(
            [str(self.adb_path), "-s", address, *args],
            creationflags=0x01000000 if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=True,
        )
        return result.stdout.strip()

    def connect(self, address: str) -> None:
        subprocess.run(
            [str(self.adb_path), "connect", address],
            creationflags=0x01000000 if os.name == "nt" else 0,
            capture_output=True,
            timeout=10,
            check=True,
        )

    @contextmanager
    def running(self, instance: Instance) -> Generator[Instance]:
        """Only shut down instances started by this operation."""
        current = next(i for i in self.instances() if i.index == instance.index)
        if current.name != instance.name:
            raise RuntimeError(f"MuMu 实例被重命名，请重新匹配: {instance.name} -> {current.name}")
        instance = current
        launched = not instance.running
        try:
            if launched:
                self.command("control", "-v", str(instance.index), "launch")
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                current = next(i for i in self.instances() if i.index == instance.index)
                if current.address:
                    self.connect(current.address)
                    try:
                        booted = (
                            self.adb(current.address, "shell", "getprop", "sys.boot_completed")
                            == "1"
                        )
                    except subprocess.CalledProcessError:
                        booted = False
                    if booted:
                        yield current
                        return
                time.sleep(3)
            raise TimeoutError(f"MuMu {instance.name}: 等待 Android/ADB 就绪超时")
        finally:
            if launched:
                self.command("control", "-v", str(instance.index), "shutdown")

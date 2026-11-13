"""Structured execution logs; SQLite never owns terminal execution history."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from maa_planner.daemon.tools.storage import sync_directory


class HistoryLog:
    """One writer, durable append, with a rebuildable in-memory offset index."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.failed = False
        self.index: dict[str, dict[str, int]] = {}
        created = not path.exists()
        with path.open("a+b") as stream:
            path.chmod(0o600)
            stream.seek(0)
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    # A killed writer can leave an incomplete final record.
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    break
                record = json.loads(line)
                self.index.setdefault(record["kind"], {})[record["key"]] = offset
            if created:
                stream.flush()
                os.fsync(stream.fileno())
                sync_directory(path.parent)
                sync_directory(path.parent.parent)
        self.sequence = max((int(k) for k in self.index.get("event", {})), default=0)

    def save(self, kind: str, key: str, value: dict) -> None:
        record = {"kind": kind, "key": key, "logged_at": time.time(), "value": value}
        content = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if self.failed:
            raise OSError("历史日志写入状态不确定，需要重新打开日志")
        with self.path.open("ab", buffering=0) as stream:
            offset = stream.tell()
            try:
                if stream.write(content) != len(content):
                    raise OSError("历史日志未完整写入")
                os.fsync(stream.fileno())
            except BaseException:
                try:
                    stream.truncate(offset)
                    os.fsync(stream.fileno())
                except OSError:
                    self.failed = True
                raise
        self.index.setdefault(kind, {})[key] = offset

    def get(self, kind: str, key: str) -> dict | None:
        offset = self.index.get(kind, {}).get(key)
        if offset is None:
            return None
        with self.path.open("rb") as stream:
            stream.seek(offset)
            return json.loads(stream.readline())["value"]

    def values(self, kind: str) -> list[dict]:
        return [
            value for key in self.index.get(kind, {}) if (value := self.get(kind, key)) is not None
        ]

    def event(self, kind: str, data: dict, *, sequence: int | None = None) -> None:
        number = self.sequence + 1 if sequence is None else sequence
        self.save("event", str(number), {"sequence": number, "kind": kind, "data": data})
        self.sequence = max(self.sequence, number)

    def events(self, after: int, limit: int) -> list[dict]:
        keys = sorted(int(k) for k in self.index.get("event", {}) if int(k) > after)[:limit]
        return [value for key in keys if (value := self.get("event", str(key))) is not None]

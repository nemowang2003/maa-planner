"""Native OS directory events, including editors' atomic file replacements."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


class ConfigWatch(FileSystemEventHandler):
    def __init__(self, path: Path, changed: Callable[[], None]):
        self.path = path.absolute()
        self.changed = changed
        self.loop = asyncio.get_running_loop()
        self.observer = Observer()
        self.timer: asyncio.TimerHandle | None = None
        self.closed = False

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type not in {
            "created",
            "modified",
            "closed",
            "moved",
            "deleted",
            "attrib",
        }:
            return
        paths = (event.src_path, event.dest_path)
        if any(value and Path(os.fsdecode(value)).absolute() == self.path for value in paths):
            self.loop.call_soon_threadsafe(self._notify)

    def _notify(self) -> None:
        if self.closed:
            return
        if self.timer is not None:
            self.timer.cancel()
        self.timer = self.loop.call_later(0.05, self.changed)

    def start(self) -> None:
        # Watch the parent rather than the file inode replaced by most editors.
        self.observer.schedule(self, str(self.path.parent), recursive=False)
        self.observer.start()

    async def close(self) -> None:
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()
        self.observer.stop()
        if self.observer.ident is not None:
            await asyncio.to_thread(self.observer.join)

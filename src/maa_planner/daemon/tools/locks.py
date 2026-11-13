"""Process-owned locks whose identity does not depend on a config file."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_state_path

from maa_planner.daemon.adapters.windows import process_key, windows_path


class BusyError(RuntimeError):
    pass


def state_directory() -> Path:
    if root := os.environ.get("XDG_STATE_HOME"):
        return Path(root) / "maa-planner" / "daemon"
    return user_state_path("maa-planner", appauthor=False) / "daemon"


def identity(path: Path) -> str:
    return process_key(windows_path(path.resolve()))


@dataclass(frozen=True)
class Resource:
    key: str
    shared: bool = False


def maa_resource(path: Path) -> Resource:
    return Resource("maa:" + identity(path))


def emulator_resource(root: Path, index: int) -> Resource:
    return Resource(f"emulator:{identity(root)}:{index}")


def data_resource(path: Path, *, shared: bool = False) -> Resource:
    return Resource("resource:" + identity(path), shared)


@contextmanager
def lock_file(path: Path, *, shared: bool = False) -> Generator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            from maa_planner.daemon.adapters.windows import lock_handle

            try:
                lock_handle(stream, shared=shared)
            except OSError as error:
                raise BusyError(f"资源正被占用: {path.name}") from error
            try:
                yield
            finally:
                lock_handle(stream, shared=shared, unlock=True)
        else:
            import fcntl

            operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            try:
                fcntl.flock(stream.fileno(), operation | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BusyError(f"资源正被占用: {path.name}") from error
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def acquire(resources: Iterable[Resource], *, root: Path | None = None) -> Generator[None]:
    from contextlib import ExitStack

    # The root is user-wide, including when a daemon uses another database directory.
    folder = root or state_directory().parent / "locks"
    modes: dict[str, bool] = {}
    for resource in resources:
        modes[resource.key] = modes.get(resource.key, True) and resource.shared
    with ExitStack() as stack:
        for key, shared in sorted(modes.items()):
            name = hashlib.sha256(key.encode()).hexdigest() + ".lock"
            stack.enter_context(lock_file(folder / name, shared=shared))
        yield

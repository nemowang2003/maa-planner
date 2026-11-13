import subprocess
import sys

import pytest

from maa_planner.daemon.tools.locks import BusyError, Resource, acquire, lock_file


def test_shared_readers_and_exclusive_writer_across_processes(tmp_path):
    script = """
import sys
from pathlib import Path
from maa_planner.daemon.tools.locks import Resource,acquire,BusyError
try:
    with acquire([Resource('same-resource', sys.argv[2]=='read')],root=Path(sys.argv[1])):
        pass
except BusyError:
    sys.exit(17)
"""

    def child(mode):
        return subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), mode], check=False
        ).returncode

    with acquire([Resource("same-resource", shared=True)], root=tmp_path):
        assert child("read") == 0
        assert child("write") == 17
    assert child("write") == 0


def test_shared_dependencies_allow_parallel_readers_and_exclude_writers(tmp_path):
    path = tmp_path / "resource.lock"
    with lock_file(path, shared=True), lock_file(path, shared=True):
        with pytest.raises(BusyError):
            with lock_file(path):
                pytest.fail("writer acquired a locked resource")
    with lock_file(path):
        with pytest.raises(BusyError):
            with lock_file(path, shared=True):
                pytest.fail("reader acquired a writing resource")

import asyncio
import os
import subprocess
import sys
import time

import pytest

from maa_planner.daemon.cli import wait_lifecycle
from maa_planner.daemon.state import StateStore
from tests.helpers.control import ApiPeer


def test_real_foreground_daemon_restarts_in_place_and_stops(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[[profiles]]\nname="nemo"\n', newline="\n")
    sock = tmp_path / "runtime/control.json"
    with (tmp_path / "daemon.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "maa_planner.daemon",
                "--config",
                str(config),
                "run",
                "--endpoint",
                str(sock),
                "--state-dir",
                str(tmp_path / "state"),
            ],
            stdout=log,
            stderr=log,
            env={
                **os.environ,
                "XDG_STATE_HOME": str(tmp_path / "user"),
                "LOCALAPPDATA": str(tmp_path / "user"),
            },
        )

    async def scenario():
        client = ApiPeer(sock)
        async with asyncio.timeout(15):
            while True:
                try:
                    first = await client.call("daemon.status")
                    break
                except OSError:
                    await asyncio.sleep(0.03)
        state = await wait_lifecycle(sock, "restart", timeout=15)
        assert state["pid"] > 0 and state["started_at"] != first["started_at"]
        assert state["restarted"]
        stopped = await wait_lifecycle(sock, "stop", timeout=15)
        assert stopped["stopped"] and not sock.exists()

    try:
        asyncio.run(scenario())
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_actual_daemon_cli_starts_empty_and_handles_sigterm(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
    sock = tmp_path / "runtime/control.json"
    with (tmp_path / "daemon.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "maa_planner.daemon",
                "--config",
                str(config),
                "run",
                "--state-dir",
                str(tmp_path / "state"),
                "--endpoint",
                str(sock),
            ],
            stdout=log,
            stderr=log,
            env={
                **os.environ,
                "XDG_STATE_HOME": str(tmp_path / "isolated-user-state"),
                "LOCALAPPDATA": str(tmp_path / "isolated-user-state"),
            },
        )
        try:
            from tests.helpers.control import send_message

            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None
                try:
                    result = asyncio.run(send_message(sock, {}))
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        pytest.fail("daemon did not publish endpoint")
                    time.sleep(0.02)
            assert "error" in result
            from tests.helpers.control import ApiPeer

            asyncio.run(ApiPeer(sock).call("daemon.stop"))
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert not sock.exists()
    state = StateStore(tmp_path / "state/state.sqlite3")
    assert state.results() == []
    state.close()

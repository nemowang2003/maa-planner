"""Exercise the real detached process without touching emulators or game data."""

import json
import os
import subprocess
import sys
import time

import psutil


def test_start_returns_after_readiness_and_background_daemon_restarts(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[[profiles]]\nname="nemo"\n')
    endpoint = tmp_path / "endpoint/control.json"
    state = tmp_path / "state"
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "user-state"),
        "LOCALAPPDATA": str(tmp_path / "user-state"),
    }
    command = [
        sys.executable,
        "-c",
        "from maa_planner import main; main()",
        "daemon",
        "--config",
        str(config),
    ]

    def call(action, *extra, check=True):
        return subprocess.run(
            command + [action, "--endpoint", str(endpoint), *extra],
            env=env,
            capture_output=True,
            text=True,
            check=check,
            timeout=20,
        )

    daemon_process = None
    try:
        first = json.loads(call("start", "--state-dir", str(state)).stdout)
        pid = first["pid"]
        daemon_process = psutil.Process(pid)
        assert pid != os.getpid()
        assert json.loads(call("status").stdout)["pid"] == pid
        assert (state / "daemon.log").is_file()
        duplicate = call("start", "--state-dir", str(state), check=False)
        assert duplicate.returncode != 0 and "已在运行" in duplicate.stderr
        other = subprocess.run(
            command
            + [
                "start",
                "--endpoint",
                str(tmp_path / "other/control.json"),
                "--state-dir",
                str(tmp_path / "other-state"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert other.returncode != 0 and "启动失败" in other.stderr
        for force in ([], ["--force"]):
            old = json.loads(call("status").stdout)
            call("restart", *force)
            deadline = time.monotonic() + 8
            while True:
                response = call("status", check=False)
                if response.returncode == 0:
                    new = json.loads(response.stdout)
                    if new["started_at"] > old["started_at"]:
                        pid = new["pid"]
                        daemon_process = psutil.Process(pid)
                        break
                assert time.monotonic() < deadline
                time.sleep(0.05)
        call("stop")
        daemon_process.wait(timeout=8)
    finally:
        if daemon_process is not None and daemon_process.is_running():
            daemon_process.terminate()
            daemon_process.wait(timeout=8)

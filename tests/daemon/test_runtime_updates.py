import asyncio
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from maa_planner.daemon import coordinator
from maa_planner.daemon.config import Settings
from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.execution.runtime import NAV_FILES, CoreRuntime
from maa_planner.daemon.state import StateStore
from maa_planner.daemon.tools import runtime_install as updates
from tests.helpers.execution import request


def runtime_at(root: Path, cli: str, core: str):
    for folder in ("bin", "data/lib", "data/resource", "navigation"):
        (root / folder).mkdir(parents=True)
    runtime = CoreRuntime(
        root / "bin" / Path(sys.executable).name,
        root / "data",
        root / "navigation",
        root.parent / "runs",
    )
    runtime.executable.write_text(cli)
    runtime.core_path.write_text(core)
    (root / "data/resource/version.json").write_text("{}")
    for name in NAV_FILES:
        path = root / "navigation" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    return runtime


@pytest.fixture
def latest_environment(tmp_path, monkeypatch):
    current = runtime_at(tmp_path / "installed", "1.2.3", "7.8.9")
    settings = Settings(
        tmp_path / "config.toml",
        (),
        core_runtime=current,
        runtime_directory=tmp_path / "versions",
        shared_resource_dir=current.resources,
    )
    latest = {"core": "7.8.9"}
    calls, prepared = [], []

    def release(url):
        assert url.endswith("/releases/latest")
        calls.append(url)
        version = latest["core"] if "/maa-cli/" in url else latest["core"]
        return {"tag_name": "v" + version, "assets": [], "draft": False, "prerelease": False}

    def probe(runtime):
        return f"MaaCore v{runtime.core_path.read_text()}"

    def prepare(destination, *, releases, **kwargs):
        prepared.append(destination)
        return runtime_at(destination, "python", releases.core_version)

    monkeypatch.setattr(updates, "_json", release)
    monkeypatch.setattr(updates, "probe_runtime", probe)
    monkeypatch.setattr(updates, "prepare_runtime", prepare)
    return settings, latest, calls, prepared


def test_each_execution_rechecks_both_releases_and_reuses_verified_files(latest_environment):
    settings, latest, calls, prepared = latest_environment
    first = updates.ensure_latest_runtime(settings)
    assert first.executable != settings.core_runtime.executable
    assert len(calls) == 1 and len(prepared) == 1
    latest.update(core="8.9.0")
    second = updates.ensure_latest_runtime(settings)
    assert len(calls) == 2 and len(prepared) == 2
    assert updates.runtime_versions(second) == "8.9.0"
    assert second.resources == settings.core_runtime.resources
    assert settings.core_runtime.core_path.read_text() == "7.8.9"  # Existing users are untouched.
    third = updates.ensure_latest_runtime(settings)
    assert third.core_path == second.core_path
    assert len(calls) == 3 and len(prepared) == 2


def test_network_failure_never_falls_back_to_cached_or_old_runtime(latest_environment, monkeypatch):
    settings, latest, _, _ = latest_environment
    latest["core"] = "8.0.0"
    updates.ensure_latest_runtime(settings)

    def failed(url):
        raise OSError("release service unavailable")

    monkeypatch.setattr(updates, "_json", failed)
    with pytest.raises(OSError, match="unavailable"):
        updates.ensure_latest_runtime(settings)


def test_corrupt_cached_runtime_is_not_trusted_by_directory_name(latest_environment):
    settings, latest, _, _ = latest_environment
    latest["core"] = "8.0.0"
    selected = updates.ensure_latest_runtime(settings)
    selected.core_path.write_text("wrong-version")
    with pytest.raises(ValueError, match="实际版本"):
        updates.ensure_latest_runtime(settings)


def test_latest_endpoint_must_describe_a_stable_valid_release(monkeypatch):
    monkeypatch.setattr(
        updates, "_json", lambda url: {"tag_name": "v1.2.3", "assets": [], "prerelease": True}
    )
    with pytest.raises(ValueError, match="稳定发布"):
        updates.latest_releases("https://example.test")


def test_daemon_coalesces_only_overlapping_checks(tmp_path, monkeypatch):
    req = request(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3")
    state.configuration(req.config_revision, b"snapshot")
    settings = Settings(tmp_path / "config.toml", (), core_runtime=req.core.runtime)
    monkeypatch.setattr(coordinator, "settings_from_bytes", lambda *args: settings)
    started, release = threading.Event(), threading.Event()
    calls = []

    def ensure(settings):
        calls.append(settings)
        started.set()
        assert release.wait(3)
        return settings.core_runtime

    from maa_planner.daemon.preparation import Preparation, PreparedEnvironment
    from tests.helpers.activity import empty_activity

    monkeypatch.setattr(
        coordinator,
        "prepare_environment",
        lambda settings: PreparedEnvironment(ensure(settings), empty_activity()),
    )

    async def scenario():
        daemon = Daemon(settings.config_file, tmp_path / "socket/control.json")
        daemon.store = state
        try:
            daemon._preparations[req.attempt_id] = Preparation(settings)
            first = asyncio.create_task(daemon.latest_runtime(req))
            second = asyncio.create_task(daemon.latest_runtime(req))
            async with asyncio.timeout(3):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            release.set()
            assert (await first) == (await second) == req.core.runtime
            assert len(calls) == 1
            await daemon.latest_runtime(req)
            assert len(calls) == 1  # Same explicit preparation unit stays pinned.
            daemon._preparations.clear()
            await daemon.latest_runtime(req)
            assert len(calls) == 2
        finally:
            release.set()
            state.close()

    asyncio.run(scenario())


def test_prepared_request_preserves_original_deduplication_payload(tmp_path):
    req = request(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3")
    try:
        state.enqueue(req)
        state.start(req.attempt_id)
        updated = replace(
            req,
            core=replace(
                req.core, runtime=replace(req.core.runtime, expected_core_version="9.8.7")
            ),
        )
        state.prepared_request(updated)
        assert not state.enqueue(req)
        payload = state.db.execute("SELECT payload FROM prepared_executions").fetchone()[0]
        assert "9.8.7" in payload
    finally:
        state.close()


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("latest", "succeeded"),
        ("offline", "failed"),
        ("cancel", "cancelled"),
        ("expired", "failed"),
    ],
)
def test_daemon_checks_latest_before_creating_worker_and_records_resolved_snapshot(
    tmp_path, monkeypatch, mode, expected
):
    import time

    from maa_planner.daemon.coordinator import PendingExecution
    from maa_planner.daemon.execution.context import RunResult

    req = request(tmp_path)
    selected = replace(req.core.runtime, expected_core_version="8.9.0")
    created = []
    state = StateStore(tmp_path / "state.sqlite3")
    state.enqueue(req)
    state.start(req.attempt_id)
    payload = {"action": "run", "target": "nemo", "arguments": {}}
    state.reserve_operation("manual", payload)
    state.reserve_copilot("manual", payload, req.attempt_id)

    class Worker:
        def __init__(self, fixed_request, root):
            created.append(fixed_request)
            self.request = fixed_request

        async def run(self, admit, phase):
            return RunResult(self.request.attempt_id, "succeeded")

    async def scenario():
        daemon = Daemon(
            tmp_path / "config.toml", tmp_path / "socket/control.json", worker_factory=Worker
        )
        daemon.store = state
        daemon._immediate[req.attempt_id] = "manual"
        daemon._deadlines["manual"] = time.time() + 60
        pending = PendingExecution(req)

        async def latest(request):
            assert not created
            if mode == "offline":
                raise OSError("cannot determine latest release")
            if mode == "cancel":
                await pending.cancel()
            if mode == "expired":
                daemon._deadlines["manual"] = time.time() - 1
            return selected

        monkeypatch.setattr(daemon, "latest_runtime", latest)
        await daemon._execute(req, pending)
        await daemon._events()
        assert state.results()[0].status == expected
        if mode == "latest":
            assert created[0].core.runtime == selected
            prepared = state.prepared_execution(req.attempt_id)
            assert prepared is not None
            fixed = prepared.to_dict()
            assert state.db.execute("SELECT COUNT(*) FROM prepared_executions").fetchone()[0] == 0
            assert fixed["core"]["runtime"]["expected_core_version"] == "8.9.0"
            assert daemon.operations.get("manual")["runtime"]["expected_core_version"] == "8.9.0"
        else:
            assert not created
            assert state.db.execute("SELECT COUNT(*) FROM prepared_executions").fetchone()[0] == 0

    try:
        asyncio.run(scenario())
    finally:
        state.close()

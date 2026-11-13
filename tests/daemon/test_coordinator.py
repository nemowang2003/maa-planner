from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from maa_planner.daemon import coordinator as module
from maa_planner.daemon.config import Settings
from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.copilot_command import CopilotCommand
from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.policy import Decision, EmptyPolicy
from maa_planner.daemon.state import StateStore
from maa_planner.schema import Request
from tests.helpers.async_tools import eventually
from tests.helpers.control import send_message as wire_message
from tests.helpers.execution import request

prepare_dependencies_impl = Daemon.prepare_dependencies


async def call(path, method, **params):
    value = await wire_message(path, Request(method, params).to_dict())
    return value.get("result", {"error": value.get("error", {}).get("message", "unknown")})


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    async def latest_runtime(self, request):
        assert request.core is not None
        return request.core.runtime

    from maa_planner.daemon.preparation import PreparedEnvironment
    from tests.helpers.activity import empty_activity

    async def prepare_dependencies(self, settings, *, pinned=None):
        return PreparedEnvironment(settings.runtime_template(), empty_activity())

    monkeypatch.setattr(Daemon, "prepare_dependencies", prepare_dependencies)
    monkeypatch.setattr(Daemon, "latest_runtime", latest_runtime)
    monkeypatch.setenv("WSL_DISTRO_NAME", "NixOS")
    monkeypatch.setattr(module, "state_directory", lambda: tmp_path / "user/daemon")


def test_empty_daemon_reloads_atomic_replacements_and_retains_last_valid(tmp_path):
    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        daemon = Daemon(config, tmp_path / "runtime/control.json", root=tmp_path / "state")
        task = asyncio.create_task(daemon.run())
        try:
            await eventually(lambda: daemon.configuration is not None)
            first = daemon.configuration
            candidate = tmp_path / "next.toml"
            candidate.write_text('[[profiles]]\nname="nemo"\n# revision 2')
            candidate.replace(config)
            await eventually(lambda: daemon.configuration != first)
            second = daemon.configuration
            config.write_text("[invalid")
            await eventually(lambda: daemon.config_error is not None)
            assert daemon.configuration == second
            assert not daemon.running
        finally:
            daemon.request_stop()
            await asyncio.wait_for(task, 5)
        assert not (tmp_path / "runtime/control.json").exists()

    asyncio.run(scenario())


class OncePolicy(EmptyPolicy):
    def __init__(self, requests):
        self.requests = requests
        self.bound = None

    def evaluate(self, context):
        if self.bound is None:
            self.bound = tuple(
                replace(r, config_revision=context.configuration.revision) for r in self.requests
            )
        return Decision(self.bound)

    def admit(self, request, context):
        return True


class FakeWorker:
    def __init__(self, request, root, gate, collection):
        self.request = request
        self.gate = gate
        self.cancelled = False
        self.allowed = None
        collection.append(self)

    async def cancel(self):
        self.cancelled = True
        self.gate.set()

    async def run(self, admit, phase):
        await self.gate.wait()
        self.allowed = await admit()
        if self.allowed:
            await phase("running")
        return RunResult(self.request.attempt_id, "succeeded" if self.allowed else "cancelled")


def test_reload_during_preparation_denies_stale_runner(tmp_path):
    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        req = request(tmp_path)
        gate = asyncio.Event()
        workers = []
        daemon = Daemon(
            config,
            tmp_path / "runtime/control.json",
            root=tmp_path / "state",
            policy=OncePolicy([req]),
            worker_factory=lambda r, p: FakeWorker(r, p, gate, workers),
        )
        task = asyncio.create_task(daemon.run())
        try:
            await eventually(lambda: bool(workers))
            assert daemon.configuration is not None
            first_revision = daemon.configuration.revision
            config.write_text('[[profiles]]\nname="nemo"\n# revision 2')
            await eventually(
                lambda: (
                    daemon.configuration is not None
                    and daemon.configuration.revision != first_revision
                )
            )
            gate.set()
            await eventually(lambda: workers[0].allowed is not None)
            assert workers[0].allowed is False
            await eventually(lambda: not daemon.running)
        finally:
            daemon.request_stop()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_shared_preparation_alerts_distinguish_activity_source(tmp_path):
    from maa_planner.daemon.tools.hot_update import ActivitySourceError

    daemon = Daemon(tmp_path / "config.toml", tmp_path / "runtime/control.json")
    daemon.store = StateStore(tmp_path / "state.sqlite3")
    try:
        daemon._observe_preparation_error(ActivitySourceError("活动推荐无效"))
        alerts = daemon.store.alerts("admin")
        assert [(alert["category"], alert["subject"]) for alert in alerts] == [
            ("activity_source", "shared-environment")
        ]
        assert alerts[0]["details"] == {"reason": "活动推荐无效"}

        daemon._resolve_preparation_alerts()
        assert daemon.store.alerts("admin") == []
    finally:
        daemon.store.close()


def test_preparation_success_resolves_previous_failure_without_query_clearing_core_error(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from maa_planner.daemon.preparation import PreparedEnvironment
    from tests.helpers.activity import empty_activity

    async def scenario():
        settings = Settings(tmp_path / "config.toml", ())
        daemon = Daemon(settings.config_file, tmp_path / "control.json")
        daemon.store = StateStore(tmp_path / "state.sqlite3")
        activity = empty_activity()
        environment = PreparedEnvironment(settings.runtime_template(), activity)
        monkeypatch.setattr(module, "prepare_environment", lambda settings: environment)
        monkeypatch.setattr(
            "maa_planner.daemon.tools.hot_update.prepare_hot_update",
            lambda cache: SimpleNamespace(activity=activity),
        )
        try:
            daemon._observe_preparation_error(ValueError("Core unavailable"))
            await daemon.activity_snapshot(settings)
            assert daemon.store.alerts("admin")[0]["details"]["reason"] == "Core unavailable"
            assert await prepare_dependencies_impl(daemon, settings) == environment
            assert not daemon.store.alerts("admin")
        finally:
            daemon.store.close()

    asyncio.run(scenario())


def test_daemon_shutdown_cancels_workers_without_waiting_for_admission(tmp_path):
    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        gate = asyncio.Event()
        workers = []
        daemon = Daemon(
            config,
            tmp_path / "runtime/control.json",
            root=tmp_path / "state",
            policy=OncePolicy([request(tmp_path)]),
            worker_factory=lambda r, p: FakeWorker(r, p, gate, workers),
        )
        task = asyncio.create_task(daemon.run())
        await eventually(lambda: bool(workers))
        daemon.request_stop()
        await asyncio.wait_for(task, 5)
        assert workers[0].cancelled and workers[0].allowed is False
        state = StateStore(tmp_path / "state/state.sqlite3")
        assert state.unfinished() == []
        assert state.results()[0].status == "cancelled"
        state.close()

    asyncio.run(scenario())


def test_four_independent_emulators_run_concurrently_and_guard_single_daemon(tmp_path):
    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        gate = asyncio.Event()
        workers = []
        reqs = []
        for i in range(4):
            req = request(tmp_path, str(i))
            reqs.append(
                replace(
                    req,
                    binding=replace(req.binding, instance_index=i, instance_name=f"account-{i}"),
                )
            )
        daemon = Daemon(
            config,
            tmp_path / "runtime/control.json",
            root=tmp_path / "state",
            policy=OncePolicy(reqs),
            worker_factory=lambda r, p: FakeWorker(r, p, gate, workers),
        )
        task = asyncio.create_task(daemon.run())
        try:
            await eventually(lambda: len(workers) == 4)
            assert len(daemon.running) == 4
            other = Daemon(config, tmp_path / "elsewhere/control.json", root=tmp_path / "other")
            with pytest.raises(RuntimeError, match="占用"):
                await other.run()
            gate.set()
            await eventually(lambda: len(workers) == 4 and not daemon.running)
            assert all(w.allowed for w in workers)
        finally:
            daemon.request_stop()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_restart_marks_unfinished_attempt_for_recovery(tmp_path):
    async def scenario():
        req = request(tmp_path)
        root = tmp_path / "state"
        state = StateStore(root / "state.sqlite3")
        state.enqueue(req)
        state.start(req.attempt_id)
        state.close()
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        daemon = Daemon(config, tmp_path / "runtime/control.json", root=root)
        task = asyncio.create_task(daemon.run())
        try:
            await eventually(lambda: daemon.configuration is not None)
            assert not daemon.running
        finally:
            daemon.request_stop()
            await asyncio.wait_for(task, 5)
        state = StateStore(root / "state.sqlite3")
        assert state.results()[0].status == "recovery_required"
        assert not state.unfinished()
        state.close()

    asyncio.run(scenario())


def test_daemon_apk_check_is_explicit_single_flight_and_publishes_observations(
    tmp_path, monkeypatch
):
    import threading

    from maa_planner.daemon.config import Settings
    from maa_planner.daemon.tools.apk import ApkInfo
    from maa_planner.daemon.tools.game_update import GameUpdateReport, LatestApk, UnitApkCheck

    calls = []
    started = threading.Event()
    release = threading.Event()
    report = GameUpdateReport(
        LatestApk(
            "https://ak.hycdn.cn/app.apk", ApkInfo("com.hypergryph.arknights", 200, "2.7.81"), "now"
        ),
        (UnitApkCheck("nemo", 1, "up_to_date"),),
    )

    def inspect(settings, *, cancel):
        calls.append(settings)
        started.set()
        assert release.wait(3)
        return report

    monkeypatch.setattr(module, "inspect_game_updates", inspect)

    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        coordinator = Daemon(config, tmp_path / "runtime/control.json", root=tmp_path / "state")
        settings = Settings(config, ())
        loop = asyncio.create_task(coordinator.run())
        try:
            await eventually(
                lambda: coordinator.configuration is not None and coordinator.server.is_serving()
            )
            assert calls == []  # No schedule or policy was silently introduced.
            first = asyncio.create_task(coordinator.check_game_updates(settings))
            second = asyncio.create_task(coordinator.check_game_updates(settings))
            await eventually(started.is_set)
            assert not coordinator.running
            release.set()
            assert await first == await second == report
            await eventually(lambda: coordinator.game_updates == report)
            assert coordinator.context().game_updates == report
            assert len(calls) == 1
            assert await coordinator.check_game_updates(settings) == report
            assert len(calls) == 2  # Completed observations never replace a fresh check.
        finally:
            release.set()
            coordinator.request_stop()
            await asyncio.wait_for(loop, 5)

    asyncio.run(scenario())


def test_failed_daemon_apk_probe_exposes_unknown_without_installing(tmp_path, monkeypatch):
    from maa_planner.daemon.config import Settings

    def fail(*args, **kwargs):
        raise ValueError("Range unsupported")

    monkeypatch.setattr(module, "inspect_game_updates", fail)

    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        coordinator = Daemon(config, tmp_path / "runtime/control.json", root=tmp_path / "state")
        loop = asyncio.create_task(coordinator.run())
        try:
            await eventually(
                lambda: coordinator.configuration is not None and coordinator.server.is_serving()
            )
            with pytest.raises(ValueError, match="Range"):
                await coordinator.check_game_updates(Settings(config, ()))
            await eventually(lambda: coordinator.game_update_error is not None)
            assert coordinator.context().game_updates is None
            assert coordinator.context().game_update_error == "Range unsupported"
            assert not coordinator.running
        finally:
            coordinator.request_stop()
            await asyncio.wait_for(loop, 5)

    asyncio.run(scenario())


def copilot_dependencies(tmp_path, monkeypatch):
    req = request(tmp_path)
    settings = Settings(tmp_path / "config.toml", (req.binding.profile,))
    monkeypatch.setattr(module, "settings_from_bytes", lambda content, path: settings)
    monkeypatch.setattr(module, "preflight_copilot", lambda settings, command: req.binding)
    calls = []

    def prepare(settings, command, binding, revision, attempt_id, cancel):
        calls.append(command)
        return replace(req, job_id=attempt_id, attempt_id=attempt_id, config_revision=revision)

    monkeypatch.setattr(module, "prepare_copilot", prepare)
    return req, calls


def test_immediate_copilot_session_dispatches_under_empty_policy_and_deduplicates(
    tmp_path, monkeypatch
):
    req, preparations = copilot_dependencies(tmp_path, monkeypatch)

    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        gate = asyncio.Event()
        workers = []
        coordinator = Daemon(
            config,
            tmp_path / "runtime/control.json",
            root=tmp_path / "state",
            worker_factory=lambda r, p: FakeWorker(r, p, gate, workers),
        )
        loop = asyncio.create_task(coordinator.run())
        message = {
            "idempotency_key": "manual-1",
            "profile": "nemo",
            "url": "prts://s56648",
            "formation": False,
            "ignore_requirements": True,
            "select": [3, 1],
        }
        try:
            await eventually(
                lambda: coordinator.configuration is not None and coordinator.server.is_serving()
            )
            response = await call(coordinator.server.path, "copilot.run", **message)
            assert response["status"] == "preparing"
            await eventually(lambda: len(workers) == 1)
            duplicate = await call(coordinator.server.path, "copilot.run", **message)
            assert duplicate["attempt_id"] == response["attempt_id"]
            conflict = await call(
                coordinator.server.path,
                "copilot.run",
                **{**message, "formation": True},
            )
            assert "参数不同" in conflict["error"]
            busy = await call(
                coordinator.server.path, "copilot.run", **{**message, "idempotency_key": "manual-2"}
            )
            assert "不排队" in busy["error"]
            gate.set()
            await eventually(lambda: workers[0].allowed is not None and (not coordinator.running))
            assert workers[0].allowed is True
            status = await call(
                coordinator.server.path, "operation.get", operation_id=response["operation_id"]
            )
            assert status["status"] == "succeeded"
            assert (await call(coordinator.server.path, "copilot.run", **message))[
                "status"
            ] == "succeeded"
            assert len(workers) == len(preparations) == 1
            assert preparations[0].formation is False
            assert preparations[0].select == (3, 1)
        finally:
            coordinator.request_stop()
            await asyncio.wait_for(loop, 5)
        coordinator = Daemon(config, tmp_path / "runtime/control.json", root=tmp_path / "state")
        loop = asyncio.create_task(coordinator.run())
        try:
            await eventually(
                lambda: coordinator.configuration is not None and coordinator.server.is_serving()
            )
            assert (await call(coordinator.server.path, "copilot.run", **message))[
                "status"
            ] == "succeeded"
            assert not coordinator.running
        finally:
            coordinator.request_stop()
            await asyncio.wait_for(loop, 5)

    asyncio.run(scenario())


def test_inactive_game_is_rejected_before_download_or_worker(tmp_path, monkeypatch):
    _, calls = copilot_dependencies(tmp_path, monkeypatch)

    def inactive(*args):
        raise RuntimeError("明日方舟未运行")

    monkeypatch.setattr(module, "preflight_copilot", inactive)

    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        coordinator = Daemon(config, tmp_path / "runtime/control.json", root=tmp_path / "state")
        loop = asyncio.create_task(coordinator.run())
        try:
            await eventually(
                lambda: coordinator.configuration is not None and coordinator.server.is_serving()
            )
            response = await call(
                coordinator.server.path,
                "copilot.run",
                idempotency_key="offline",
                profile="nemo",
                url="prts://1",
            )
            assert "未运行" in response["error"]
            assert calls == [] and (not coordinator.running)
            assert coordinator.store is not None
            assert coordinator.store.copilot("offline") is None
        finally:
            coordinator.request_stop()
            await asyncio.wait_for(loop, 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("reload", [False, True])
def test_copilot_preparation_failure_and_config_snapshot_isolation(tmp_path, monkeypatch, reload):
    import threading

    req, calls = copilot_dependencies(tmp_path, monkeypatch)
    started, release = (threading.Event(), threading.Event())

    def prepare(settings, command, binding, revision, attempt_id, cancel):
        started.set()
        assert release.wait(3)
        if not reload:
            raise ValueError("下载失败")
        return replace(req, job_id=attempt_id, attempt_id=attempt_id, config_revision=revision)

    monkeypatch.setattr(module, "prepare_copilot", prepare)

    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        coordinator = Daemon(config, tmp_path / "runtime/control.json", root=tmp_path / "state")
        gate = asyncio.Event()
        workers = []
        coordinator.worker_factory = lambda r, p: FakeWorker(r, p, gate, workers)
        loop = asyncio.create_task(coordinator.run())
        try:
            await eventually(
                lambda: coordinator.configuration is not None and coordinator.server.is_serving()
            )
            original = coordinator.configuration
            assert original is not None
            await call(
                coordinator.server.path,
                "copilot.run",
                idempotency_key="test",
                profile="nemo",
                url="prts://1",
            )
            await eventually(started.is_set)
            assert (await call(coordinator.server.path, "operation.find", idempotency_key="test"))[
                "status"
            ] == "preparing"
            if reload:
                before = coordinator.configuration
                config.write_text('[[profiles]]\nname="nemo"\n# revision 2')
                await eventually(lambda: coordinator.configuration != before)
            release.set()
            store = coordinator.store
            assert store is not None

            def failed():
                value = store.copilot(store.find_operation_key("test"))
                return value is not None and value["status"] == "failed"

            if reload:
                await eventually(lambda: bool(workers))
                assert workers[0].request.config_revision == original.revision
                gate.set()
                await eventually(lambda: not coordinator.running)
                assert workers[0].allowed is True
                result = store.copilot(store.find_operation_key("test"))
                assert result is not None and result["status"] == "succeeded"
            else:
                await eventually(failed)
                assert not workers
            assert not coordinator.running
            assert store.unfinished() == []
        finally:
            release.set()
            coordinator.request_stop()
            await asyncio.wait_for(loop, 5)

    asyncio.run(scenario())


def test_shutdown_cancels_pending_preparation_without_replay(tmp_path, monkeypatch):
    import threading

    copilot_dependencies(tmp_path, monkeypatch)
    started = threading.Event()

    def prepare(settings, command, binding, revision, attempt_id, cancel):
        started.set()
        assert cancel.wait(3)
        raise RuntimeError("copilot 准备已取消")

    monkeypatch.setattr(module, "prepare_copilot", prepare)

    async def scenario():
        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n# revision 1')
        coordinator = Daemon(config, tmp_path / "runtime/control.json", root=tmp_path / "state")
        loop = asyncio.create_task(coordinator.run())
        await eventually(
            lambda: coordinator.configuration is not None and coordinator.server.is_serving()
        )
        await coordinator.submit_copilot(CopilotCommand("stopping", "nemo", "prts://1"))
        await eventually(started.is_set)
        coordinator.request_stop()
        await asyncio.wait_for(loop, 5)
        store = StateStore(tmp_path / "state/state.sqlite3")
        value = store.copilot("stopping")
        assert value is not None and value["status"] == "failed"
        assert store.unfinished() == []
        store.close()

    asyncio.run(scenario())


def test_interrupted_copilot_preparation_is_not_replayed(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.reserve_copilot("interrupted", {"url": "prts://1"}, "attempt")
    store.close()
    store = StateStore(tmp_path / "state.sqlite3")
    store.recover_copilot()
    value = store.copilot("interrupted")
    assert value is not None and value["status"] == "failed"
    assert store.unfinished() == []
    store.close()


@pytest.mark.parametrize("reason", ["SIGINT", "SIGTERM", "SIGBREAK", "stop --force"])
def test_interruption_requests_stop_and_persists_one_alert(tmp_path, reason):
    from maa_planner.daemon.coordinator import PendingExecution

    async def scenario():
        daemon = Daemon(tmp_path / "config", tmp_path / "socket", root=tmp_path / "state")
        daemon.store = StateStore(tmp_path / "state.sqlite3")
        req = request(tmp_path)
        daemon.running[req.attempt_id] = (
            PendingExecution(req),
            asyncio.create_task(asyncio.sleep(0)),
        )
        if reason == "stop --force":
            daemon.request_drain(force=True)
        else:
            daemon.request_stop(reason=reason)
        daemon.request_stop(reason=reason)
        assert daemon.stopping.is_set() and daemon.wakeup.is_set()
        alerts = daemon.store.alerts("nemo")
        assert len(alerts) == 1
        assert alerts[0]["category"] == "execution_interrupted"
        assert alerts[0]["details"]["reason"] == reason
        assert len([e for e in daemon.store.events(0) if e["kind"] == "daemon_interrupted"]) == 1
        daemon.store.close()

    asyncio.run(scenario())

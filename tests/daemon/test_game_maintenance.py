from datetime import datetime
from pathlib import Path

import pytest

from maa_planner.daemon.config import settings_from_bytes
from maa_planner.daemon.constants import BUSINESS_TIMEZONE


def test_game_update_defaults_and_configured_clock_use_beijing_time():
    settings = settings_from_bytes(b'[[profiles]]\nname="nemo"', Path("/tmp/config.toml"))
    assert settings.game_update.enabled
    assert settings.game_update.time == "11:00"
    assert settings.game_update.poll_interval == 30
    now = datetime(2026, 11, 12, 10, 59, tzinfo=BUSINESS_TIMEZONE).timestamp()
    assert settings.game_update.next_after(now) == now + 60
    configured = settings_from_bytes(
        b'[[profiles]]\nname="nemo"\n[game_update]\ntime="09:30"\npoll_interval=60\nresource_timeout=1200',
        Path("/tmp/config.toml"),
    )
    assert configured.game_update.time == "09:30"
    assert configured.game_update.poll_interval == 60
    assert configured.game_update.resource_timeout == 1200


@pytest.mark.parametrize(
    "setting",
    [
        'time="25:00"',
        "enabled=1",
        "poll_interval=0",
        "poll_interval=true",
        "resource_timeout=-1",
        "resource_timeout=nan",
        "unexpected=true",
    ],
)
def test_invalid_game_update_schedule_is_rejected(setting):
    with pytest.raises(ValueError, match="game_update"):
        settings_from_bytes(
            ('[[profiles]]\nname="nemo"\n[game_update]\n' + setting).encode(),
            Path("/tmp/config.toml"),
        )


def test_failed_resource_probe_persists_only_pending_fact_and_retry_does_not_reinstall(
    tmp_path, monkeypatch
):
    import asyncio
    import threading
    from types import SimpleNamespace

    from maa_planner.daemon import game_maintenance as service
    from maa_planner.daemon.config import Profile, Settings
    from maa_planner.daemon.execution.context import RunResult, UnitBinding
    from maa_planner.daemon.state import StateStore
    from maa_planner.daemon.tools.apk import ApkInfo
    from maa_planner.daemon.tools.game_update import GameUpdateReport, LatestApk, UnitApkCheck

    target = ApkInfo("com.hypergryph.arknights", 200, "2.7.81")
    profile = Profile("nemo")
    settings = Settings(tmp_path / "config.toml", (profile,), mumu_path=tmp_path / "MuMu")
    installed = []
    probes = []
    ready = False

    def install(settings, **kwargs):
        assert not kwargs["allow_emulator_start"]
        status = "up_to_date"
        if not installed:
            kwargs["on_install"](profile, target)
            installed.append("installed")
            status = "updated"
        return GameUpdateReport(
            LatestApk("https://ak.hycdn.cn/app.apk", target, "now"),
            (UnitApkCheck("nemo", 1, status, target),),
        )

    monkeypatch.setattr(service, "update_game", install)
    monkeypatch.setattr(
        service, "resolve_binding", lambda *a: UnitBinding(profile, tmp_path / "MuMu", 1, "nemo")
    )

    class Worker:
        def __init__(self, request, root):
            self.request = request

        async def run(self, admit, phase):
            assert await admit()
            assert not self.request.options.allow_emulator_start
            import json

            tasks = json.loads(self.request.core.tasks.document)["tasks"]
            assert [t["type"] for t in tasks] == ["Custom", "CloseDown"]
            probes.append(self.request.attempt_id)
            evidence = ["callbacks.jsonl:1"] if ready else []
            return RunResult(
                self.request.attempt_id,
                "succeeded",
                business_results=(
                    {"id": "maintenance:resources", "status": "completed", "evidence": evidence},
                ),
            )

        async def cancel(self):
            pass

    async def scenario():
        nonlocal ready

        async def prepare(settings):
            return SimpleNamespace(runtime=settings.runtime_template())

        daemon = SimpleNamespace(
            store=StateStore(tmp_path / "state.sqlite3"),
            root=tmp_path,
            stopping=asyncio.Event(),
            _apk_cancel=threading.Event(),
            prepare_dependencies=prepare,
            worker_factory=Worker,
        )
        try:
            with pytest.raises(service.GameUpdateIncomplete):
                await service.perform_update(
                    daemon, settings, threading.Event(), automatic=True, revision="r1"
                )
            assert list(daemon.store.pending_game_resources()) == ["nemo"]
            daemon.store.close()
            daemon.store = StateStore(tmp_path / "state.sqlite3")
            assert list(daemon.store.pending_game_resources()) == ["nemo"]
            ready = True
            result = await service.perform_update(
                daemon, settings, threading.Event(), automatic=True, revision="r1"
            )
            assert result["units"][0]["resources"]["status"] == "ready"
            assert daemon.store.pending_game_resources() == {}
            assert installed == ["installed"] and len(probes) == 2
            assert (
                daemon.store.db.execute("SELECT COUNT(*) FROM game_resource_pending").fetchone()[0]
                == 0
            )
        finally:
            daemon.store.close()

    asyncio.run(scenario())


def test_due_update_waits_for_existing_work_is_exclusive_and_deduplicated_after_restart(
    tmp_path, monkeypatch
):
    import asyncio
    import threading
    import time

    from maa_planner.daemon import game_maintenance as service
    from maa_planner.daemon import observations as api_module
    from tests.helpers.async_tools import eventually
    from tests.helpers.daemon import running
    from tests.helpers.scheduling import Clock

    clock = Clock("2026-11-12T10:59:59")
    monkeypatch.setattr(time, "time", clock.now)
    config = tmp_path / "config.toml"
    config.write_text('[mumu]\npath="MuMu"\n[[profiles]]\nname="nemo"')
    gate = threading.Event()
    started = threading.Event()
    calls = []
    async_gate = None

    def query(*args):
        started.set()
        assert gate.wait(5)
        return {}

    monkeypatch.setattr(api_module, "execute_sync", query)

    async def update(daemon, settings, cancel, *, automatic, revision):
        calls.append((automatic, settings.game_update.poll_interval))
        assert async_gate is not None
        await async_gate.wait()
        return {"units": []}

    monkeypatch.setattr(service, "perform_update", update)

    async def scenario():
        nonlocal async_gate
        async_gate = asyncio.Event()
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            await client.start("emulator.list", idempotency_key="existing")
            await eventually(started.is_set)
            clock.set("2026-11-12T11:00:00")
            daemon.wakeup.set()
            await eventually(lambda: daemon.game_maintenance.pending_at is not None)
            assert calls == [] and daemon.operations.maintenance is None
            # A due update closes every new resource entry while its current user finishes.
            from maa_planner.schema import APIError

            run = {"profile": "nemo", "plan": {"tasks": [{"type": "Fight"}]}}
            operations = [
                ("emulator.list", {}),
                ("execution.run", run),
                ("copilot.run", {"profile": "nemo", "url": "prts://123"}),
                ("execution.batch", {"jobs": [{"method": "execution.run", **run}]}),
                ("game.update", {}),
            ]
            for attempt in range(3):
                for method, arguments in operations:
                    key = f"waiting-{attempt}-{method}"
                    with pytest.raises(APIError) as failure:
                        await client.call(method, idempotency_key=key, **arguments)
                    assert failure.value.code == "busy"
                    assert await client.call("operation.find", idempotency_key=key) is None
                assert (await client.call("daemon.status"))["game_update"]["pending_at"] is not None
                assert calls == []
            gate.set()
            await eventually(lambda: len(calls) == 1)
            assert calls == [(True, 30)]
            with pytest.raises(RuntimeError, match="维护"):
                await client.start("emulator.list", idempotency_key="blocked")
            for _ in range(3):
                daemon.wakeup.set()
                await asyncio.sleep(0)
            assert len(calls) == 1
            async_gate.set()
            await eventually(lambda: daemon.game_maintenance.active is None)
            assert daemon.store.db.execute("SELECT COUNT(*) FROM scheduled_runs").fetchone()[0] == 0
        async with running(tmp_path, monkeypatch) as (daemon, _, _):
            await asyncio.sleep(0.03)
            assert len(calls) == 1
            assert daemon.game_maintenance.active is None

    try:
        asyncio.run(scenario())
    finally:
        gate.set()


def test_update_and_daily_at_same_startup_minute_preserve_daily_occurrence(tmp_path, monkeypatch):
    import asyncio

    from maa_planner.daemon import game_maintenance as service
    from tests.helpers.async_tools import eventually
    from tests.helpers.scheduling import Clock, document, running

    async def scenario():
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def update(*args, **kwargs):
            entered.set()
            await gate.wait()
            return {"units": []}

        monkeypatch.setattr(service, "perform_update", update)
        clock = Clock("2026-11-12T11:00:00")
        content = document(names=("nemo",), times='["11:00"]') + '\n[mumu]\npath="MuMu"\n'
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            await eventually(entered.is_set)
            assert len(state.daemon.daily.pending) == 1
            assert not state.workers
            gate.set()
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())


def test_due_maintenance_runs_before_new_daily_after_existing_daily_finishes(tmp_path, monkeypatch):
    import asyncio

    from maa_planner.daemon import game_maintenance as service
    from tests.helpers.async_tools import eventually
    from tests.helpers.scheduling import Clock, document, running, wake

    async def scenario():
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def update(*args, **kwargs):
            entered.set()
            await gate.wait()
            return {"units": []}

        monkeypatch.setattr(service, "perform_update", update)
        clock = Clock("2026-11-12T10:58:59")
        content = document(names=("nemo",), times='["10:59","11:00"]') + '\n[mumu]\npath="MuMu"\n'
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            wake(state, clock, "2026-11-12T10:59:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            wake(state, clock, "2026-11-12T11:00:00")
            await eventually(lambda: state.daemon.game_maintenance.pending_at is not None)
            state.workers[0].finish.set()
            await eventually(entered.is_set)
            assert len(state.workers) == 1
            gate.set()
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            state.workers[1].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())


def test_login_exemption_is_global_survives_restart_and_expires_at_midnight(tmp_path, monkeypatch):
    import asyncio
    import time

    from maa_planner.daemon import game_maintenance as service
    from tests.helpers.async_tools import eventually
    from tests.helpers.daemon import running
    from tests.helpers.scheduling import Clock, timestamp

    clock = Clock("2026-11-13T06:02:00")
    monkeypatch.setattr(time, "time", clock.now)
    (tmp_path / "config.toml").write_text(
        '[mumu]\npath="MuMu"\n[[profiles]]\nname="nemo"\n[[profiles]]\nname="wyf"'
    )
    calls = []

    async def update(*args, **kwargs):
        calls.append("update")
        return {"units": []}

    monkeypatch.setattr(service, "perform_update", update)
    receipt = {
        "started_at": timestamp("2026-11-13T06:01:00"),
        "observed_at": timestamp("2026-11-13T06:01:10"),
        "evidence": ["callbacks:1", "callbacks:2"],
    }

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, _, _):
            daemon.store.mark_game_resources_pending(
                "wyf", {"package": "game", "version_code": 1, "version_name": "old"}
            )
            daemon.game_maintenance.observe_login("nemo", receipt, attempt_id="login")
            assert daemon.store.maintenance_login()["profile"] == "nemo"
            clock.set("2026-11-13T11:00:00")
            daemon.wakeup.set()
            await eventually(lambda: daemon.game_maintenance.next_at > clock.now())
            assert calls == []
            assert "wyf" in daemon.store.pending_game_resources()
        async with running(tmp_path, monkeypatch) as (daemon, _, _):
            assert daemon.game_maintenance.status()["login_exemption"]["day"] == "2026-11-13"
            assert calls == []
            clock.set("2026-11-14T00:00:00")
            daemon.wakeup.set()
            await eventually(lambda: daemon.store.maintenance_login() is None)
            clock.set("2026-11-14T11:00:00")
            daemon.wakeup.set()
            await eventually(lambda: calls == ["update"])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-11-13T05:59:59", "2026-11-13T06:01:00"),
        ("2026-11-13T11:59:59", "2026-11-13T12:00:00"),
        ("2026-11-12T06:01:00", "2026-11-12T06:02:00"),
    ],
)
def test_login_outside_window_cannot_skip_update(tmp_path, monkeypatch, start, end):
    import asyncio
    import time
    from types import SimpleNamespace

    from maa_planner.daemon.game_maintenance import GameUpdateScheduler
    from maa_planner.daemon.state import StateStore
    from tests.helpers.scheduling import Clock, timestamp

    clock = Clock("2026-11-13T13:00:00")
    monkeypatch.setattr(time, "time", clock.now)
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        daemon = SimpleNamespace(store=store, wakeup=asyncio.Event())
        scheduler = GameUpdateScheduler(daemon)
        scheduler.observe_login(
            "nemo",
            {"started_at": timestamp(start), "observed_at": timestamp(end), "evidence": ["a", "b"]},
            attempt_id="login",
        )
        assert store.maintenance_login() is None
    finally:
        store.close()


def test_login_report_cancels_waiting_update_while_other_work_is_still_running(
    tmp_path, monkeypatch
):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace

    from maa_planner.daemon import game_maintenance as service
    from maa_planner.daemon import observations as api_module
    from tests.helpers.async_tools import eventually
    from tests.helpers.daemon import running
    from tests.helpers.scheduling import Clock, timestamp

    clock = Clock("2026-11-13T10:59:59")
    monkeypatch.setattr(time, "time", clock.now)
    (tmp_path / "config.toml").write_text('[mumu]\npath="MuMu"\n[[profiles]]\nname="nemo"')
    gate = threading.Event()
    started = threading.Event()

    def query(*args):
        started.set()
        assert gate.wait(5)
        return {}

    async def unexpected_update(*args, **kwargs):
        pytest.fail("已确认窗口内登录成功，不应派发当天自动更新")

    monkeypatch.setattr(api_module, "execute_sync", query)
    monkeypatch.setattr(service, "perform_update", unexpected_update)

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            await client.start("emulator.list", idempotency_key="busy")
            await eventually(started.is_set)
            clock.set("2026-11-13T11:00:00")
            daemon.wakeup.set()
            await eventually(lambda: daemon.game_maintenance.pending_at is not None)
            req = SimpleNamespace(
                binding=SimpleNamespace(profile=SimpleNamespace(name="nemo")),
                attempt_id="fresh-login",
            )
            receipt = {
                "started_at": timestamp("2026-11-13T10:59:00"),
                "observed_at": timestamp("2026-11-13T10:59:10"),
                "evidence": ["login", "home"],
            }
            await daemon._changes.put(("login_success", (req, receipt)))
            daemon.wakeup.set()
            await eventually(lambda: daemon.game_maintenance.pending_at is None)
            assert not gate.is_set()
            assert daemon.game_maintenance.next_at == timestamp("2026-11-14T11:00:00")
            assert daemon.game_maintenance.active is None
            gate.set()
            await eventually(lambda: not daemon.operations.tasks)

    try:
        asyncio.run(scenario())
    finally:
        gate.set()

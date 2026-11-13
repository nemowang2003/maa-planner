import asyncio
import threading
import time

import pytest

from maa_planner.daemon import coordinator as daemon_module
from maa_planner.daemon import observations as api_module
from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.state import StateStore
from tests.helpers.async_tools import eventually
from tests.helpers.daemon import running


def test_async_operation_is_idempotent_and_events_survive_restart(tmp_path, monkeypatch):
    calls = []
    gate = threading.Event()

    def execute(action, settings, cancel, root):
        calls.append(action)
        assert gate.wait(3)
        return {"instances": 4}

    monkeypatch.setattr(api_module, "execute_sync", execute)

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            operation = await client.start("emulator.list", idempotency_key="query")
            assert operation["status"] == "preparing"
            assert (await client.call("daemon.status"))["operations"] == [operation["operation_id"]]
            assert (await client.start("emulator.list", idempotency_key="query"))[
                "operation_id"
            ] == operation["operation_id"]
            with pytest.raises(RuntimeError, match="参数不同"):
                await client.start("runtime.ensure", idempotency_key="query")
            gate.set()
            await eventually(lambda: daemon.operations.find("query")["status"] == "succeeded")
            value = await client.call("operation.find", idempotency_key="query")
            assert value["result"] == {"instances": 4}
            events = await client.call("event.list", after=0)
            assert len(events) >= 3 and [e["sequence"] for e in events] == sorted(
                e["sequence"] for e in events
            )
        async with running(tmp_path, monkeypatch) as (_, client, _):
            value = await client.start("emulator.list", idempotency_key="query")
            assert value["status"] == "succeeded"
            assert value["operation_id"] == operation["operation_id"]
            assert (await client.call("event.list", after=events[-1]["sequence"])) == []
        assert calls == ["emulators"]

    asyncio.run(scenario())


def test_maintenance_is_exclusive_and_restart_drains_it(tmp_path, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def execute(action, settings, cancel, root):
        started.set()
        assert gate.wait(3)
        return {"done": True}

    monkeypatch.setattr(api_module, "execute_sync", execute)

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, task):
            await client.start("resource.update", idempotency_key="maintenance")
            await eventually(started.is_set)
            with pytest.raises(RuntimeError, match="维护"):
                await client.start("emulator.list", idempotency_key="query")
            with pytest.raises(RuntimeError, match="不能中途取消"):
                await client.call(
                    "operation.cancel",
                    operation_id=(
                        await client.call("operation.find", idempotency_key="maintenance")
                    )["operation_id"],
                )
            state = await client.call("daemon.restart")
            assert state["draining"] and state["restart_requested"]
            assert not task.done()
            gate.set()
            await asyncio.wait_for(task, 5)
            assert daemon.restart_requested
            store = StateStore(tmp_path / "state/state.sqlite3")
            value = store.operation(store.find_operation_key("maintenance"))
            assert value is not None and value["status"] == "succeeded"
            store.close()

    asyncio.run(scenario())


def test_queries_can_be_cancelled_without_claiming_thread_finished(tmp_path, monkeypatch):
    started = threading.Event()

    def execute(action, settings, cancel, root):
        started.set()
        assert cancel.wait(3)
        return {"late": True}

    monkeypatch.setattr(api_module, "execute_sync", execute)

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            await client.start("emulator.list", idempotency_key="query")
            await eventually(started.is_set)
            state = await client.call(
                "operation.cancel",
                operation_id=(await client.call("operation.find", idempotency_key="query"))[
                    "operation_id"
                ],
            )
            assert state["cancel_requested"]
            await eventually(lambda: daemon.operations.find("query")["status"] == "cancelled")

    asyncio.run(scenario())


def test_expired_and_invalid_requests_have_no_side_effects(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(api_module, "execute_sync", lambda *args: calls.append(args))

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            for method, params in [
                ("game.update", {"latest_start_at": time.time() - 1}),
                ("runtime.prepare", {"directory": "/tmp/arbitrary"}),
                ("unknown", {}),
                ("execution.run", {"profile": "missing", "plan": {"tasks": [{"type": "Award"}]}}),
            ]:
                with pytest.raises(RuntimeError):
                    await client.call(method, idempotency_key="bad", **params)
            assert calls == [] and daemon.operations.find_key("bad") is None
            with pytest.raises(RuntimeError):
                await client.call("daemon.restart", force="false")

    asyncio.run(scenario())


def test_semantically_invalid_config_retains_effective_snapshot(tmp_path, monkeypatch):
    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            before = await client.call("config.get")
            daemon.config.write_text(
                '[defaults]\nfight_times=99\n[[profiles]]\nname="nemo"\n', newline="\n"
            )
            await eventually(lambda: daemon.config_error is not None)
            assert daemon.configuration.revision == before["revision"]
            with pytest.raises(RuntimeError, match="有效"):
                await client.start("emulator.list", idempotency_key="invalid")
            # Status remains available while configuration is invalid.
            assert (await client.call("daemon.status"))["configuration_error"]

    asyncio.run(scenario())


def test_interrupted_operation_has_no_automatic_replay(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    store.reserve_operation(
        "interrupted",
        {"action": "game-update", "target": None, "arguments": {}, "expires_at": None},
    )
    store.close()
    store = StateStore(path)
    store.recover_operations()
    value = store.operation("interrupted")
    assert value is not None and value["status"] == "recovery_required"
    store.close()


def test_run_and_copilot_submit_cannot_race_with_maintenance(tmp_path, monkeypatch):
    from maa_planner.daemon.config import Settings
    from tests.helpers.execution import request

    req = request(tmp_path)
    gate, started = threading.Event(), threading.Event()

    def preflight(settings, command):
        started.set()
        assert gate.wait(3)
        return req.binding

    monkeypatch.setattr(daemon_module, "preflight_copilot", preflight)
    monkeypatch.setattr(
        daemon_module,
        "settings_from_bytes",
        lambda *args: Settings(tmp_path / "config.toml", (req.binding.profile,)),
    )

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            pending = asyncio.create_task(
                client.start(
                    "copilot.run",
                    profile="nemo",
                    idempotency_key="first",
                    parameters={"url": "prts://1"},
                )
            )
            await eventually(started.is_set)
            with pytest.raises(RuntimeError, match="维护不排队"):
                await client.start("resource.update", idempotency_key="maintenance")
            with pytest.raises(RuntimeError, match="参数不同"):
                await client.start("emulator.list", idempotency_key="first")
            await client.call(
                "operation.cancel",
                operation_id=(await client.call("operation.find", idempotency_key="first"))[
                    "operation_id"
                ],
            )
            gate.set()
            with pytest.raises(RuntimeError, match="取消"):
                await pending
            assert (await client.call("operation.find", idempotency_key="first"))[
                "status"
            ] == "cancelled"
            assert not daemon.running

    asyncio.run(scenario())


def test_explicit_embedded_plan_is_executed_only_by_daemon_worker(tmp_path, monkeypatch):
    import json

    from maa_planner.daemon.config import Settings
    from maa_planner.daemon.execution import copilot
    from maa_planner.daemon.execution.context import RunResult
    from tests.helpers.execution import request

    req = request(tmp_path)
    seen = []

    class Worker:
        def __init__(self, value, root):
            seen.append(value)
            self.value = value

        async def run(self, admit, phase):
            assert await admit()
            await phase("running")
            return RunResult(self.value.attempt_id, "succeeded")

        async def cancel(self):
            pass

    monkeypatch.setattr(copilot, "resolve_binding", lambda *args: req.binding)

    async def scenario():
        async with running(tmp_path, monkeypatch, worker_factory=Worker) as (daemon, client, _):
            settings = Settings(
                daemon.config, (req.binding.profile,), core_runtime=req.core.runtime
            )
            monkeypatch.setattr(daemon.control_configuration, "settings", lambda **kw: settings)
            value = await client.start(
                "execution.run",
                profile="nemo",
                idempotency_key="embedded",
                parameters={
                    "plan": {
                        "tasks": [{"type": "Infrast", "params": {"filename": "@file:base.json"}}],
                        "attachments": {"base.json": {"plans": []}},
                    }
                },
            )
            assert value["operation_id"] != "embedded" and value["idempotency_key"] == "embedded"
            await eventually(lambda: daemon.operations.find("embedded")["status"] == "succeeded")
            assert len(seen) == 1
            assert json.loads(seen[0].core.tasks.attachments) == {"base.json": {"plans": []}}
            assert "arguments" not in daemon.operations.find("embedded")

    asyncio.run(scenario())


def test_game_query_publishes_context_and_never_persists_apk_metadata(tmp_path, monkeypatch):
    from maa_planner.daemon.tools.apk import ApkInfo
    from maa_planner.daemon.tools.game_update import GameUpdateReport, LatestApk

    report = GameUpdateReport(
        LatestApk(
            "https://ak.hycdn.cn/current.apk",
            ApkInfo("com.hypergryph.arknights", 999, "private-version"),
            "now",
        ),
        (),
    )
    monkeypatch.setattr(daemon_module, "inspect_game_updates", lambda *args, **kwargs: report)

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            await client.start("game.inspect", idempotency_key="apk")
            await eventually(lambda: daemon.operations.find("apk")["status"] == "succeeded")
            await eventually(lambda: daemon.context().game_updates == report)
            value = await client.call("operation.find", idempotency_key="apk")
            assert (
                value["result_available"]
                and value["result"]["latest"]["metadata"]["version_name"] == "private-version"
            )
            stored = daemon.operations.store.operation(
                daemon.operations.store.find_operation_key("apk")
            )
            assert stored is not None and stored["result"] == {"ephemeral": True}
        assert b"private-version" not in (tmp_path / "state/state.sqlite3").read_bytes()
        async with running(tmp_path, monkeypatch) as (_, client, _):
            value = await client.call("operation.find", idempotency_key="apk")
            assert value["status"] == "succeeded" and not value["result_available"]
            assert value["result"] is None

    asyncio.run(scenario())


def test_apk_error_details_are_also_ephemeral(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private-version https://ak.hycdn.cn/current.apk")

    monkeypatch.setattr(daemon_module, "inspect_game_updates", fail)

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            await client.start("game.inspect", idempotency_key="apk-error")
            await eventually(lambda: daemon.operations.find("apk-error")["status"] == "failed")
            value = await client.call("operation.find", idempotency_key="apk-error")
            assert "private-version" in value["error"]
            stored = daemon.operations.store.operation(
                daemon.operations.store.find_operation_key("apk-error")
            )
            assert stored is not None and "private-version" not in stored["error"]
        assert b"private-version" not in (tmp_path / "state/state.sqlite3").read_bytes()

    asyncio.run(scenario())


def test_existing_copilot_ids_are_migrated_to_control_operations(tmp_path):
    from maa_planner.daemon.copilot_command import CopilotCommand

    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    store.reserve_copilot(
        "existing", CopilotCommand("existing", "nemo", "prts://1").to_dict(), "attempt"
    )
    store.close()

    store = StateStore(path)
    value = store.operation("existing")
    assert value is not None and value["action"] == "copilot"
    assert value["target"] == "nemo" and value["arguments"]["formation"] is False
    store.close()


def test_manual_stage_confirmation_is_account_activity_and_mode_scoped(tmp_path, monkeypatch):
    async def scenario():
        params = {"profile": "nemo", "activity_id": "act55side"}
        async with running(tmp_path, monkeypatch) as (_, client, _):
            assert await client.call("stage.progress.get", **params) == []
            value = await client.call(
                "stage.confirm", **params, stage_id="act55side_ex01", is_raid=True
            )
            assert value["source"] == "api"
            with pytest.raises(RuntimeError, match="is_raid"):
                await client.call(
                    "stage.confirm", **params, stage_id="act55side_ex01", is_raid="false"
                )
            with pytest.raises(RuntimeError, match="未知 profile"):
                await client.call(
                    "stage.confirm",
                    profile="unknown",
                    activity_id="act55side",
                    stage_id="act55side_ex01",
                )
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            rows = await client.call("stage.progress.get", **params)
            assert len(rows) == 1 and rows[0]["is_raid"] is True
            assert daemon.operations.store.completed_stages("nemo", "act55side") == {
                ("act55side_ex01", True)
            }
            assert (
                await client.call("stage.progress.get", profile="nemo", activity_id="other_event")
                == []
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("option", ["is_raid", "use_sanity_potion"])
@pytest.mark.parametrize("enabled", [False, True])
def test_copilot_options_retries_survive_restart(tmp_path, monkeypatch, batch, option, enabled):
    arguments = {
        "url": "prts://31",
        "formation": False,
        "ignore_requirements": False,
        "start_at": 1,
        "select": None,
    }
    if enabled:
        arguments[option] = True
    job = {"action": "copilot", "target": "nemo", "arguments": arguments, "expires_at": None}
    payload = (
        {"action": "batch", "target": None, "arguments": {"jobs": [job]}, "expires_at": None}
        if batch
        else job
    )

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, _, _):
            daemon.operations.store.reserve_operation(
                "old", {**payload, "idempotency_key": "replay"}
            )
            daemon.operations.store.operation_update("old", "succeeded")
        async with running(tmp_path, monkeypatch) as (daemon, client, _):

            async def retry(flag):
                args = {"url": "prts://31", **flag}
                if batch:
                    return await client.call(
                        "execution.batch",
                        idempotency_key="replay",
                        jobs=[{"method": "copilot.run", "profile": "nemo", **args}],
                    )
                return await client.call(
                    "copilot.run", idempotency_key="replay", profile="nemo", **args
                )

            assert (await retry({option: enabled}))["status"] == "succeeded"
            if not enabled:
                assert (await retry({}))["status"] == "succeeded"
            with pytest.raises(RuntimeError, match="参数不同"):
                await retry({option: not enabled})
            assert not daemon.operations.tasks

    asyncio.run(scenario())


def test_configuration_replace_persists_full_toml_and_detects_conflicts(tmp_path, monkeypatch):
    content = '# 保留这段注释\n[[profiles]]\nname="wyf"\nenabled=false\n'

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            old = await client.call("config.get")
            new = await client.replace_config(content, expected_revision=old["revision"])
            assert new["toml"] == content
            assert new["revision"] != old["revision"]
            assert new["effective"]["profiles"][0]["name"] == "wyf"
            assert daemon.config.read_text() == content
            with pytest.raises(RuntimeError, match="版本冲突"):
                await client.replace_config(old["toml"], expected_revision=old["revision"])
            # Even an external edit not yet delivered by inotify must be observed.
            external = content.replace("wyf", "zy")
            daemon.config.write_text(external, newline="\n")
            with pytest.raises(RuntimeError, match="版本冲突"):
                await client.replace_config(content, expected_revision=new["revision"])
            assert (await client.call("config.get"))["toml"] == external
        async with running(tmp_path, monkeypatch) as (_, client, _):
            assert (await client.call("config.get"))["toml"] == external

    asyncio.run(scenario())


def test_invalid_or_unwritable_configuration_is_not_accepted(tmp_path, monkeypatch):
    from maa_planner.daemon.tools import storage

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            old = await client.call("config.get")
            with pytest.raises(RuntimeError):
                await client.replace_config("[invalid")
            assert daemon.config.read_text() == old["toml"]
            assert (await client.call("config.get"))["revision"] == old["revision"]

            def fail(*args):
                raise OSError("disk write failed")

            with monkeypatch.context() as patch:
                patch.setattr(storage, "atomic_write", fail)
                with pytest.raises(RuntimeError, match="disk write failed"):
                    await client.replace_config('[[profiles]]\nname="wyf"\n')
            assert daemon.config.read_text() == old["toml"]
            assert (await client.call("config.get"))["revision"] == old["revision"]
            daemon.config.write_text("[invalid", newline="\n")
            await eventually(lambda: daemon.config_error is not None)
            new = await client.replace_config(old["toml"])
            assert new["error"] is None

    asyncio.run(scenario())


def test_reconciliation_and_reading_config_do_not_poll_the_file(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    async def scenario():
        async with running(tmp_path, monkeypatch, reconcile_interval=0.01) as (daemon, client, _):
            reload = AsyncMock(wraps=daemon._configuration)
            monkeypatch.setattr(daemon, "_configuration", reload)
            (tmp_path / "unrelated.toml").write_text("ignored")
            daemon.config.read_text()  # Read-only OS events must not trigger another read.
            for _ in range(3):
                daemon.wakeup.set()
                await client.call("config.get")
                await asyncio.sleep(0.04)
            assert reload.await_count == 0
            daemon.config.write_text('[[profiles]]\nname="wyf"\n', newline="\n")
            await eventually(lambda: daemon.operations.settings().profiles[0].name == "wyf")
            assert reload.await_count >= 1

    asyncio.run(scenario())


def test_configuration_replace_does_not_change_an_active_operation(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    observed = []

    def execute(action, settings, cancel, root):
        observed.append(settings.profiles[0].name)
        started.set()
        assert release.wait(3)
        observed.append(settings.profiles[0].name)
        return {"account": settings.profiles[0].name}

    monkeypatch.setattr(api_module, "execute_sync", execute)

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            try:
                await client.start("emulator.list", idempotency_key="old-config")
                await eventually(started.is_set)
                await client.replace_config('[[profiles]]\nname="wyf"\n')
                assert daemon.operations.settings().profiles[0].name == "wyf"
                release.set()
                await eventually(
                    lambda: daemon.operations.find("old-config")["status"] == "succeeded"
                )
                assert daemon.operations.find("old-config")["result"] == {"account": "nemo"}
                assert observed == ["nemo", "nemo"]
            finally:
                release.set()

    asyncio.run(scenario())


def test_four_explicit_tasks_keep_their_snapshot_and_only_same_instance_is_busy(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from maa_planner.daemon.config import Settings
    from maa_planner.daemon.execution import copilot
    from maa_planner.daemon.execution.context import RunResult
    from tests.helpers.execution import request

    requests = [request(tmp_path, f"account-{i}") for i in range(4)]
    bindings = {
        req.binding.profile.name: replace(req.binding, instance_index=i)
        for i, req in enumerate(requests)
    }
    workers = []

    class Worker:
        def __init__(self, request, root):
            self.request = request
            self.release = asyncio.Event()
            self.admitted = False
            workers.append(self)

        async def run(self, admit, phase):
            await self.release.wait()
            self.admitted = await admit()
            return RunResult(self.request.attempt_id, "succeeded" if self.admitted else "cancelled")

        async def cancel(self):
            self.release.set()

    monkeypatch.setattr(copilot, "resolve_binding", lambda settings, target, pure: bindings[target])

    async def scenario():
        async with running(tmp_path, monkeypatch, worker_factory=Worker) as (daemon, client, _):
            settings = Settings(
                daemon.config,
                tuple(b.profile for b in bindings.values()),
                core_runtime=requests[0].core.runtime,
            )
            monkeypatch.setattr(daemon.control_configuration, "settings", lambda **kw: settings)
            before = daemon.configuration
            assert before is not None
            for name in bindings:
                await client.start(
                    "execution.run",
                    profile=name,
                    idempotency_key=name,
                    parameters={"plan": {"tasks": [{"type": "Fight"}]}},
                )
            assert len(daemon.running) == len(workers) == 4
            with pytest.raises(RuntimeError, match="实例已占用"):
                await client.start(
                    "execution.run",
                    profile="account-0",
                    idempotency_key="duplicate-instance",
                    parameters={"plan": {"tasks": [{"type": "Fight"}]}},
                )
            await client.replace_config('[[profiles]]\nname="replacement"\n')
            assert daemon.configuration != before
            for worker in workers:
                assert worker.request.config_revision == before.revision
                worker.release.set()
            await eventually(lambda: not daemon.running)
            assert all(worker.admitted for worker in workers)

    asyncio.run(scenario())


def test_schedule_plan_preview_does_not_start_worker_or_resolve_emulator(tmp_path, monkeypatch):
    from tests.helpers.activity import empty_activity

    async def snapshot(self, settings):
        return empty_activity()

    monkeypatch.setattr(Daemon, "activity_snapshot", snapshot)
    config = tmp_path / "config.toml"
    config.write_text(
        """[[profiles]]
name="nemo"
[[schedules]]
id="preview"
profiles=["nemo"]
times=["12:00"]
enabled=false
[[schedules.tasks]]
type="Fight"
params={stage="1-7",times=2}
""",
        newline="\n",
    )

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            response = await client.start(
                "schedule.plan.preview",
                idempotency_key="preview-1",
                profile="nemo",
                parameters={"schedule_id": "preview"},
            )
            identifier = response["operation_id"]
            await eventually(lambda: daemon.operations.get(identifier)["status"] == "succeeded")
            result = daemon.operations.get(identifier)["result"]
            assert result["tasks"] == [
                {"name": "Fight", "type": "Fight", "params": {"stage": "1-7", "series": 0}}
            ]
            assert result["attachments"] == {} and not result["enabled"]
            assert not daemon.running and not daemon.daily.pending
            assert daemon.activity is None
            bad = await client.start(
                "schedule.plan.preview",
                idempotency_key="preview-2",
                profile="nemo",
                parameters={"schedule_id": "missing"},
            )
            await eventually(
                lambda: daemon.operations.get(bad["operation_id"])["status"] == "failed"
            )
            assert "日程" in daemon.operations.get(bad["operation_id"])["error"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments", [{}, {"schedule_id": ""}, {"schedule_id": 1}, {"schedule_id": "daily", "at": 1}]
)
def test_plan_requires_explicit_schedule_id(arguments):
    from maa_planner.daemon.commands import Submission

    with pytest.raises(ValueError, match="schedule_id"):
        Submission.from_dict(
            {"request_id": "preview", "action": "plan", "target": "nemo", "arguments": arguments}
        )


def test_plan_preview_uses_actual_expert_observation_and_freezes_attachment(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from maa_planner.daemon.config import settings_from_bytes
    from tests.helpers.planning import NOW, skland_base, warehouse

    for mode in ("normal", "three_gold", "orundum", "sell_only"):
        (tmp_path / f"{mode}.json").write_text(json.dumps({"plans": [{"name": mode}]}))
    config = """[[profiles]]
name="nemo"
[[schedules]]
id="preview"
profiles=["nemo"]
times=["12:00"]
[[schedules.tasks]]
type="Infrast"
strategy="expert"
files={normal="normal.json",three_gold="three_gold.json",orundum="orundum.json",sell_only="sell_only.json"}
"""
    settings = settings_from_bytes(config.encode(), tmp_path / "config.toml")
    calls = []

    async def collect(settings, profile):
        calls.append(profile.name)
        return SimpleNamespace(cultivate=warehouse(), player_info=skland_base()), "nemo"

    monkeypatch.setattr("maa_planner.daemon.planning.skland.collect_character_info", collect)
    monkeypatch.setattr(api_module.time, "time", lambda: NOW)
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control")
    daemon.store = StateStore(tmp_path / "preview.sqlite3")
    api = daemon.observations
    result = asyncio.run(
        api.execute("plan", settings, threading.Event(), {"schedule_id": "preview"})
    )
    assert calls == ["nemo"]
    assert result["decisions"][0]["mode"] == "orundum"
    assert result["decisions"][0]["warnings"][0]["item_id"] == "30012"
    assert result["attachments"]["task-0.json"]["plans"][0]["name"] == "orundum"
    assert result["tasks"][0]["params"]["filename"] == "@file:task-0.json"


def test_activity_farming_preview_fetches_inventory_without_emulator_or_core(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from maa_planner.daemon.config import settings_from_bytes
    from tests.helpers.clearance import no_clearance_needed
    from tests.helpers.planning import NOW, farming_activity

    config = """[[profiles]]
name="nemo"
[[schedules]]
id="preview"
profiles=["nemo"]
times=["12:00"]
[[schedules.tasks]]
type="Fight"
strategy="expert"
params={stage="1-7",medicine=0,stone=0,times=30}
"""
    settings = settings_from_bytes(config.encode(), tmp_path / "config.toml")
    calls = []

    async def collect(settings, profile):
        calls.append(profile.name)
        return SimpleNamespace(
            cultivate={"items": [{"id": "31083", "count": 100}, {"id": "31043", "count": 100}]},
            player_info={},
        ), profile.name

    async def activity(self, settings):
        return farming_activity()

    monkeypatch.setattr("maa_planner.daemon.planning.skland.collect_character_info", collect)
    monkeypatch.setattr(Daemon, "activity_snapshot", activity)
    monkeypatch.setattr(api_module.time, "time", lambda: NOW)
    monkeypatch.setattr(
        "maa_planner.daemon.planning.auto_copilot.prepare_automatic", no_clearance_needed
    )
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control")
    daemon.store = StateStore(tmp_path / "state.sqlite3")
    api = daemon.observations
    result = asyncio.run(
        api.execute("plan", settings, threading.Event(), {"schedule_id": "preview"})
    )
    assert calls == ["nemo"]
    assert result["tasks"][0]["params"] == {"stage": "TL-9", "medicine": 0, "stone": 0, "series": 0}
    assert result["farming"][0]["item_id"] == "31113"
    assert result["farming"][0]["stock"] == 0
    assert result["annihilation"] is None
    daemon.store.close()


def test_config_api_changes_fallback_order_and_stock_preview_uses_auto_series(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from maa_planner.daemon.planning.farming import DEFAULT_CHIP_TARGETS
    from tests.helpers.activity import empty_activity
    from tests.helpers.planning import NOW

    config = """[[profiles]]
name="nemo"
[[schedules]]
id="preview"
profiles=["nemo"]
times=["12:00"]
enabled=false
[[schedules.tasks]]
type="Fight"
strategy="expert"
params={times=30,series=0,medicine=0,stone=0}
"""

    async def info(settings, profile):
        return SimpleNamespace(
            cultivate={
                "items": [{"id": item, "count": count} for item, count in DEFAULT_CHIP_TARGETS]
            },
            player_info={},
        ), profile.name

    async def activity(self, settings):
        return empty_activity()

    monkeypatch.setattr("maa_planner.daemon.planning.skland.collect_character_info", info)
    monkeypatch.setattr(Daemon, "activity_snapshot", activity)
    monkeypatch.setattr(time, "time", lambda: NOW)  # Monday; AP-5 opens.

    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            first = await client.call("config.replace", toml=config)
            for index, expected in enumerate(("1-7", "AP-5")):
                await client.start(
                    "schedule.plan.preview",
                    idempotency_key=f"stock-{index}",
                    profile="nemo",
                    parameters={"schedule_id": "preview"},
                )
                await eventually(
                    lambda: daemon.operations.find(f"stock-{index}")["status"] == "succeeded"
                )
                result = daemon.operations.find(f"stock-{index}")["result"]
                assert result["tasks"][0]["params"] == {
                    "stage": expected,
                    "series": 0,
                    "medicine": 0,
                    "stone": 0,
                }
                assert result["farming"][0]["stage"] == expected
                if index == 0:
                    changed = config.replace(
                        'strategy="expert"', 'strategy="expert"\nfallback_stages=["AP-5","1-7"]'
                    )
                    await client.call(
                        "config.replace", toml=changed, expected_revision=first["revision"]
                    )
            effective = await client.call("config.get")
            assert effective["effective"]["schedules"][0]["tasks"][0]["fallback_stages"] == [
                "AP-5",
                "1-7",
            ]
            assert (
                daemon.operations.find("stock-0")["result"]["tasks"][0]["params"]["stage"] == "1-7"
            )
            assert not daemon.running and not daemon.daily.pending

    asyncio.run(scenario())


def test_alert_query_and_events_share_persistent_incident_identity(tmp_path, monkeypatch):
    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            daemon.store.reconcile_alerts(
                "nemo", "infrast_material", {"30012": {"count": 150}}, now=1
            )
            active = await client.call("alert.list", profile="nemo")
            events = await client.call("event.list")
            event = next(e for e in events if e["kind"] == "alert")
            assert active[0]["alert_id"] == event["data"]["alert_id"]
            daemon.store.reconcile_alerts("nemo", "infrast_material", {}, now=2)
            assert await client.call("alert.list", profile="nemo") == []
            assert len(await client.call("alert.list", active_only=False)) == 1
            assert daemon.store.alerts("other") == []

    asyncio.run(scenario())


def test_admin_alert_channel_is_distinct_from_account_alerts(tmp_path, monkeypatch):
    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            daemon.store.observe_alert(
                "admin", "activity_shop_navigation", "event", {"reason": "navigation_failed"}, now=1
            )
            assert await client.call("alert.list", profile="nemo") == []
            alerts = await client.call("alert.list", profile="admin")
            assert alerts[0]["profile"] == "admin"
            assert await client.call("alert.list") == alerts

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "parameters", "persistence"),
    [
        ("emulator.list", {}, "reserve_operation"),
        ("resource.update", {}, "reserve_operation"),
        (
            "execution.run",
            {"profile": "nemo", "plan": {"tasks": [{"type": "Fight"}]}},
            "reserve_operation",
        ),
        ("copilot.run", {"profile": "nemo", "url": "prts://123"}, "reserve_operation"),
        (
            "execution.run",
            {"profile": "nemo", "plan": {"tasks": [{"type": "Fight"}]}},
            "reserve_copilot",
        ),
        ("copilot.run", {"profile": "nemo", "url": "prts://123"}, "reserve_copilot"),
        (
            "execution.batch",
            {
                "jobs": [
                    {
                        "method": "execution.run",
                        "profile": "nemo",
                        "plan": {"tasks": [{"type": "Fight"}]},
                    }
                ]
            },
            "reserve_operation",
        ),
    ],
)
def test_failed_operation_persistence_releases_resources_and_dependency_pin(
    tmp_path, monkeypatch, method, parameters, persistence
):
    from maa_planner.daemon.execution import copilot
    from maa_planner.daemon.execution.context import RunResult
    from tests.helpers.execution import request

    base = request(tmp_path)
    monkeypatch.setattr(copilot, "resolve_binding", lambda *args: base.binding)
    monkeypatch.setattr(api_module, "execute_sync", lambda *args: {})
    monkeypatch.setattr(daemon_module, "preflight_copilot", lambda *args: base.binding)
    original_reserve = getattr(StateStore, persistence)
    failed = False

    def fail_once(store, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("模拟持久化失败")
        return original_reserve(store, *args, **kwargs)

    monkeypatch.setattr(StateStore, persistence, fail_once)

    class Worker:
        def __init__(self, request, root):
            self.request = request

        async def run(self, admit, phase):
            assert await admit()
            return RunResult(self.request.attempt_id, "succeeded")

        async def cancel(self):
            pass

    async def scenario():
        async with running(tmp_path, monkeypatch, worker_factory=Worker) as (daemon, client, _):
            with pytest.raises(RuntimeError, match="模拟持久化失败"):
                await client.call(method, idempotency_key="failed-write", **parameters)
            assert failed
            # A new dependency configuration proves that the failed admission left no pin.
            await client.replace_config(
                '[[profiles]]\nname="nemo"\n[maa_core]\ndirectory="fresh-runtime"\n'
            )
            await client.call(
                "execution.run",
                idempotency_key="fresh-run",
                profile="nemo",
                plan={"tasks": [{"type": "Fight"}]},
            )
            await eventually(lambda: daemon.operations.find("fresh-run")["status"] == "succeeded")
            await client.start("resource.update", idempotency_key="fresh-maintenance")
            await eventually(
                lambda: daemon.operations.find("fresh-maintenance")["status"] == "succeeded"
            )
            assert (await client.call("daemon.status"))["maintenance"] is None

    asyncio.run(scenario())


def test_stage_unconfirm_is_single_stage_and_difficulty_scoped_and_survives_restart(
    tmp_path, monkeypatch
):
    async def scenario():
        params = {"profile": "nemo", "activity_id": "act55side"}
        async with running(tmp_path, monkeypatch) as (_, client, _):
            for stage, raid in (
                ("act55side_01", False),
                ("act55side_ex01", False),
                ("act55side_ex01", True),
                ("act55side_ex02", True),
            ):
                await client.call("stage.confirm", **params, stage_id=stage, is_raid=raid)
            await client.call(
                "stage.confirm",
                profile="nemo",
                activity_id="other_event",
                stage_id="act55side_ex01",
                is_raid=True,
            )
            removed = await client.call(
                "stage.unconfirm", **params, stage_id="act55side_ex01", is_raid=True
            )
            assert removed["removed"] is True
            repeated = await client.call(
                "stage.unconfirm", **params, stage_id="act55side_ex01", is_raid=True
            )
            assert repeated["removed"] is False
            with pytest.raises(RuntimeError, match="is_raid"):
                await client.call(
                    "stage.unconfirm", **params, stage_id="act55side_01", is_raid="false"
                )
            with pytest.raises(RuntimeError, match="未知 profile"):
                await client.call(
                    "stage.unconfirm",
                    profile="unknown",
                    activity_id="act55side",
                    stage_id="act55side_01",
                )
        async with running(tmp_path, monkeypatch) as (_, client, _):
            assert (
                len(
                    await client.call(
                        "stage.progress.get", profile="nemo", activity_id="other_event"
                    )
                )
                == 1
            )
            rows = await client.call("stage.progress.get", **params)
            assert {(v["stage_id"], v["is_raid"]) for v in rows} == {
                ("act55side_01", False),
                ("act55side_ex01", False),
                ("act55side_ex02", True),
            }

    asyncio.run(scenario())

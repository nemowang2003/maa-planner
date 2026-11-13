import asyncio
from dataclasses import replace

import pytest

from maa_planner.daemon import coordinator
from maa_planner.daemon.commands import BatchSubmission
from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.execution import copilot
from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.preparation import PreparedEnvironment
from maa_planner.daemon.state import StateStore
from maa_planner.schema import Request
from tests.helpers.activity import empty_activity
from tests.helpers.async_tools import eventually
from tests.helpers.control import send_message
from tests.helpers.execution import request


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"action": "activities"}],
        [{"action": "run", "target": "nemo"}, {"action": "run", "target": "nemo"}],
    ],
)
def test_batch_rejects_invalid_members(jobs):
    with pytest.raises(ValueError):
        BatchSubmission.from_dict({"request_id": "batch", "jobs": jobs})


def test_batch_and_later_single_share_preparation_until_last_user_finishes(tmp_path, monkeypatch):
    base = request(tmp_path)
    calls, workers = ([], [])

    def prepare(settings, *, pinned=None):
        if pinned is None:
            calls.append(settings)
        return PreparedEnvironment(base.core.runtime, empty_activity())

    monkeypatch.setattr(coordinator, "prepare_environment", prepare)

    def binding(settings, target, require_game=False):
        profile = next((p for p in settings.profiles if p.name == target))
        return replace(
            base.binding,
            profile=profile,
            instance_index=["nemo", "wyf", "zy"].index(target),
            instance_name=target,
        )

    monkeypatch.setattr(copilot, "resolve_binding", binding)
    monkeypatch.setattr(coordinator, "state_directory", lambda: tmp_path / "global/daemon")

    async def scenario():
        gate = asyncio.Event()

        class Worker:
            def __init__(self, request, root):
                self.request = request
                self.cancelled = False
                workers.append(self)

            async def run(self, admit, phase):
                assert await admit()
                await phase("running")
                await gate.wait()
                return RunResult(
                    self.request.attempt_id, "cancelled" if self.cancelled else "succeeded"
                )

            async def cancel(self):
                self.cancelled = True

        config = tmp_path / "config.toml"
        config.write_text(
            "".join((f'[[profiles]]\nname="{name}"\n' for name in ["nemo", "wyf", "zy"])),
            newline="\n",
        )
        daemon = Daemon(
            config,
            tmp_path / "runtime/control.json",
            root=tmp_path / "state",
            worker_factory=Worker,
        )
        task = asyncio.create_task(daemon.run())

        async def call(method, **params):
            response = await send_message(daemon.server.path, Request(method, params).to_dict())
            assert "error" not in response, response
            return response["result"]

        job = {"plan": {"tasks": [{"type": "Award"}]}}
        try:
            await eventually(
                lambda: daemon.configuration is not None and daemon.server.is_serving()
            )
            batch_params = {
                "idempotency_key": "morning",
                "jobs": [
                    dict(job, method="execution.run", profile="nemo"),
                    dict(job, method="execution.run", profile="wyf"),
                ],
            }
            batch = await call("execution.batch", **batch_params)
            await eventually(lambda: len(workers) == 2)
            assert len(calls) == 1
            assert (await call("execution.batch", **batch_params))["operation_id"] == batch[
                "operation_id"
            ]
            assert len(workers) == 2
            await call("execution.run", idempotency_key="extra", **dict(job, profile="zy"))
            await eventually(lambda: len(workers) == 3)
            assert len(calls) == 1
            original = config.read_text()
            await call(
                "config.replace",
                toml=original
                + f'''\n[maa_core]\ndirectory="{(tmp_path / "different").as_posix()}"\n''',
            )
            rejected = await send_message(
                daemon.server.path,
                Request(
                    "execution.run",
                    {"idempotency_key": "different-config", **dict(job, profile="nemo")},
                ).to_dict(),
            )
            assert "运行中的依赖配置不同" in rejected["error"]["message"]
            assert len(calls) == 1 and len(workers) == 3
            await call("config.replace", toml=original)
            await call("operation.cancel", operation_id=batch["result"]["operations"][0])
            assert next((w for w in workers if w.request.binding.profile.name == "nemo")).cancelled
            gate.set()
            await eventually(lambda: not daemon.running)
            result = await call("operation.get", operation_id=batch["operation_id"])
            assert result["status"] == "failed"
            assert sorted((m["status"] for m in result["result"]["members"])) == [
                "cancelled",
                "succeeded",
            ]
            await call("execution.run", idempotency_key="next", **dict(job, profile="nemo"))
            await eventually(lambda: len(workers) == 4)
            assert len(calls) == 2
        finally:
            gate.set()
            daemon.request_stop()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_shared_preparation_failure_never_starts_workers(tmp_path, monkeypatch):
    from maa_planner.daemon.config import Settings
    from maa_planner.daemon.preparation import Preparation

    calls = []

    async def failed(settings):
        calls.append(settings)
        raise OSError("offline")

    async def scenario():
        preparation = Preparation(Settings(tmp_path / "config.toml", ()))
        results = await asyncio.gather(
            *(preparation.get(failed) for _ in range(4)), return_exceptions=True
        )
        assert len(calls) == 1
        assert all(isinstance(result, OSError) for result in results)

    asyncio.run(scenario())


@pytest.mark.parametrize("drain_after_first", [False, True])
def test_late_batch_member_keeps_same_preparation_after_first_member_finishes(
    tmp_path, monkeypatch, drain_after_first
):
    import threading

    base = request(tmp_path)
    first_finished = threading.Event()
    calls = []

    def prepare(settings, *, pinned=None):
        if pinned is None:
            calls.append(settings)
        return PreparedEnvironment(base.core.runtime, empty_activity())

    def binding(settings, target, require_game=False):
        if target == "wyf":
            assert first_finished.wait(3)
        profile = next(p for p in settings.profiles if p.name == target)
        return replace(
            base.binding,
            profile=profile,
            instance_index=0 if target == "nemo" else 1,
            instance_name=target,
        )

    monkeypatch.setattr(coordinator, "prepare_environment", prepare)
    monkeypatch.setattr(copilot, "resolve_binding", binding)
    monkeypatch.setattr(coordinator, "state_directory", lambda: tmp_path / "global/daemon")

    async def scenario():
        class Worker:
            def __init__(self, request, root):
                self.request = request

            async def run(self, admit, phase):
                assert await admit()
                if drain_after_first and self.request.binding.profile.name == "nemo":
                    daemon.request_drain()
                first_finished.set()
                return RunResult(self.request.attempt_id, "succeeded")

            async def cancel(self):
                pass

        config = tmp_path / "config.toml"
        config.write_text('[[profiles]]\nname="nemo"\n[[profiles]]\nname="wyf"\n', newline="\n")
        daemon = Daemon(
            config, tmp_path / "socket/control.json", root=tmp_path / "state", worker_factory=Worker
        )
        task = asyncio.create_task(daemon.run())
        try:
            await eventually(
                lambda: daemon.configuration is not None and daemon.server.is_serving()
            )
            job = {"action": "run", "arguments": {"plan": {"tasks": [{"type": "Award"}]}}}
            result = await daemon.operations.submit_batch(
                {
                    "request_id": "delayed",
                    "jobs": [dict(job, target="nemo"), dict(job, target="wyf")],
                }
            )
            if drain_after_first:
                await asyncio.wait_for(task, 5)
                store = StateStore(tmp_path / "state/state.sqlite3")
                try:
                    operation = store.operation("delayed")
                    assert operation is not None and operation["status"] == "succeeded"
                finally:
                    store.close()
            else:
                await eventually(lambda: daemon.operations.get("delayed")["status"] == "succeeded")
            assert len(calls) == 1
            assert len(result["result"]["operations"]) == 2
        finally:
            first_finished.set()
            daemon.request_stop()
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())

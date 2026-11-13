import asyncio
import json

import pytest

from maa_planner.daemon import daily_control
from maa_planner.daemon.execution.context import RunResult, UnitBinding
from tests.helpers.async_tools import eventually
from tests.helpers.scheduling import Clock, document, running, timestamp, wake


async def call(state, method, **params):
    response = await state.daemon.api.handle({"method": method, "params": params})
    if "error" in response:
        from maa_planner.schema import APIError

        error = response["error"]
        raise APIError(error["code"], error["message"], error["details"])
    return response["result"]


def patch_binding(monkeypatch, root):
    monkeypatch.setattr(
        daily_control,
        "resolve_daily_binding",
        lambda settings, profile: UnitBinding(
            next(p for p in settings.profiles if p.name == profile), root / "mumu", 0, profile
        ),
    )


async def change(state, method, target, *, key="control", **params):
    return await call(
        state,
        method,
        profile=target["profile"],
        occurrence_id=target["occurrence_id"],
        expected_revision=state.daemon.configuration.revision,
        expected_state_version=target["state_version"],
        idempotency_key=key,
        **params,
    )


def test_next_is_read_only_returns_all_tied_profiles_and_skip_preserves_cursor(
    tmp_path, monkeypatch
):
    async def scenario():
        clock = Clock()
        content = (
            document(("nemo",))
            + '\n[[schedules]]\nid="extra"\nprofiles=["nemo"]\ntimes=["12:10"]\n[[schedules.tasks]]\ntype="Recruit"\n'
        )
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            store = state.daemon.store
            before = store.db.total_changes
            result = await call(state, "daily.next", profile="nemo")
            assert len(result["occurrences"]) == 2
            assert store.db.total_changes == before
            assert not store.db.execute("SELECT * FROM schedule_cursors").fetchall()
            target = result["occurrences"][0]
            receipt = await change(state, "daily.skip", target)
            assert receipt["status"] == "succeeded"
            assert not store.db.execute("SELECT * FROM schedule_cursors").fetchall()
            assert len((await call(state, "daily.next", profile="nemo"))["occurrences"]) == 1
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1)
            assert state.workers[0].request.job_id != target["occurrence_id"]
            assert not store.daily_control(target["occurrence_id"])

    asyncio.run(scenario())


def test_postpone_absolute_time_idempotency_versions_and_next_period(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=document(("nemo",))) as state:
            target = (await call(state, "daily.next", profile="nemo"))["occurrences"][0]
            kwargs = dict(
                profile="nemo",
                occurrence_id=target["occurrence_id"],
                expected_revision=state.daemon.configuration.revision,
                expected_state_version=0,
                idempotency_key="once",
                not_before=timestamp("2026-11-02T12:20:00"),
            )
            receipt = await call(state, "daily.postpone", **kwargs)
            assert await call(state, "daily.postpone", **kwargs) == receipt
            with pytest.raises(Exception, match="版本"):
                await change(state, "daily.skip", target, key="stale")
            latest = (await call(state, "daily.next", profile="nemo"))["occurrences"][0]
            assert latest["state_version"] == 1
            with pytest.raises(Exception, match="原下一次"):
                await change(
                    state, "daily.postpone", latest, key="cross", not_before=latest["expires_at"]
                )
            wake(state, clock, "2026-11-02T12:10:00")
            await asyncio.sleep(0.05)
            assert not state.workers
            wake(state, clock, "2026-11-02T12:20:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.running)
            assert (await call(state, "daily.next", profile="nemo"))["occurrences"][0][
                "scheduled_at"
            ] == timestamp("2026-11-02T15:40:00")
            content = state.config.read_text() + "\n# revision\n"
            await state.daemon.replace_configuration(content, state.daemon.configuration.revision)
            assert (await call(state, "daily.postpone", **kwargs))["operation_id"] == receipt[
                "operation_id"
            ]

    asyncio.run(scenario())


def test_temporary_reference_and_inline_execute_and_do_not_change_cycle(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)

    async def scenario():
        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:
            first = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="ref",
                schedule_id="daily",
                expires_at=clock.now() + 1000,
            )
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            assert state.workers[0].request.options.allow_emulator_start
            assert first["result"]["source"] == "temporary"
            assert await call(state, "daily.next", profile="nemo") == {
                "revision": state.daemon.configuration.revision,
                "occurrences": [],
            }
            state.workers[0].finish.set()
            await eventually(
                lambda: state.daemon.operations.get(first["operation_id"])["status"] == "succeeded"
            )
            assert not state.daemon.store.daily_controls()
            second = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="inline",
                inline={"tasks": [{"type": "Recruit"}]},
                expires_at=clock.now() + 1000,
            )
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            assert json.loads(state.workers[1].request.core.tasks.document)["tasks"] == [
                {"name": "Recruit", "type": "Recruit", "params": {}}
            ]
            await call(state, "operation.cancel", operation_id=second["operation_id"])
            await eventually(
                lambda: state.daemon.operations.get(second["operation_id"])["status"] == "cancelled"
            )

    asyncio.run(scenario())


def test_temporary_frozen_files_survive_config_changes_and_waiting_restart(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)
    base = tmp_path / "base.json"
    base.write_text('{"plans":[{"name":"accepted","rooms":{}}]}')

    async def scenario():
        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:
            receipt = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="frozen",
                inline={"tasks": [{"type": "Infrast", "file": "base.json"}]},
                not_before=clock.now() + 600,
                expires_at=clock.now() + 1200,
            )
            assert receipt["status"] == "waiting"
            assert not state.workers and not state.calls
            state.daemon.request_drain()
            await eventually(lambda: state.task.done())
        base.unlink()
        async with running(
            tmp_path, monkeypatch, clock, content='[[profiles]]\nname="nemo"\n'
        ) as state:
            assert state.daemon.operations.get(receipt["operation_id"])["status"] == "waiting"
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            attachments = json.loads(state.workers[0].request.core.tasks.attachments)
            assert attachments["task-0.json"]["plans"][0]["name"] == "accepted"
            state.workers[0].finish.set()
            await eventually(
                lambda: state.daemon.operations.get(receipt["operation_id"])["status"]
                == "succeeded"
            )
            assert not state.daemon.store.db.execute("SELECT * FROM daily_controls").fetchall()
            assert not state.daemon.store.db.execute("SELECT * FROM operations").fetchall()

    asyncio.run(scenario())


def test_temporary_recovery_keeps_operation_active_and_current_business(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)

    async def scenario():
        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:
            receipt = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="recover",
                inline={"tasks": [{"type": "Recruit"}, {"type": "Award"}]},
                expires_at=clock.now() + 5000,
            )
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            req = state.workers[0].request
            state.workers[0].result = RunResult(
                req.attempt_id,
                "failed",
                business_results=(
                    {"id": "task:0", "status": "error"},
                    {"id": "task:1", "status": "pending"},
                ),
            )
            state.workers[0].finish.set()
            await eventually(lambda: len(state.daemon.store.scheduled_retries()) == 1)
            assert state.daemon.operations.get(receipt["operation_id"])["status"] == "waiting"
            state.config.write_text('[[profiles]]\nname="nemo"\n# changed\n')
            await state.daemon._read_configuration()
            wake(state, clock, "2026-11-02T12:05:00")
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            assert [
                t["type"] for t in json.loads(state.workers[1].request.core.tasks.document)["tasks"]
            ] == ["StartUp", "Recruit", "Award"]
            state.workers[1].finish.set()
            await eventually(
                lambda: state.daemon.operations.get(receipt["operation_id"])["status"]
                == "succeeded"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["daily.postpone", "daily.skip", "daily.run"])
def test_daily_protocol_requires_profile_and_finite_time(method):
    from maa_planner.schema import METHODS

    with pytest.raises(ValueError):
        METHODS[method].validate({"idempotency_key": "key"})
    from maa_planner.schema.daily import validate_daily

    for value in (True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            validate_daily(method, {"not_before": value})


def test_concurrent_key_and_committed_notification_failure_preserve_snapshot(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)
    base = tmp_path / "base.json"
    base.write_text('{"plans":[{"name":"accepted","rooms":{}}]}')

    async def scenario():
        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:
            params = dict(
                profile="nemo",
                idempotency_key="concurrent",
                inline={"tasks": [{"type": "Recruit"}]},
                not_before=clock.now() + 600,
                expires_at=clock.now() + 1200,
            )
            first, second = await asyncio.gather(
                call(state, "daily.run", **params), call(state, "daily.run", **params)
            )
            assert first["operation_id"] == second["operation_id"]
            assert len(state.daemon.store.daily_controls()) == 1
            await call(state, "operation.cancel", operation_id=first["operation_id"])
            params.update(
                idempotency_key="fault",
                inline={"tasks": [{"type": "Infrast", "file": "base.json"}]},
            )
            store = state.daemon.store
            original = store.event

            def fail_event(kind, value):
                if kind == "operation":
                    raise OSError("notification unavailable")
                original(kind, value)

            monkeypatch.setattr(store, "event", fail_event)
            with pytest.raises(Exception, match="notification unavailable"):
                await call(state, "daily.run", **params)
            values = store.daily_controls()
            assert len(values) == 1
            frozen = values[0]["definition"]["tasks"][0]["file"]
            from pathlib import Path

            assert Path(frozen).read_bytes() == base.read_bytes()
            monkeypatch.setattr(store, "event", original)
            receipt = await call(state, "daily.run", **params)
            assert len(store.daily_controls()) == 1
        base.unlink()
        async with running(tmp_path, monkeypatch, clock) as state:
            again = await call(state, "daily.run", **params)
            assert again["operation_id"] == receipt["operation_id"]
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            state.workers[0].finish.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["cancel", "expire"])
def test_preparing_temporary_keeps_files_until_preparation_releases(tmp_path, monkeypatch, ending):
    patch_binding(monkeypatch, tmp_path)
    from pathlib import Path

    base = tmp_path / "base.json"
    base.write_text('{"plans":[{"rooms":{}}]}')

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def loader():
            entered.set()
            await release.wait()
            # The cancelled/expired input must remain accessible while a
            # preparation still owns it, even after the original is removed.
            assert list((tmp_path / "state/daily-inputs").rglob("*.json"))

        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False), loader=loader
        ) as state:
            receipt = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="prepare",
                inline={"tasks": [{"type": "Infrast", "file": "base.json"}]},
                expires_at=clock.now() + 60,
            )
            await entered.wait()
            value = state.daemon.store.daily_controls()[0]
            frozen = Path(value["directory"])
            base.unlink()
            if ending == "cancel":
                cancellation = asyncio.create_task(
                    call(state, "operation.cancel", operation_id=receipt["operation_id"])
                )
                await eventually(
                    lambda: state.daemon.operations.get(receipt["operation_id"])["cancel_requested"]
                )
            else:
                wake(state, clock, "2026-11-02T12:02:00")
                await eventually(
                    lambda: state.daemon.store.scheduled_run(value["id"])["status"] == "expired"
                )
            assert frozen.exists()
            assert state.daemon.resources_busy
            assert state.daemon.operations.get(receipt["operation_id"])["status"] not in {
                "cancelled",
                "failed",
                "succeeded",
            }
            release.set()
            if ending == "cancel":
                await cancellation
            expected = "cancelled" if ending == "cancel" else "failed"
            await eventually(
                lambda: state.daemon.operations.get(receipt["operation_id"])["status"] == expected
            )
            assert not state.workers
            await eventually(lambda: not frozen.exists() and not state.daemon.resources_busy)

    asyncio.run(scenario())


def test_control_ownership_and_metadata_during_maintenance(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=document(("a", "b"))) as state:
            target = (await call(state, "daily.next", profile="a"))["occurrences"][0]
            from maa_planner.schema import APIError

            with pytest.raises(APIError) as wrong:
                await call(
                    state,
                    "daily.skip",
                    profile="b",
                    occurrence_id=target["occurrence_id"],
                    expected_revision=state.daemon.configuration.revision,
                    expected_state_version=0,
                    idempotency_key="foreign",
                )
            assert wrong.value.code == "not_found"
            state.daemon._maintenance_owner = "test"
            try:
                assert (await call(state, "daily.next", profile="a"))["occurrences"]
                receipt = await change(
                    state, "daily.postpone", target, not_before=clock.now() + 1200
                )
                assert receipt["status"] == "succeeded"
                revised = (await call(state, "daily.next", profile="a"))["occurrences"][0]
                assert (await change(state, "daily.skip", revised, key="skip-maintenance"))[
                    "status"
                ] == "succeeded"
            finally:
                state.daemon._maintenance_owner = None

    asyncio.run(scenario())


def test_temporary_expert_freezes_all_files_but_reobserves_inventory(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)
    from types import SimpleNamespace

    from maa_planner.daemon import scheduling
    from tests.helpers.planning import skland_base, warehouse

    files = {}
    for mode in ("normal", "three_gold", "orundum", "sell_only"):
        path = tmp_path / f"{mode}.json"
        path.write_text(json.dumps({"plans": [{"name": mode, "rooms": {}}]}))
        files[mode] = path.name

    async def scenario():
        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:
            observed = []

            async def info(settings, user):
                observed.append(clock.now())
                player = skland_base("normal")
                player["status"]["storeTs"] = clock.now() - 50000
                return SimpleNamespace(cultivate=warehouse(), player_info=player), "Doctor"

            monkeypatch.setattr(scheduling, "collect_character_info", info)
            receipt = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="expert",
                inline={"tasks": [{"type": "Infrast", "strategy": "expert", "files": files}]},
                not_before=clock.now() + 600,
                expires_at=clock.now() + 1200,
            )
            assert not observed
            for name in files.values():
                (tmp_path / name).unlink()
            state.config.write_text("invalid TOML [")
            await state.daemon._read_configuration()
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            assert len(observed) == 1
            attachments = json.loads(state.workers[0].request.core.tasks.attachments)
            assert attachments["task-0.json"]["plans"][0]["name"] == "sell_only"
            # Expiry constrains starts, never interrupts admitted work.
            wake(state, clock, "2026-11-02T12:30:00")
            await asyncio.sleep(0.03)
            assert not state.workers[0].cancelled
            state.workers[0].finish.set()
            await eventually(
                lambda: state.daemon.operations.get(receipt["operation_id"])["status"]
                == "succeeded"
            )

    asyncio.run(scenario())


def test_postpone_already_pending_waits_after_other_instance_owner_releases(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)

    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=document(("nemo",))) as state:
            other = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="occupy",
                inline={"tasks": [{"type": "Recruit"}]},
                expires_at=clock.now() + 7200,
            )
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            wake(state, clock, "2026-11-02T12:11:00")
            await eventually(lambda: bool(state.daemon.daily.pending))
            target = (await call(state, "daily.next", profile="nemo"))["occurrences"][0]
            assert target["scheduled_at"] == timestamp("2026-11-02T12:10:00")
            await change(
                state, "daily.postpone", target, not_before=timestamp("2026-11-02T12:30:00")
            )
            wake(state, clock, "2026-11-02T12:12:00")
            state.workers[0].finish.set()
            await eventually(
                lambda: state.daemon.operations.get(other["operation_id"])["status"] == "succeeded"
            )
            await asyncio.sleep(0.03)
            assert len(state.workers) == 1
            wake(state, clock, "2026-11-02T12:30:00")
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            state.workers[1].finish.set()

    asyncio.run(scenario())


def test_daily_key_cannot_race_with_other_operation_method(tmp_path, monkeypatch):
    import threading

    from maa_planner.schema import APIError

    async def scenario():
        clock = Clock()
        entered, release = threading.Event(), threading.Event()

        def binding(settings, profile):
            entered.set()
            assert release.wait(5)
            return UnitBinding(settings.profiles[0], tmp_path / "mumu", 0, profile)

        monkeypatch.setattr(daily_control, "resolve_daily_binding", binding)
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:

            async def observe(*args, **kwargs):
                return []

            monkeypatch.setattr(state.daemon.observations, "execute", observe)
            attempt = asyncio.create_task(
                call(
                    state,
                    "daily.run",
                    profile="nemo",
                    idempotency_key="shared",
                    inline={"tasks": [{"type": "Recruit"}]},
                    not_before=clock.now() + 600,
                    expires_at=clock.now() + 1200,
                )
            )
            try:
                await eventually(entered.is_set)
                winner = await call(
                    state, "emulator.list", profile="nemo", idempotency_key="shared"
                )
            finally:
                release.set()
            with pytest.raises(APIError) as error:
                await attempt
            assert error.value.code == "conflict"
            assert error.value.details["operation_id"] == winner["operation_id"]
            assert not state.daemon.store.daily_controls()
            assert not list((tmp_path / "state/daily-inputs").glob("*"))
            assert state.daemon.store.find_operation_key("shared") == winner["operation_id"]

    asyncio.run(scenario())


def test_temporary_expiry_is_rechecked_after_binding_resolution(tmp_path, monkeypatch):
    import threading

    from maa_planner.schema import APIError

    async def scenario():
        clock = Clock()
        entered, release = threading.Event(), threading.Event()

        def binding(settings, profile):
            entered.set()
            assert release.wait(5)
            return UnitBinding(settings.profiles[0], tmp_path / "mumu", 0, profile)

        monkeypatch.setattr(daily_control, "resolve_daily_binding", binding)
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:
            attempt = asyncio.create_task(
                call(
                    state,
                    "daily.run",
                    profile="nemo",
                    idempotency_key="expired-accept",
                    inline={"tasks": [{"type": "Recruit"}]},
                    expires_at=clock.now() + 1,
                )
            )
            try:
                await eventually(entered.is_set)
                clock.set("2026-11-02T12:00:02")
            finally:
                release.set()
            with pytest.raises(APIError) as error:
                await attempt
            assert error.value.code == "invalid_argument"
            assert not state.daemon.store.daily_controls()
            assert state.daemon.store.find_operation_key("expired-accept") is None
            assert not list((tmp_path / "state/daily-inputs").glob("*"))

    asyncio.run(scenario())


def test_committed_metadata_receipt_is_archived_after_publication_failure(tmp_path, monkeypatch):
    from maa_planner.daemon.state import StateStore

    store = StateStore(tmp_path / "state.sqlite3")

    def fail_event(*args):
        raise OSError("publication failed")

    monkeypatch.setattr(store, "event", fail_event)
    with pytest.raises(OSError, match="publication failed"):
        store.accept_daily_control(
            "receipt",
            {"action": "daily-skip", "idempotency_key": "key"},
            {"id": "target", "kind": "skip"},
            {"occurrence_id": "target"},
            "succeeded",
        )
    assert store.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    store.recover_operations()
    assert store.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert store.find_operation_key("key") == "receipt"
    assert store.daily_control("target")["kind"] == "skip"
    store.close()


def test_temporary_retry_survives_restart_with_invalid_current_config(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)

    async def scenario():
        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:
            receipt = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="retry-invalid-config",
                inline={"tasks": [{"type": "Recruit"}, {"type": "Award"}]},
                expires_at=clock.now() + 5000,
            )
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            worker = state.workers[0]
            worker.result = RunResult(
                worker.request.attempt_id,
                "failed",
                business_results=(
                    {"id": "task:0", "status": "error"},
                    {"id": "task:1", "status": "pending"},
                ),
            )
            worker.finish.set()
            await eventually(lambda: len(state.daemon.store.scheduled_retries()) == 1)
        async with running(tmp_path, monkeypatch, clock, content="invalid TOML [") as state:
            assert state.daemon.configuration is None
            assert state.daemon.config_error is not None
            assert state.daemon.operations.get(receipt["operation_id"])["status"] == "waiting"
            wake(state, clock, "2026-11-02T12:05:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            assert [
                task["type"]
                for task in json.loads(state.workers[0].request.core.tasks.document)["tasks"]
            ] == ["StartUp", "Recruit", "Award"]
            state.workers[0].finish.set()
            await eventually(
                lambda: state.daemon.operations.get(receipt["operation_id"])["status"]
                == "succeeded"
            )

    asyncio.run(scenario())


def test_postpone_waits_for_valid_configuration_after_restart(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        content = document(("nemo",))
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            target = (await call(state, "daily.next", profile="nemo"))["occurrences"][0]
            await change(state, "daily.postpone", target, not_before=clock.now() + 1200)
        async with running(tmp_path, monkeypatch, clock, content="invalid TOML [") as state:
            assert state.daemon.configuration is None
            wake(state, clock, "2026-11-02T12:11:00")
            await asyncio.sleep(0.03)
            assert not state.task.done() and not state.workers
            state.config.write_text(content)
            await state.daemon._read_configuration()
            wake(state, clock, "2026-11-02T12:20:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            state.workers[0].finish.set()

    asyncio.run(scenario())


def test_pause_survives_restart_allows_manual_daily_and_resume_dispatches(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)

    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=document(("nemo",))) as state:
            params = dict(profile="nemo", paused=True, idempotency_key="pause")
            receipt = await call(state, "daily.pause", **params)
            assert receipt["status"] == "succeeded"
            assert (await call(state, "daily.pause", **params))["operation_id"] == receipt[
                "operation_id"
            ]
            wake(state, clock, "2026-11-02T12:11:00")
            await asyncio.sleep(0.05)
            assert state.daemon.daily.pending and not state.workers
        async with running(tmp_path, monkeypatch, clock) as state:
            assert await call(state, "daily.status", profile="nemo") == {
                "profile": "nemo",
                "paused": True,
            }
            manual = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="manual-paused",
                inline={"tasks": [{"type": "Recruit"}]},
                expires_at=clock.now() + 600,
            )
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            state.workers[0].finish.set()
            await eventually(
                lambda: state.daemon.operations.get(manual["operation_id"])["status"] == "succeeded"
            )
            await call(state, "daily.pause", profile="nemo", paused=False, idempotency_key="resume")
            wake(state, clock, "2026-11-02T15:40:00")
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            state.workers[1].finish.set()

    asyncio.run(scenario())


def test_retry_selects_only_failed_business_and_preserves_source(tmp_path, monkeypatch):
    patch_binding(monkeypatch, tmp_path)
    base = tmp_path / "successful-base.json"
    base.write_text('{"plans":[{"rooms":{}}]}')

    async def scenario():
        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("nemo",), enabled=False)
        ) as state:
            receipt = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="source",
                inline={"tasks": [{"type": "Infrast", "file": base.name}, {"type": "Award"}]},
                expires_at=clock.now() + 60,
            )
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            worker = state.workers[0]
            worker.result = RunResult(
                worker.request.attempt_id,
                "failed",
                business_results=(
                    {"id": "task:0", "status": "completed"},
                    {"id": "task:1", "status": "error"},
                ),
            )
            worker.finish.set()
            await eventually(
                lambda: state.daemon.operations.get(receipt["operation_id"])["status"] == "failed"
            )
            source = receipt["result"]["occurrence_id"]
            base.unlink()
        async with running(tmp_path, monkeypatch, clock) as state:
            params = dict(
                profile="nemo",
                occurrence_id=source,
                expected_revision=state.daemon.configuration.revision,
                expires_at=clock.now() + 3600,
            )
            from maa_planner.schema import APIError

            with pytest.raises(APIError, match="证据"):
                await call(
                    state, "daily.retry", **params, task_ids=["task:0"], idempotency_key="bad"
                )
            retry = await call(
                state, "daily.retry", **params, task_ids=["task:1"], idempotency_key="retry"
            )
            repeated = await call(
                state, "daily.retry", **params, task_ids=["task:1"], idempotency_key="retry"
            )
            assert repeated["operation_id"] == retry["operation_id"]
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            doc = json.loads(state.workers[0].request.core.tasks.document)
            assert [t["type"] for t in doc["tasks"]] == ["Award"]
            assert "task:1" in doc["business_ids"] and "task:0" not in doc["business_ids"]
            assert state.daemon.store.scheduled_run(source)["status"] == "failed"
            state.workers[0].finish.set()
            await eventually(
                lambda: state.daemon.operations.get(retry["operation_id"])["status"] == "succeeded"
            )

    asyncio.run(scenario())


def test_retry_rejects_active_recovery_then_cancellation_and_restart_keep_inputs(
    tmp_path, monkeypatch
):
    patch_binding(monkeypatch, tmp_path)

    async def scenario():
        clock = Clock()
        content = document(("nemo",), enabled=False)
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            receipt = await call(
                state,
                "daily.run",
                profile="nemo",
                idempotency_key="source-waiting",
                inline={"tasks": [{"type": "Recruit"}]},
                expires_at=clock.now() + 4000,
            )
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            worker = state.workers[0]
            worker.result = RunResult(
                worker.request.attempt_id,
                "failed",
                business_results=({"id": "task:0", "status": "error"},),
            )
            worker.finish.set()
            await eventually(lambda: bool(state.daemon.store.scheduled_retries()))
            source = receipt["result"]["occurrence_id"]
            params = dict(
                profile="nemo",
                occurrence_id=source,
                expected_revision=state.daemon.configuration.revision,
                task_ids=["task:0"],
                not_before=clock.now() + 5000,
                expires_at=clock.now() + 6000,
            )
            from maa_planner.schema import APIError

            with pytest.raises(APIError, match="终结"):
                await call(state, "daily.retry", **params, idempotency_key="active")
            wake(state, clock, "2026-11-02T13:10:00")
            await eventually(
                lambda: state.daemon.operations.get(receipt["operation_id"])["status"] == "failed"
            )
            future = await call(state, "daily.retry", **params, idempotency_key="future-retry")
        async with running(tmp_path, monkeypatch, clock) as state:
            assert state.daemon.operations.get(future["operation_id"])["status"] == "waiting"
            cancelled = await call(state, "operation.cancel", operation_id=future["operation_id"])
            assert cancelled["status"] == "cancelled"
            assert not state.daemon.store.daily_controls()
            assert not state.workers

    asyncio.run(scenario())

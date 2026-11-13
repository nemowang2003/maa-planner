import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from maa_planner.daemon import scheduling
from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.planning.schedule import ScheduledOccurrence
from maa_planner.daemon.preparation import PreparedEnvironment
from tests.helpers.activity import empty_activity
from tests.helpers.async_tools import eventually
from tests.helpers.planning import skland_base, warehouse
from tests.helpers.scheduling import Clock, document, running, timestamp, wake


def test_timer_starts_four_accounts_with_one_preparation_and_preserved_params(
    tmp_path, monkeypatch
):
    async def scenario():
        clock = Clock("2026-11-02T12:09:59.75")
        async with running(tmp_path, monkeypatch, clock, content=document()) as state:
            clock.realtime = True
            clock.began = time.monotonic()
            state.daemon.wakeup.set()
            await eventually(
                lambda: len(state.workers) == 4 and all(w.allowed for w in state.workers)
            )
            assert len(state.calls) == 1
            for worker in state.workers:
                assert not worker.request.options.allow_emulator_start
                tasks = json.loads(worker.request.core.tasks.document)["tasks"]
                assert [t["type"] for t in tasks] == ["StartUp", "Fight", "CloseDown"]
                assert tasks[1]["params"] == {
                    "stage": "1-7",
                    "medicine": 0,
                    "stone": 0,
                    "expiring_medicine": 999,
                    "medicine_expire_days": 2,
                    "series": 0,
                }
            for _ in range(5):
                state.daemon.wakeup.set()
                await asyncio.sleep(0)
            assert len(state.workers) == 4
            for worker in state.workers:
                worker.finish.set()
            await eventually(lambda: not state.daemon.running)
            for worker in state.workers:
                assert (
                    state.daemon.store.scheduled_run(worker.request.job_id)["status"] == "succeeded"
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("download_failure", [False, True])
def test_clearance_daily_freezes_ledger_and_reports_before_remaining_tasks_finish(
    tmp_path, monkeypatch, download_failure
):
    from dataclasses import asdict

    from maa_planner.daemon.execution.activity_copilot import ClearanceReport, ClearanceResult
    from maa_planner.daemon.planning import auto_copilot
    from maa_planner.daemon.planning.copilot import CopilotTarget
    from maa_planner.schema import StageConfirmation
    from tests.helpers.clearance import clearance
    from tests.helpers.planning import farming_activity

    async def prepare(settings, profile, info, activity, store, *args, **kwargs):
        if download_failure:
            raise OSError("offline scripts")
        prepared = clearance()
        prepared[0]["completed"] = [list(v) for v in store.completed_stages(profile.name, "event")]
        return prepared

    monkeypatch.setattr(auto_copilot, "prepare_automatic", prepare)
    monkeypatch.setattr("tests.helpers.scheduling.empty_activity", farming_activity)

    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        content = document(names=("nemo",))
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            store = state.daemon.store
            store.confirm_stage(StageConfirmation("nemo", "event", "act_01"), now=clock.now())
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            worker = state.workers[0]
            doc = json.loads(worker.request.core.tasks.document)
            if download_failure:
                assert "activity_copilot" not in doc
                assert store.alerts("nemo")[0]["category"] == "activity_copilot_prepare"
            else:
                assert doc["activity_copilot"]["before_task"] == 1
                assert doc["activity_copilot"]["completed"] == [["act_01", False]]
                result = ClearanceReport(
                    "stopped",
                    (
                        ClearanceResult(
                            CopilotTarget(1, "act_01", "EV-EX-1", True, "act_01#f#"),
                            "completed",
                            0,
                            ("callbacks.jsonl:10",),
                        ),
                    ),
                    {"reason": "three_consecutive_defeats", "stage_id": "act_02", "failures": 3},
                )
                report = json.loads(json.dumps({"activity_id": "event", **asdict(result)}))
                state.daemon.daily.clearance_report(worker.request, report)
                assert store.completed_stages("nemo", "event") == {
                    ("act_01", False),
                    ("act_01", True),
                }
                assert store.alerts("nemo")[0]["details"]["reason"] == "three_consecutive_defeats"
                assert (
                    not worker.finish.is_set()
                )  # Alert/progress do not wait for the daily run to end.
                worker.result = RunResult(
                    worker.request.attempt_id, "succeeded", activity_copilot_results=(report,)
                )
            worker.finish.set()
            await eventually(lambda: not state.daemon.running)
            if not download_failure:
                assert (
                    len([e for e in store.events(0, 100) if e["kind"] == "activity_copilot_result"])
                    == 1
                )

    asyncio.run(scenario())


def test_no_activity_skips_copilot_preparation_and_continues_daily(tmp_path, monkeypatch):
    def download(*args):
        pytest.fail("非活动期间不应下载或准备自动 Copilot")

    monkeypatch.setattr("maa_planner.daemon.planning.auto_copilot.prepare_automatic", download)

    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        content = document(names=("a",))
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            doc = json.loads(state.workers[0].request.core.tasks.document)
            assert "activity_copilot" not in doc
            assert not state.daemon.store.alerts("a")
            state.workers[0].finish.set()

    asyncio.run(scenario())


def test_file_watcher_replaces_future_schedule_without_old_timer_starting_it(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=document(("a",))) as state:
            before = state.daemon.configuration.revision
            candidate = tmp_path / "next.toml"
            candidate.write_text(document(("a",), times='["15:40"]'))
            candidate.replace(state.config)
            await eventually(lambda: state.daemon.configuration.revision != before)
            wake(state, clock, "2026-11-02T12:10:00")
            await asyncio.sleep(0.02)
            assert state.workers == []
            wake(state, clock, "2026-11-02T15:40:00")
            await eventually(lambda: len(state.workers) == 1)

    asyncio.run(scenario())


def test_busy_account_waits_for_completion_and_pending_slot_can_expire(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=document(("a",))) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            wake(state, clock, "2026-11-02T15:40:00")
            await eventually(lambda: len(state.daemon.daily.pending) == 1)
            expired = next(iter(state.daemon.daily.pending))
            assert len(state.workers) == 1
            wake(state, clock, "2026-11-02T19:40:00")
            await eventually(
                lambda: state.daemon.store.scheduled_run(expired)["status"] == "expired"
            )
            assert len(state.workers) == 1
            state.workers[0].finish.set()
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            assert state.workers[1].request.job_id != expired

    asyncio.run(scenario())


def test_configuration_change_during_preparation_prevents_runner_start(tmp_path, monkeypatch):
    async def scenario():
        clock, gate = Clock(), asyncio.Event()
        async with running(
            tmp_path, monkeypatch, clock, content=document(), loader=gate.wait
        ) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(
                lambda: len(state.calls) == 1 and len(state.daemon.daily.preparing) == 4
            )
            identifiers = tuple(state.daemon.daily.pending)
            state.config.write_text(document(enabled=False), newline="\n")
            await eventually(lambda: not state.daemon.daily.pending)
            from tests.helpers.control import ApiPeer

            try:
                with pytest.raises(RuntimeError, match="准备"):
                    await ApiPeer(state.daemon.server.path).start(
                        "resource.update", idempotency_key="cancel-still-preparing"
                    )
            finally:
                gate.set()
            await eventually(lambda: not state.daemon.daily.preparing)
            assert state.workers == []
            assert all(
                state.daemon.store.scheduled_run(i)["status"] == "cancelled" for i in identifiers
            )

    asyncio.run(scenario())


def test_admission_rechecks_configuration_after_worker_is_ready(tmp_path, monkeypatch):
    async def scenario():
        clock, gate = Clock(), asyncio.Event()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("a",)), admit_gate=gate
        ) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1)
            old = state.daemon.configuration.revision
            state.config.write_text(document(("a",), enabled=False), newline="\n")
            await eventually(lambda: state.daemon.configuration.revision != old)
            gate.set()
            await eventually(lambda: state.workers[0].allowed is False)
            await eventually(lambda: not state.daemon.running)
            assert (
                state.daemon.store.scheduled_run(state.workers[0].request.job_id)["status"]
                == "cancelled"
            )

    asyncio.run(scenario())


def test_same_instance_aliases_wait_without_concurrent_workers(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        async with running(
            tmp_path, monkeypatch, clock, content=document(("a", "b")), alias=True
        ) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            await eventually(lambda: bool(state.daemon.daily.waiting_units))
            assert len(state.daemon.running) == 1
            state.workers[0].finish.set()
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            assert len(state.daemon.running) == 1

    asyncio.run(scenario())


def test_occurrence_deduplication_survives_daemon_restart(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=document(("a",))) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            identifier = state.workers[0].request.job_id
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.running)
        async with running(tmp_path, monkeypatch, clock) as state:
            await asyncio.sleep(0.03)
            assert state.workers == []
            assert state.daemon.store.scheduled_run(identifier)["status"] == "succeeded"

    asyncio.run(scenario())


def test_no_schedule_has_no_periodic_reconciliation(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content='[[profiles]]\nname="a"') as state:
            assert await state.daemon._reconcile() is None
            assert state.calls == []
            assert state.workers == []

    asyncio.run(scenario())


def test_day_without_enabled_tasks_skips_preparation_entirely(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        content = document(("a",)).replace('type="', 'weekdays=[2]\ntype="')
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            identifier = ScheduledOccurrence(
                "daily", "a", clock.now(), timestamp("2026-11-02T15:40:00")
            ).id
            await eventually(lambda: state.daemon.store.scheduled_run(identifier) is not None)
            assert state.daemon.store.scheduled_run(identifier)["status"] == "skipped"
            assert state.calls == []
            assert state.workers == []

    asyncio.run(scenario())


def test_running_daily_is_not_cancelled_by_configuration_change(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=document(("a",))) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            old = state.daemon.configuration.revision
            state.config.write_text(document(("a",), enabled=False), newline="\n")
            await eventually(lambda: state.daemon.configuration.revision != old)
            assert not state.workers[0].cancelled
            assert len(state.daemon.running) == 1
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.running)
            assert (
                state.daemon.store.scheduled_run(state.workers[0].request.job_id)["status"]
                == "succeeded"
            )

    asyncio.run(scenario())


def test_daily_preparation_blocks_mutating_maintenance_and_shutdown_starts_no_workers(
    tmp_path, monkeypatch
):
    from tests.helpers.control import ApiPeer

    async def scenario():
        clock, gate = Clock(), asyncio.Event()
        async with running(
            tmp_path, monkeypatch, clock, content=document(), loader=gate.wait
        ) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.daemon.daily.preparing) == 4)
            with pytest.raises(RuntimeError, match="准备"):
                await ApiPeer(state.daemon.server.path).start(
                    "resource.update", idempotency_key="maintenance"
                )
            state.daemon.request_stop()
            gate.set()
            await asyncio.wait_for(state.task, 5)
            assert state.workers == []

    asyncio.run(scenario())


def test_expert_fetch_failure_is_isolated_and_success_emits_decision_event(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        files = {}
        for mode in ("normal", "three_gold", "orundum", "sell_only"):
            path = tmp_path / f"{mode}.json"
            path.write_text('{"plans": [{}]}')
            files[mode] = str(path)
        content = (
            document(("a", "b"))
            + '\n[[schedules.tasks]]\ntype="Infrast"\nstrategy="expert"\n[schedules.tasks.files]\n'
        )
        content += "\n".join(f"{key}={json.dumps(value)}" for key, value in files.items())
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            state.daemon.store.reconcile_alerts(
                "a", "infrast_material", {"30012": {"count": 100}}, now=clock.now()
            )

            async def info(settings, user):
                if user.name == "a":
                    raise ValueError("认证文件缺失")
                player = skland_base()
                player["status"]["storeTs"] = clock.now() - 60
                return SimpleNamespace(cultivate=warehouse(), player_info=player), "Doctor"

            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            failed = ScheduledOccurrence(
                "daily", "a", clock.now(), timestamp("2026-11-02T15:40:00")
            ).id
            await eventually(
                lambda: (
                    len(state.workers) == 1
                    and state.daemon.store.scheduled_run(failed)["status"] == "failed"
                )
            )
            assert state.workers[0].request.binding.profile.name == "b"
            events = [e for e in state.daemon.store.events(0) if e["kind"] == "infrast_decision"]
            assert len(events) == 1
            assert events[0]["data"]["mode"] == "orundum"
            assert events[0]["data"]["warnings"][0]["item_id"] == "30012"
            alerts = state.daemon.store.alerts("b")
            assert state.daemon.store.alerts("a")[0]["details"]["count"] == 100
            skland = next(
                alert
                for alert in state.daemon.store.alerts("a")
                if alert["category"] == "skland_prepare"
            )
            assert skland["subject"] == "daily"
            assert len(alerts) == 1 and alerts[0]["profile"] == "b"
            assert alerts[0]["subject"] == "30012"
            assert alerts[0]["details"]["count"] == 150
            assert len(state.calls) == 1

    asyncio.run(scenario())


def test_skland_prepare_alert_resolves_after_successful_collection(tmp_path, monkeypatch):
    content = document(("a",)).replace('type="Fight"', 'type="Fight"\nstrategy="expert"')
    content = content.replace('times=["12:10", "15:40", "19:40"]', 'times=["12:10", "15:40"]')

    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            state.daemon.store.observe_alert(
                "a", "skland_prepare", "daily", {"reason": "旧错误"}, now=clock.now()
            )

            async def info(settings, user):
                return SimpleNamespace(
                    cultivate={"items": [{"id": "31113", "count": 1}]}, player_info={}
                ), user.name

            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            assert not [
                alert
                for alert in state.daemon.store.alerts("a")
                if alert["category"] == "skland_prepare"
            ]
            state.workers[0].finish.set()

    asyncio.run(scenario())


def test_activity_farming_requires_inventory_and_freezes_account_specific_choices(
    tmp_path, monkeypatch
):
    from tests.helpers.clearance import no_clearance_needed
    from tests.helpers.planning import farming_activity

    monkeypatch.setattr(
        "maa_planner.daemon.planning.auto_copilot.prepare_automatic", no_clearance_needed
    )

    async def scenario():
        clock = Clock()
        content = document().replace('type="Fight"', 'type="Fight"\nstrategy="expert"')
        monkeypatch.setattr("tests.helpers.scheduling.empty_activity", farming_activity)
        async with running(tmp_path, monkeypatch, clock, content=content) as state:

            async def info(settings, user):
                if user.name == "a":
                    raise ValueError("库存请求失败")
                cultivate = {
                    "b": {},
                    "c": {"items": [{"id": "31083", "count": 100}, {"id": "31043", "count": 100}]},
                    "d": {
                        "items": [
                            {"id": "31113", "count": "120"},
                            {"id": "31083", "count": "99"},
                            {"id": "31043", "count": "10"},
                        ]
                    },
                }[user.name]
                # Farming must not depend on an infrastructure layout or weekly quota.
                return SimpleNamespace(cultivate=cultivate, player_info={}), user.name

            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 2)
            failed_ids = [
                ScheduledOccurrence("daily", name, clock.now(), timestamp("2026-11-02T15:40:00")).id
                for name in ("a", "b")
            ]
            await eventually(
                lambda: all(
                    state.daemon.store.scheduled_run(identifier)["status"] == "failed"
                    for identifier in failed_ids
                )
            )
            selected = {}
            for worker in state.workers:
                tasks = json.loads(worker.request.core.tasks.document)["tasks"]
                params = tasks[1]["params"]
                selected[worker.request.binding.profile.name] = params["stage"]
                assert params["medicine"] == 0 and params["stone"] == 0
                worker.finish.set()
            assert selected == {"c": "TL-9", "d": "TL-7"}
            decisions = [
                e["data"] for e in state.daemon.store.events(0) if e["kind"] == "farming_decision"
            ]
            assert {d["profile"]: d["stage"] for d in decisions} == selected
            assert len(state.calls) == 1

    asyncio.run(scenario())


def test_weekly_annihilation_retry_completion_and_restart_are_account_week_scoped(
    tmp_path, monkeypatch
):
    from maa_planner.daemon.execution.annihilation import AnnihilationResult

    content = """[[profiles]]
name="a"
[[schedules]]
id="daily"
profiles=["a"]
times=["12:10","15:40","19:40"]
session={startup={}}
[[schedules.tasks]]
name="weekly"
type="Fight"
strategy="expert"
params={stage="Annihilation",times=5,medicine=0,stone=0}
[[schedules.tasks]]
name="normal"
type="Fight"
params={stage="1-7",times=1}
"""

    def has_weekly(worker):
        return any(
            t["name"] == "weekly" for t in json.loads(worker.request.core.tasks.document)["tasks"]
        )

    async def info(*args):
        return SimpleNamespace(
            player_info={
                "status": {"storeTs": timestamp("2026-11-01T12:00:00")},
                "campaign": {"reward": {"current": 1800, "total": 1800}},
            }
        ), "a"

    async def finish(state, worker):
        worker.finish.set()
        await eventually(lambda: not state.daemon.running)

    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            first = state.workers[0]
            assert has_weekly(first)  # Last week's 1800 does not complete this week.
            first.result = RunResult(first.request.attempt_id, "succeeded")  # No quota evidence.
            await finish(state, first)
            wake(state, clock, "2026-11-02T15:40:00")
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            assert has_weekly(state.workers[1])
            await finish(state, state.workers[1])
            wake(state, clock, "2026-11-03T12:10:00")
            await eventually(lambda: len(state.workers) == 3 and state.workers[2].allowed)
            second = state.workers[2]
            assert has_weekly(second)
            second.result = RunResult(
                second.request.attempt_id,
                "succeeded",
                annihilation_results=(
                    AnnihilationResult(
                        2,
                        "Annihilation",
                        "completed",
                        "weekly_quota",
                        1800,
                        1800,
                        clock.now(),
                        ("callbacks.jsonl:1",),
                    ),
                ),
            )
            await finish(state, second)
            assert not state.daemon.store.db.execute(
                "SELECT name FROM sqlite_master WHERE name='weekly_annihilation'"
            ).fetchall()
        clock.set("2026-11-04T12:00:00")
        async with running(tmp_path, monkeypatch, clock) as restarted:

            async def refreshed_info(*args):
                data, uid = await info(*args)
                data.player_info["status"]["storeTs"] = timestamp("2026-11-03T12:00:00")
                return data, uid

            monkeypatch.setattr(scheduling, "collect_character_info", refreshed_info)
            wake(restarted, clock, "2026-11-04T12:10:00")
            await eventually(lambda: len(restarted.workers) == 1 and restarted.workers[0].allowed)
            assert not has_weekly(restarted.workers[0])
            await finish(restarted, restarted.workers[0])
            wake(restarted, clock, "2026-11-09T12:10:00")
            await eventually(lambda: len(restarted.workers) == 2 and restarted.workers[1].allowed)
            assert has_weekly(restarted.workers[1])
            await finish(restarted, restarted.workers[1])

    asyncio.run(scenario())


@pytest.mark.parametrize("missing_weekly", [False, True])
@pytest.mark.parametrize("original_days", [14, 21])
def test_weekly_scheduler_joins_original_history_without_changing_shared_activity(
    tmp_path, monkeypatch, original_days, missing_weekly
):
    import threading
    from dataclasses import replace
    from datetime import datetime

    from maa_planner.daemon.config import settings_from_bytes
    from maa_planner.daemon.constants import BUSINESS_TIMEZONE
    from maa_planner.daemon.planning.activity import SideStory
    from tests.helpers.activity_history import history
    from tests.helpers.clearance import no_clearance_needed

    monkeypatch.setattr(
        "maa_planner.daemon.planning.auto_copilot.prepare_automatic", no_clearance_needed
    )

    content = """[[profiles]]
name="a"
[[schedules]]
id="daily"
profiles=["a"]
times=["12:10","19:40"]
[[schedules.tasks]]
name="weekly"
type="Fight"
strategy="expert"
params={stage="Annihilation",times=5,medicine=0,stone=0}
[[schedules.tasks]]
name="normal"
type="Fight"
params={stage="1-7",times=1}
"""

    async def scenario():
        clock = Clock()
        public = empty_activity()
        reopen = SideStory(
            key="SSReopen",
            name="墟",
            tip="",
            stages=(),
            minimum_required=None,
            starts_at=datetime(2026, 10, 25, 16, tzinfo=BUSINESS_TIMEZONE),
            ends_at=datetime(2026, 11, 8, 3, 59, 59, tzinfo=BUSINESS_TIMEZONE),
            timezone_offset=8,
        )
        public = replace(public, data=replace(public.data, side_stories=(reopen,)))
        async with running(tmp_path, monkeypatch, clock, content=content) as state:

            async def prepare(settings, *, pinned=None):
                return PreparedEnvironment(settings.runtime_template(), public)

            async def info(settings, profile):
                data = history(
                    end="2025-08-23T03:59:59" if original_days == 21 else "2025-08-16T03:59:59"
                )
                data.update(
                    status={"storeTs": clock.now()},
                    campaign={"reward": {"current": 0, "total": 1800}},
                )
                if missing_weekly:
                    data.pop("campaign")
                return SimpleNamespace(player_info=data), "a"

            monkeypatch.setattr(state.daemon, "prepare_dependencies", prepare)
            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)

            async def preview_activity(settings):
                return public

            monkeypatch.setattr(state.daemon.observations, "activity_snapshot", preview_activity)
            monkeypatch.setattr("maa_planner.daemon.planning.skland.collect_character_info", info)
            preview = await state.daemon.observations.execute(
                "plan",
                settings_from_bytes(content.encode(), state.config),
                threading.Event(),
                {"schedule_id": "daily"},
            )
            tasks = json.loads(state.workers[0].request.core.tasks.document)["tasks"]
            assert preview["tasks"] == tasks
            assert preview["annihilation"]["completed"] is False
            assert any(t["name"] == "weekly" for t in tasks) is (original_days == 14)
            assert public.data.side_stories[0].original is None
            decision = next(
                e["data"]
                for e in state.daemon.store.events(0)
                if e["kind"] == "annihilation_decision"
            )
            assert decision["history_unknown"] is False
            if original_days == 21:
                assert decision["reopen_original_ids"] == ["act44side"]
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.running)
            wake(state, clock, "2026-11-08T12:10:00")
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            after = json.loads(state.workers[1].request.core.tasks.document)["tasks"]
            assert any(t["name"] == "weekly" for t in after)
            state.workers[1].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())


def test_farming_recovery_reobserves_inventory_researches_and_reads_only_certified_ledger(
    tmp_path, monkeypatch
):
    from dataclasses import asdict

    from maa_planner.daemon.execution.activity_copilot import ClearanceReport, ClearanceResult
    from maa_planner.daemon.planning.copilot import CopilotTarget
    from tests.helpers.clearance import clearance
    from tests.helpers.planning import farming_activity

    inventories = [{"31113": 1, "31083": 20, "31043": 30}, {"31113": 80, "31083": 2, "31043": 30}]
    observed = []
    searched = []

    async def info(settings, profile):
        depot = inventories[min(len(observed), 1)]
        observed.append(dict(depot))
        return SimpleNamespace(
            cultivate={"items": [{"id": k, "count": v} for k, v in depot.items()]}, player_info={}
        ), profile.name

    async def automatic(settings, profile, info, activity, store, *args, **kwargs):
        known = store.completed_stages(profile.name, "event")
        searched.append(known)
        prepared = clearance()
        prepared[0]["completed"] = [list(v) for v in sorted(known)]
        return prepared

    monkeypatch.setattr("tests.helpers.scheduling.empty_activity", farming_activity)
    monkeypatch.setattr("maa_planner.daemon.planning.auto_copilot.prepare_automatic", automatic)

    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        content = document(("nemo",)).replace('type="Fight"', 'type="Fight"\nstrategy="expert"')
        content += '\n[[schedules.tasks]]\ntype="Award"\n'
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            first = state.workers[0]
            old = json.loads(first.request.core.tasks.document)
            assert old["tasks"][1]["params"]["stage"] == "TL-9"
            certified = ClearanceReport(
                "running",
                (
                    ClearanceResult(
                        CopilotTarget(1, "act_01", "EV-EX-1", False, "act_01"),
                        "completed",
                        0,
                        ("callbacks.jsonl:10",),
                    ),
                ),
            )
            state.daemon.daily.clearance_report(
                first.request, json.loads(json.dumps({"activity_id": "event", **asdict(certified)}))
            )
            first.result = RunResult(
                first.request.attempt_id,
                "failed",
                business_results=(
                    {"id": "lifecycle:startup", "status": "completed"},
                    {
                        "id": "task:0",
                        "status": "error",
                        "error": {"kind": "core", "evidence": "callbacks.jsonl:20"},
                    },
                    {"id": "task:1", "status": "completed"},
                    {"id": "lifecycle:closedown", "status": "completed"},
                ),
            )
            first.finish.set()
            await eventually(lambda: not state.daemon.running)
            retry = state.daemon.store.scheduled_retries()[0]
            assert "request" not in retry
            wake(state, clock, "2026-11-02T12:15:00")
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            fresh = json.loads(state.workers[1].request.core.tasks.document)
            assert [t["type"] for t in fresh["tasks"]] == ["StartUp", "Fight", "CloseDown"]
            assert fresh["tasks"][1]["params"]["stage"] == "TL-8"
            assert fresh["activity_copilot"]["completed"] == [["act_01", False]]
            assert searched == [frozenset(), frozenset({("act_01", False)})]
            assert len(observed) == 2 and len(state.calls) == 2
            state.workers[1].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())


def test_nine_colored_deer_warns_and_skips_processing_without_skland_or_recipe_requests(
    tmp_path, monkeypatch
):
    async def scenario():
        clock = Clock("2026-11-02T12:09:59")
        content = document(names=("nemo",)).replace(
            'type="Fight"\nparams={stage="1-7", medicine=0, stone=0, expiring_medicine=999, medicine_expire_days=2}',
            """type="Infrast"
params={facility=["Mfg"]}
processing={operators=["九色鹿"],targets={T3=20,T4=10}}""",
        )
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            plan = json.loads(state.workers[0].request.core.tasks.document)
            assert [t["type"] for t in plan["tasks"]] == ["StartUp", "Infrast", "CloseDown"]
            assert "infrast_processing" not in plan
            assert [
                (a["category"], a["details"]["reason"]) for a in state.daemon.store.alerts("nemo")
            ] == [("infrast_processing", "unsupported_operator")]
            state.workers[0].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())

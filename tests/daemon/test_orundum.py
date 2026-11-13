import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from maa_planner.daemon.config import settings_from_bytes
from maa_planner.daemon.daily_preparation import DailyEvidence
from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.execution.runtime import CoreRun
from maa_planner.daemon.orundum import OrundumIntents, check_observation
from maa_planner.daemon.planning.daily import InfrastObservation, build_daily_plan
from maa_planner.daemon.planning.expert import InfrastMode, ShardRecipe
from maa_planner.daemon.state import StateStore
from maa_planner.schema import METHODS, APIError
from tests.helpers.execution import request
from tests.helpers.orundum import native_plan
from tests.helpers.planning import NOW, skland_base, warehouse
from tests.helpers.scheduling import Clock, running


def configuration(tmp_path):
    for mode in ("normal", "three_gold", "orundum", "sell_only"):
        (tmp_path / f"{mode}.json").write_text(json.dumps(native_plan(mode)))
    return """[[profiles]]
name="nemo"
[[schedules]]
id="daily"
profiles=["nemo"]
times=["12:10"]
[[schedules.tasks]]
type="Infrast"
strategy="expert"
files={normal="normal.json",three_gold="three_gold.json",orundum="orundum.json",sell_only="sell_only.json"}
"""


def test_manual_entry_selects_file_without_threshold_or_synthetic_current_recipe(tmp_path):
    settings = settings_from_bytes(configuration(tmp_path).encode(), tmp_path / "config.toml")
    # Inventory is parsed, but there are no material quantities or a current shard recipe.
    observation = InfrastObservation({}, InfrastMode.NORMAL, None, NOW)
    plan = build_daily_plan(
        settings.schedules[0], now=NOW, observation=observation, manual_entry="intent"
    )
    assert plan.decisions[0].mode == InfrastMode.ORUNDUM
    assert plan.decisions[0].file.name == "orundum.json"
    assert observation.mode == InfrastMode.NORMAL and observation.recipe is None
    assert plan.tasks is not None
    frozen = json.loads(plan.tasks.document)
    assert len(frozen["infrast_orundum"]["targets"]) == 6
    assert frozen["infrast_orundum"]["business_id"] == "task:0"
    with pytest.raises(ValueError, match="缺少"):
        build_daily_plan(settings.schedules[0], now=NOW, observation=observation)


@pytest.mark.parametrize("ending", ["applied", "failed", "crashed", "unknown", "archived"])
def test_intent_lifecycle_uses_business_evidence_and_survives_restart(tmp_path, ending):
    settings = settings_from_bytes(configuration(tmp_path).encode(), tmp_path / "config.toml")
    store = StateStore(tmp_path / "state.sqlite3")
    service = OrundumIntents(SimpleNamespace(store=store, attempts={"attempt-nemo": None}))
    first = service.register(settings, profile="nemo", key="first")
    intent_id = first["result"]["intent_id"]
    assert (
        service.register(settings, profile="nemo", key="second")["result"]["intent_id"] == intent_id
    )
    observation = InfrastObservation({}, InfrastMode.NORMAL, None, NOW)
    plan = build_daily_plan(
        settings.schedules[0], now=NOW, observation=observation, manual_entry=intent_id
    )
    original = request(tmp_path)
    assert original.core is not None and plan.tasks is not None
    actual = replace(original, core=CoreRun(original.core.runtime, plan.tasks))
    store.enqueue(actual)
    store.start(actual.attempt_id)
    store.prepared_request(actual)
    query = {"intent_id": intent_id, "business_id": "task:0"}
    assert service.start(actual, query)
    assert not service.start(actual, query)
    report = {
        **query,
        "task_id": 7,
        "mode": "orundum",
        "outcome": "applied",
        "evidence": [f"callbacks:{i}" for i in range(6)],
    }
    result = RunResult(
        actual.attempt_id, "failed", business_results=({"id": "task:0", "status": "error"},)
    )
    if ending == "applied":
        service.applied(actual, report)
        # A later report is harmless even after another manual intent is registered.
        service.applied(actual, report)
        service.finished(result)
        assert service.get("nemo") is None
        applied_at = store.db.execute(
            "SELECT applied_at FROM orundum_observation_wait WHERE profile='nemo'"
        ).fetchone()[0]
        old = DailyEvidence(
            (), observation=InfrastObservation({}, InfrastMode.NORMAL, None, applied_at - 1)
        )
        with pytest.raises(ValueError, match="早于"):
            check_observation(store, "nemo", old)
        fresh = InfrastObservation(
            {"30012": 0, "4001": 0, "3141": 0, "3003": 0},
            InfrastMode.ORUNDUM,
            ShardRecipe.ORIROCK,
            applied_at,
        )
        old.observation = fresh
        check_observation(store, "nemo", old)
        exited = build_daily_plan(settings.schedules[0], now=NOW, observation=fresh)
        assert exited.decisions[0].mode == InfrastMode.THREE_GOLD
        assert exited.tasks is not None
        assert "infrast_orundum" not in json.loads(exited.tasks.document)
        assert (
            service.register(settings, profile="nemo", key="first")["result"]["intent_id"]
            == intent_id
        )
        assert service.get("nemo") is None
        newer = service.register(settings, profile="nemo", key="third")["result"]["intent_id"]
        service.applied(actual, report)
        assert service.get("nemo")["intent_id"] == newer
    elif ending == "failed":
        service.finished(result)
        assert service.get("nemo")["status"] == "pending"
        assert service.manual_entry("nemo") == intent_id
    elif ending == "unknown":
        service.finished(replace(result, business_results=({"id": "task:0", "status": "unknown"},)))
        assert service.get("nemo")["status"] == "uncertain"
    elif ending == "archived":
        final = replace(result, orundum_results=(report,))
        store.finish(final)
        service.finished(final)
        assert service.get("nemo") is None
    store.close()
    reopened = StateStore(tmp_path / "state.sqlite3")
    recovered = OrundumIntents(SimpleNamespace(store=reopened))
    recovered.recover()
    if ending == "crashed":
        assert recovered.get("nemo")["status"] == "uncertain"
        assert recovered.manual_entry("nemo") is None
    elif ending == "failed":
        assert recovered.get("nemo")["status"] == "pending"
    elif ending == "archived":
        assert recovered.get("nemo") is None
    reopened.close()


def test_applied_mode_waits_for_post_application_skland_before_leaving(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    with store.db:
        store.db.execute("INSERT INTO orundum_observation_wait VALUES (?,?)", ("nemo", NOW))
    old = DailyEvidence((), observation=InfrastObservation({}, InfrastMode.NORMAL, None, NOW - 60))
    with pytest.raises(ValueError, match="早于"):
        check_observation(store, "nemo", old)
    # New explicit entry and actual orundum observations do not invent recipes or thresholds.
    check_observation(store, "nemo", old, manual_entry="new-intent")
    assert old.observation is not None
    old.observation = replace(old.observation, mode=InfrastMode.ORUNDUM)
    check_observation(store, "nemo", old)
    current = replace(old.observation, mode=InfrastMode.SELL_ONLY, store_ts=NOW)
    old.observation = current
    check_observation(store, "nemo", old, clear=False)
    assert store.db.execute("SELECT count(*) FROM orundum_observation_wait").fetchone()[0] == 1
    check_observation(store, "nemo", old)
    assert not store.db.execute("SELECT * FROM orundum_observation_wait").fetchall()
    store.close()


def test_api_registers_while_maintenance_busy_and_preview_does_not_bind(tmp_path, monkeypatch):
    content = configuration(tmp_path)
    for method in ("infrast.orundum.request", "infrast.orundum.get"):
        assert METHODS[method].profile == "required"
        with pytest.raises(ValueError):
            METHODS[method].validate({"idempotency_key": "key"})

    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            api, service = state.daemon.api, state.daemon.daily.orundum
            monkeypatch.setattr(
                type(state.daemon.operations),
                "maintenance",
                property(lambda _: "maintenance"),
                raising=False,
            )
            response = await api.handle(
                {
                    "method": "infrast.orundum.request",
                    "params": {"profile": "nemo", "idempotency_key": "key"},
                }
            )
            assert "error" not in response
            operation = response["result"]
            assert operation["status"] == "succeeded"
            assert operation["result"]["status"] == "pending"
            intent = service.get("nemo")
            assert not state.workers

            async def info(*args):
                player = skland_base("normal")
                player["status"]["storeTs"] = clock.now() - 60
                return SimpleNamespace(cultivate=warehouse(), player_info=player), "nemo"

            monkeypatch.setattr("maa_planner.daemon.planning.skland.collect_character_info", info)
            settings = settings_from_bytes(content.encode(), state.config)
            preview = await state.daemon.observations.execute(
                "plan", settings, threading.Event(), {"schedule_id": "daily"}
            )
            assert preview["decisions"][0]["mode"] == "orundum"
            assert service.get("nemo") == intent and not state.workers
            assert not state.daemon.daily.pending
            read = await api.handle(
                {"method": "infrast.orundum.get", "params": {"profile": "nemo"}}
            )
            assert read["result"] == intent
            duplicate = await api.handle(
                {
                    "method": "infrast.orundum.request",
                    "params": {"profile": "nemo", "idempotency_key": "key"},
                }
            )
            assert duplicate["result"]["operation_id"] == operation["operation_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["sql", "publish"])
def test_registration_commits_intent_and_receipt_together(tmp_path, monkeypatch, fault):
    import sqlite3

    settings = settings_from_bytes(configuration(tmp_path).encode(), tmp_path / "config.toml")
    store = StateStore(tmp_path / "state.sqlite3")
    service = OrundumIntents(SimpleNamespace(store=store))
    if fault == "sql":
        with store.db:
            store.db.execute(
                "CREATE TRIGGER reject_operation BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT,'test rollback'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            service.register(settings, profile="nemo", key="first")
        assert service.get("nemo") is None
        assert store.find_operation_key("first") is None
    else:

        def failed(*args, **kwargs):
            raise OSError("lost after commit")

        monkeypatch.setattr(store, "operation_update", failed)
        with pytest.raises(OSError):
            service.register(settings, profile="nemo", key="first")
        value = service.get("nemo")
        repeated = service.register(settings, profile="nemo", key="first")
        assert repeated["result"] == value and repeated["status"] == "succeeded"
    store.close()


def test_no_expert_and_cross_profile_idempotency_do_not_register(tmp_path):
    settings = settings_from_bytes(configuration(tmp_path).encode(), tmp_path / "config.toml")
    store = StateStore(tmp_path / "state.sqlite3")
    service = OrundumIntents(SimpleNamespace(store=store))
    with pytest.raises(ValueError, match="expert"):
        service.register(replace(settings, schedules=()), profile="nemo", key="first")
    service.register(settings, profile="nemo", key="first")
    with pytest.raises(APIError, match="参数不同"):
        service.register(settings, profile="other", key="first")
    assert service.get("other") is None
    store.close()


@pytest.mark.parametrize("applied", [False, True])
def test_daily_business_retry_keeps_pending_entry_but_never_reenters_consumed_intent(
    tmp_path, monkeypatch, applied
):
    from maa_planner.daemon import scheduling
    from tests.helpers.async_tools import eventually
    from tests.helpers.scheduling import wake

    content = configuration(tmp_path)

    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            service = state.daemon.daily.orundum
            service.register(
                settings_from_bytes(content.encode(), state.config), profile="nemo", key="key"
            )
            intent_id = service.get("nemo")["intent_id"]
            exited = False

            async def info(*args):
                player = skland_base("orundum" if exited else "normal")
                player["status"]["storeTs"] = clock.now()
                cultivate = warehouse()
                if exited:
                    for item in cultivate["items"]:
                        if item["id"] in {"30012", "4001", "3141"}:
                            item["count"] = 0
                return SimpleNamespace(player_info=player, cultivate=cultivate), "nemo"

            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            worker = state.workers[0]
            original = worker.request
            query = {"intent_id": intent_id, "business_id": "task:0"}
            future = asyncio.get_running_loop().create_future()
            await state.daemon._changes.put(("orundum_start", (original, query, future)))
            state.daemon.wakeup.set()
            assert await asyncio.wait_for(future, 3)
            assert service.get("nemo")["status"] == "applying"
            if applied:
                report = {
                    **query,
                    "task_id": 7,
                    "mode": "orundum",
                    "outcome": "applied",
                    "evidence": [f"callbacks:{i}" for i in range(6)],
                }
                await state.daemon._changes.put(("orundum_applied", (original, report)))
                state.daemon.wakeup.set()
                await eventually(lambda: service.get("nemo") is None)
            worker.result = RunResult(
                original.attempt_id,
                "failed",
                business_results=(
                    {"id": "task:0", "status": "error", "error": {"kind": "orundum_production"}},
                ),
            )
            worker.finish.set()
            await eventually(
                lambda: not state.daemon.running
                and state.daemon.daily.retries.current(original.job_id)["status"] == "waiting"
            )
            retry = state.daemon.daily.retries.current(original.job_id)
            assert retry["pending"] == ["task:0"]
            exited = applied
            clock.value = retry["due_at"]
            state.daemon.wakeup.set()
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            plan = json.loads(state.workers[1].request.core.tasks.document)
            assert ("infrast_orundum" in plan) is (not applied)
            if not applied:
                assert plan["infrast_orundum"]["intent_id"] == intent_id
                assert service.get("nemo")["status"] == "pending"
            else:
                assert service.get("nemo") is None
                assert not state.daemon.store.db.execute(
                    "SELECT * FROM orundum_observation_wait"
                ).fetchall()
            state.workers[1].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())


def test_logged_application_with_uncommitted_sql_cleanup_is_not_reentered(tmp_path, monkeypatch):
    settings = settings_from_bytes(configuration(tmp_path).encode(), tmp_path / "config.toml")
    store = StateStore(tmp_path / "state.sqlite3")
    service = OrundumIntents(SimpleNamespace(store=store))
    operation = service.register(settings, profile="nemo", key="key")
    value = operation["result"]
    service.save({**value, "status": "applying", "attempt_id": "attempt", "business_id": "task:0"})
    report = {
        "intent_id": value["intent_id"],
        "business_id": "task:0",
        "task_id": 7,
        "outcome": "applied",
        "mode": "orundum",
        "evidence": [f"callback:{i}" for i in range(6)],
    }
    actual = SimpleNamespace(
        attempt_id="attempt", binding=SimpleNamespace(profile=SimpleNamespace(name="nemo"))
    )
    clear = service._clear_applied

    def fail(_):
        raise OSError("cleanup lost")

    monkeypatch.setattr(service, "_clear_applied", fail)
    with pytest.raises(OSError):
        service.applied(actual, report)
    assert service.get("nemo")["status"] == "applying"
    monkeypatch.setattr(service, "_clear_applied", clear)
    service.applied(actual, report)
    assert service.get("nemo") is None
    assert store.db.execute("SELECT * FROM orundum_observation_wait").fetchall()
    assert service.manual_entry("nemo") is None
    store.close()


def test_second_expert_base_waits_for_fresh_observation_and_recovers_without_reentering(
    tmp_path, monkeypatch
):
    from maa_planner.daemon import scheduling
    from tests.helpers.async_tools import eventually
    from tests.helpers.scheduling import wake

    content = configuration(tmp_path)
    content += content[content.index("[[schedules.tasks]]") :]
    content += '\n[[schedules.tasks]]\ntype="Award"\n'

    async def scenario():
        clock = Clock()
        async with running(tmp_path, monkeypatch, clock, content=content) as state:
            service = state.daemon.daily.orundum
            service.register(
                settings_from_bytes(content.encode(), state.config), profile="nemo", key="key"
            )
            fresh = False

            async def info(*args):
                player = skland_base("orundum" if fresh else "normal")
                player["status"]["storeTs"] = clock.now()
                cultivate = warehouse()
                if fresh:
                    for item in cultivate["items"]:
                        if item["id"] in {"30012", "4001", "3141"}:
                            item["count"] = 0
                return SimpleNamespace(player_info=player, cultivate=cultivate), "nemo"

            monkeypatch.setattr(scheduling, "collect_character_info", info)
            wake(state, clock, "2026-11-02T12:10:00")
            await eventually(lambda: len(state.workers) == 1 and state.workers[0].allowed)
            worker = state.workers[0]
            first = json.loads(worker.request.core.tasks.document)
            assert first["business_ids"] == ["task:0", "task:2"]
            assert first["business_failures"] == [
                {
                    "id": "task:1",
                    "status": "error",
                    "error": {
                        "kind": "preparation",
                        "reason": "fresh_observation_required",
                    },
                }
            ]
            attachments = json.loads(worker.request.core.tasks.attachments)
            assert attachments["task-0.json"] == native_plan("orundum")
            assert "task-1.json" not in attachments
            query = {k: first["infrast_orundum"][k] for k in ("intent_id", "business_id")}
            assert service.start(worker.request, query)
            service.applied(
                worker.request,
                {
                    **query,
                    "task_id": 7,
                    "mode": "orundum",
                    "outcome": "applied",
                    "evidence": [f"callbacks:{i}" for i in range(6)],
                },
            )
            worker.result = RunResult(
                worker.request.attempt_id,
                "failed",
                business_results=(
                    {"id": "task:0", "status": "completed"},
                    {
                        "id": "task:1",
                        "status": "error",
                        "error": {"reason": "fresh_observation_required"},
                    },
                    {"id": "task:2", "status": "completed"},
                ),
            )
            worker.finish.set()
            await eventually(lambda: not state.daemon.running)
            retry = state.daemon.daily.retries.current(worker.request.job_id)
            assert retry["pending"] == ["task:1"]
            fresh = True
            clock.value = retry["due_at"]
            state.daemon.wakeup.set()
            await eventually(lambda: len(state.workers) == 2 and state.workers[1].allowed)
            request = state.workers[1].request
            second = json.loads(request.core.tasks.document)
            assert second["business_ids"] == ["lifecycle:startup", "task:1"]
            assert "infrast_orundum" not in second
            assert json.loads(request.core.tasks.attachments)["task-1.json"] == native_plan(
                "three_gold"
            )
            assert service.get("nemo") is None
            state.workers[1].finish.set()
            await eventually(lambda: not state.daemon.running)

    asyncio.run(scenario())

"""APK maintenance uses the existing exclusive API operation and native worker."""

import asyncio
import math
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.execution.context import RunOptions, RunRequest, RunResult
from maa_planner.daemon.execution.copilot import resolve_binding
from maa_planner.daemon.execution.runtime import CoreRun
from maa_planner.daemon.planning.game_update import readiness_tasks
from maa_planner.daemon.planning.schedule import ScheduledOccurrence
from maa_planner.daemon.tools.game_update import GameUpdateReport, update_game
from maa_planner.schema import ADMIN_CHANNEL, TERMINAL


class GameUpdateIncomplete(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__(
            "部分实例更新或资源就绪未完成："
            + "；".join(
                f"{u.profile}: {u.reason}"
                for u in report.units
                if u.status in {"error", "busy", "unavailable"}
            )
        )


async def perform_update(daemon, settings, cancel, *, automatic, revision):
    """Persist only unfinished readiness; a later attempt rechecks APK metadata."""
    settings = replace(settings, profiles=tuple(p for p in settings.profiles if p.enabled))
    if not settings.profiles:
        raise ValueError("没有启用的账号")
    store = daemon.store
    assert store is not None
    loop = asyncio.get_running_loop()

    async def record_intent(profile, metadata):
        store.mark_game_resources_pending(profile.name, asdict(metadata))

    def before_install(profile, metadata):
        asyncio.run_coroutine_threadsafe(record_intent(profile, metadata), loop).result()

    async def cancellation():
        while True:
            if daemon.stopping.is_set() or daemon._apk_cancel.is_set():
                cancel.set()
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(cancellation())
    try:
        report = await asyncio.to_thread(
            update_game,
            settings,
            allow_emulator_start=not automatic,
            cancel=cancel,
            on_install=before_install,
            raise_on_failure=False,
        )
        environment = None
        units = []
        for unit in report.units:
            pending = store.pending_game_resources().get(unit.profile)
            if (
                unit.status not in {"updated", "up_to_date"}
                or pending is None
                or unit.current is None
            ):
                units.append(unit)
                continue
            if pending != asdict(unit.current):
                store.mark_game_resources_pending(unit.profile, asdict(unit.current))
            if cancel.is_set():
                units.append(
                    replace(
                        unit,
                        status="error",
                        reason="资源就绪检查取消",
                        resources={"status": "pending"},
                    )
                )
                continue
            request, worker, finished = None, None, False
            try:
                if environment is None:
                    environment = await daemon.prepare_dependencies(settings)
                binding = await asyncio.to_thread(resolve_binding, settings, unit.profile)
                if binding.instance_index != unit.instance_index:
                    raise RuntimeError("APK 安装后模拟器绑定发生变化")
                identifier = "game-ready-" + uuid.uuid4().hex
                request = RunRequest(
                    identifier,
                    identifier,
                    revision,
                    binding,
                    RunOptions(
                        allow_emulator_start=not automatic,
                        resume_game_session=True,
                        execution_timeout=settings.game_update.resource_timeout,
                    ),
                    CoreRun(
                        environment.runtime, readiness_tasks(settings.game_update.poll_interval)
                    ),
                )
                if not store.enqueue(request):
                    raise RuntimeError("资源就绪任务标识重复")
                store.start(identifier)
                store.prepared_request(request)
                worker = daemon.worker_factory(request, daemon.root)

                async def admit():
                    return not cancel.is_set() and not daemon.stopping.is_set()

                async def phase(value):
                    store.phase(identifier, value)

                async def watch_worker():
                    while not cancel.is_set():
                        await asyncio.sleep(0.1)
                    await worker.cancel()

                monitor = asyncio.create_task(watch_worker())
                try:
                    result = await worker.run(admit, phase)
                finally:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                proof = next(
                    (b for b in result.business_results if b["id"] == "maintenance:resources"), None
                )
                verified = bool(proof and proof["status"] == "completed" and proof.get("evidence"))
                if result.status == "succeeded" and not verified:
                    result = replace(
                        result, status="failed", reason="未收到黄色 START 图标的资源就绪证据"
                    )
                store.finish(result)
                finished = True
                if result.status != "succeeded" or not verified:
                    raise RuntimeError(result.reason or "资源就绪检查未完成")
                assert proof is not None
                store.clear_game_resources_pending(unit.profile)
                store.observe_alert(unit.profile, "game_resources", "ready", None, now=time.time())
                store.event(
                    "game_resources_ready",
                    {
                        "profile": unit.profile,
                        "attempt_id": identifier,
                        "evidence": proof["evidence"],
                    },
                )
                units.append(
                    replace(
                        unit,
                        resources={
                            "status": "ready",
                            "evidence": proof["evidence"],
                            "log_directory": result.log_directory,
                        },
                    )
                )
            except Exception as error:
                if request is not None and not finished:
                    store.finish(
                        RunResult(
                            request.attempt_id,
                            "recovery_required" if worker is not None else "failed",
                            str(error),
                        )
                    )
                if not cancel.is_set():
                    store.observe_alert(
                        unit.profile,
                        "game_resources",
                        "ready",
                        {"reason": str(error)},
                        now=time.time(),
                    )
                units.append(
                    replace(
                        unit, status="error", reason=str(error), resources={"status": "pending"}
                    )
                )
        report = GameUpdateReport(report.latest, tuple(units))
        daemon.game_updates = report
        if any(u.status in {"error", "busy", "unavailable"} for u in report.units):
            raise GameUpdateIncomplete(report)
        return asdict(report)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


class GameUpdateScheduler:
    def __init__(self, daemon):
        self.daemon = daemon
        self.key = None
        self.next_at = None
        self.pending_at = None
        self.active = None

    def observe_login(self, profile, receipt, *, attempt_id):
        # Product assumption: maintenance, if any, continuously occupies 06:00–12:00.
        if not isinstance(receipt, dict):
            return
        evidence = receipt.get("evidence")
        if (
            not isinstance(evidence, (list, tuple))
            or not evidence
            or any(not isinstance(v, str) or not v for v in evidence)
        ):
            return
        times = [receipt.get(k) for k in ("started_at", "observed_at")]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in times):
            return
        began, observed = times
        if not began <= observed <= time.time():
            return
        start, end, now = [
            datetime.fromtimestamp(v, BUSINESS_TIMEZONE) for v in (began, observed, time.time())
        ]
        if (
            start.date() != end.date()
            or end.date() != now.date()
            or not 6 <= start.hour < 12
            or end.hour >= 12
        ):
            return
        fact = {
            "day": end.date().isoformat(),
            "profile": profile,
            "attempt_id": attempt_id,
            "started_at": began,
            "observed_at": observed,
            "evidence": list(evidence),
        }
        store = self.daemon.store
        assert store is not None
        if store.maintenance_login() != fact:
            store.save_maintenance_login(fact)
            store.event("maintenance_login_success", fact)
        self.pending_at = None
        self.daemon.set_maintenance_pending(False)
        settings = self.daemon._settings
        if (
            settings
            and self.next_at is not None
            and datetime.fromtimestamp(self.next_at, BUSINESS_TIMEZONE).date() == end.date()
        ):
            self.next_at = settings.game_update.next_after(self.next_at)
        self.daemon.wakeup.set()

    def status(self):
        settings = self.daemon._settings
        return {
            "enabled": bool(settings and settings.game_update.enabled),
            "time": settings.game_update.time if settings else None,
            "timezone": "Asia/Shanghai",
            "next_at": self.next_at,
            "pending_at": self.pending_at,
            "operation_id": self.active[0] if self.active else None,
            "login_exemption": self.daemon.store.maintenance_login() if self.daemon.store else None,
            "resources_pending": self.daemon.store.pending_game_resources()
            if self.daemon.store
            else {},
        }

    async def tick(self, *, dispatch=True):
        daemon, now = self.daemon, time.time()
        store = daemon.store
        assert store is not None
        today = datetime.fromtimestamp(now, BUSINESS_TIMEZONE).date().isoformat()
        exemption = store.maintenance_login()
        if exemption and exemption["day"] != today:
            store.save_maintenance_login(None)
            exemption = None
        expiry = None
        if exemption:
            self.pending_at = None
            expiry = (
                datetime.fromtimestamp(now, BUSINESS_TIMEZONE).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                + timedelta(days=1)
            ).timestamp()
        if self.active:
            identifier, occurrence = self.active
            operation = store.operation(identifier)
            if operation and operation["status"] in TERMINAL:
                store.schedule_update(occurrence.id, operation["status"])
                if operation["status"] == "failed":
                    store.observe_alert(
                        ADMIN_CHANNEL,
                        "game_update",
                        "scheduled",
                        {
                            "operation_id": identifier,
                            "reason": "自动更新失败，请查看本次操作结果和日志",
                        },
                        now=now,
                    )
                elif operation["status"] == "succeeded":
                    store.observe_alert(ADMIN_CHANNEL, "game_update", "scheduled", None, now=now)
                self.active = None
        settings = daemon._settings
        if not settings or daemon.config_error or daemon.stopping.is_set() or daemon.draining:
            daemon.set_maintenance_pending(False)
            return None
        if (
            not settings.game_update.enabled
            or settings.mumu_path is None
            or not any(p.enabled for p in settings.profiles)
        ):
            self.key, self.next_at, self.pending_at = None, None, None
            daemon.set_maintenance_pending(False)
            return expiry
        key = daemon.configuration.revision
        if self.key != key:
            self.key = key
            self.next_at = settings.game_update.next_after(now, inclusive=True)
            self.pending_at = None
        assert self.next_at is not None
        if (
            exemption
            and datetime.fromtimestamp(self.next_at, BUSINESS_TIMEZONE).date().isoformat() == today
        ):
            self.next_at = settings.game_update.next_after(self.next_at)
        if now >= self.next_at:
            if not exemption:
                self.pending_at = self.next_at
            self.next_at = settings.game_update.next_after(now)
        daemon.set_maintenance_pending(self.pending_at is not None)
        busy = self.active or daemon.resources_busy
        if dispatch and self.pending_at is not None and not busy:
            due = self.pending_at
            occurrence = ScheduledOccurrence("apk-update", ADMIN_CHANNEL, due, self.next_at)
            self.pending_at = None
            daemon.set_maintenance_pending(False)
            if store.claim_schedule(occurrence, key):
                identifier = "apk-update-" + str(int(due))
                try:
                    await daemon.operations.submit(
                        {"request_id": identifier, "action": "game-update"}, automatic=True
                    )
                except Exception:
                    store.schedule_update(occurrence.id, "failed", error="自动 APK 更新操作未接纳")
                    raise
                self.active = identifier, occurrence
        return min(self.next_at, expiry) if expiry is not None else self.next_at

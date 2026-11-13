"""Daemon adapter between daily occurrences and the existing execution pipeline."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime
from typing import TYPE_CHECKING

from maa_planner.daemon.config import settings_from_bytes
from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.daily_preparation import collect_daily_evidence, prepare_daily_inputs
from maa_planner.daemon.execution.context import CancelledRun, RunOptions, RunRequest, RunResult
from maa_planner.daemon.execution.copilot import resolve_binding
from maa_planner.daemon.execution.runtime import CoreRun
from maa_planner.daemon.planning.activity_shop import ActivityCatalogError
from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.planning.farming import FallbackUnavailable
from maa_planner.daemon.planning.schedule import ScheduledOccurrence
from maa_planner.daemon.planning.skland import collect_character_info
from maa_planner.daemon.policy import ConfigSnapshot
from maa_planner.daemon.scheduled_retry import ScheduledRetries
from maa_planner.daemon.tools.locks import BusyError, emulator_resource

if TYPE_CHECKING:
    from maa_planner.daemon.coordinator import Daemon


class DailyScheduler:
    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.pending: dict[str, tuple[ScheduledOccurrence, ConfigSnapshot]] = {}
        self.preparing: dict[str, asyncio.Task] = {}
        self.preparation_cancels: dict[str, threading.Event] = {}
        self.preparations: dict = {}
        self.batch_preparations: dict = {}
        self.waiting_units: dict[str, str] = {}
        self.attempts: dict[str, tuple[ScheduledOccurrence, ConfigSnapshot]] = {}
        self.annihilation_plans: dict = {}
        self.clearance_reports: dict = {}
        self.shop_reports: dict = {}
        self.shop_probes: dict[tuple[str, float], str] = {}
        self.retries = ScheduledRetries(self)
        from maa_planner.daemon.daily_control import DailyControl
        from maa_planner.daemon.orundum import OrundumIntents

        self.control = DailyControl(self)
        self.orundum = OrundumIntents(self)

    @property
    def store(self):
        assert self.daemon.store is not None
        return self.daemon.store

    def enqueue(self, occurrences: tuple[ScheduledOccurrence, ...]) -> None:
        snapshot = self.daemon.configuration
        assert snapshot is not None
        for occurrence in occurrences:
            control = self.store.daily_control(occurrence.id)
            if control and control["kind"] == "skip":
                continue
            if control and control["kind"] == "postpone":
                occurrence = replace(occurrence, not_before=control["due_at"])
            if self.store.claim_schedule(occurrence, snapshot.revision):
                self.pending[occurrence.id] = occurrence, snapshot
                self.store.schedule_update(occurrence.id, "pending")

    def allowed(self, occurrence: ScheduledOccurrence, snapshot: ConfigSnapshot) -> bool:
        current = self.daemon.configuration
        state = self.store.scheduled_run(occurrence.id)
        return bool(
            (current or self.control.is_temporary(occurrence.id))
            and (
                self.control.is_temporary(occurrence.id)
                or (current is not None and current.revision == snapshot.revision)
            )
            and (self.control.is_temporary(occurrence.id) or self.daemon.config_error is None)
            and not self.daemon.stopping.is_set()
            and (occurrence.not_before or occurrence.scheduled_at)
            <= time.time()
            < occurrence.expires_at
            and not self.control.cancel_requested(occurrence.id)
            and state
            and state["status"] in {"pending", "preparing", "running"}
        )

    def admit(self, request: RunRequest) -> bool:
        value = self.attempts.get(request.attempt_id)
        return value is None or self.allowed(*value)

    def dispatch(self) -> None:
        daemon = self.daemon
        self.control.reconcile()
        live_batches = {
            (snapshot.revision, occurrence.scheduled_at)
            for occurrence, snapshot in (*self.pending.values(), *self.attempts.values())
        }
        for key in self.batch_preparations.keys() - live_batches:
            daemon.release_preparation("daily-batch:" + repr(key))
        self.batch_preparations = {
            key: unit for key, unit in self.batch_preparations.items() if key in live_batches
        }
        if daemon.maintenance_pending:
            return
        self.retries.dispatch()
        for identifier, (occurrence, snapshot) in sorted(
            self.pending.items(), key=lambda item: item[1][0].not_before or item[1][0].scheduled_at
        ):
            if occurrence.expires_at <= time.time():
                self.store.schedule_update(identifier, "expired", error="已到该日程下一次执行时刻")
                self._discard(identifier)
                continue
            if not self.control.is_temporary(identifier) and (
                daemon.configuration is None or snapshot.revision != daemon.configuration.revision
            ):
                self.store.schedule_update(identifier, "cancelled", error="排期配置已经改变")
                self._discard(identifier)
                continue
            if identifier in self.preparing:
                continue
            if (
                not self.allowed(occurrence, snapshot)
                or daemon.maintenance is not None
                or daemon.draining
                or (
                    not self.control.is_temporary(identifier)
                    and self.store.daily_paused(occurrence.profile)
                )
            ):
                continue
            busy_profiles = {
                worker.request.binding.profile.name for worker, _ in daemon.running.values()
            } | {self.pending[key][0].profile for key in self.preparing if key in self.pending}
            if occurrence.profile in busy_profiles:
                continue
            unit = self.waiting_units.get(identifier)
            if unit is not None and daemon._unit_busy(unit):
                continue
            self.waiting_units.pop(identifier, None)
            settings = settings_from_bytes(snapshot.content, daemon.config)
            schedule = self.control.definition(identifier, settings) or next(
                s for s in settings.schedules if s.id == occurrence.schedule_id
            )
            retry = self.retries.current(identifier)
            retry = retry if retry and retry["status"] == "running" else None
            pending_business = frozenset(retry["pending"]) if retry else None
            if pending_business is not None:
                schedule = replace(
                    schedule,
                    tasks=tuple(
                        replace(
                            task,
                            enabled=task.enabled and f"task:{i}" in pending_business,
                            weekdays=(1, 2, 3, 4, 5, 6, 7),
                        )
                        for i, task in enumerate(schedule.tasks)
                    ),
                )
            weekday = datetime.fromtimestamp(time.time(), BUSINESS_TIMEZONE).isoweekday()
            if pending_business is None and not any(
                task.enabled and weekday in task.weekdays for task in schedule.tasks
            ):
                self.store.schedule_update(identifier, "skipped", error="当前日期没有启用的任务")
                self.pending.pop(identifier)
                continue
            try:
                if retry:
                    preparation = daemon.select_preparation(
                        settings, batch_key=("retry", occurrence.id, retry["number"])
                    )
                else:
                    batch_key = (snapshot.revision, occurrence.scheduled_at)
                    preparation = self.batch_preparations.get(batch_key)
                    if preparation is None:
                        preparation = daemon.select_preparation(settings, batch_key=batch_key)
                        self.batch_preparations[batch_key] = preparation
                        daemon.retain_preparation("daily-batch:" + repr(batch_key), preparation)
            except ValueError:
                # A running environment pins incompatible dependencies until it finishes.
                continue
            daemon.reserve_resources(
                "daily:" + identifier, accepted_configuration=self.control.is_temporary(identifier)
            )
            daemon.retain_preparation("daily:" + identifier, preparation)
            self.preparations[identifier] = preparation
            self.store.schedule_update(identifier, "preparing")
            cancel = threading.Event()
            self.preparation_cancels[identifier] = cancel

            async def prepare(
                occurrence=occurrence,
                snapshot=snapshot,
                settings=settings,
                preparation=preparation,
                schedule=schedule,
                retry=retry,
                pending_business=pending_business,
                cancel=cancel,
            ):
                request, plan, error, busy_unit = None, None, None, None
                try:
                    binding = self.control.binding(occurrence.id)
                    if binding is None:
                        binding = await asyncio.to_thread(
                            resolve_binding, settings, occurrence.profile, False
                        )
                    if cancel.is_set():
                        raise CancelledRun("日常准备已取消")
                    unit = emulator_resource(binding.mumu_root, binding.instance_index).key
                    if not daemon.claim_unit("daily:" + occurrence.id, unit):
                        busy_unit = unit
                    else:
                        environment = await preparation.get(daemon.prepare_dependencies)
                        if cancel.is_set():
                            raise CancelledRun("日常准备已取消")
                        daemon.remember_activity(environment.activity)
                        now = time.time()
                        evidence = await collect_daily_evidence(
                            settings,
                            binding.profile,
                            schedule,
                            now=now,
                            collect_info=collect_character_info,
                        )
                        if cancel.is_set():
                            raise CancelledRun("日常准备已取消")
                        if evidence.experts:
                            if evidence.skland_error is None:
                                self.store.observe_alert(
                                    occurrence.profile,
                                    "skland_prepare",
                                    schedule.id,
                                    None,
                                    now=now,
                                )
                            elif evidence.requires_skland:
                                self.store.observe_alert(
                                    occurrence.profile,
                                    "skland_prepare",
                                    schedule.id,
                                    {"reason": str(evidence.skland_error)},
                                    now=now,
                                )
                                raise evidence.skland_error
                        from maa_planner.daemon.orundum import check_observation

                        check_observation(
                            self.store,
                            occurrence.profile,
                            evidence,
                            manual_entry=self.orundum.manual_entry(occurrence.profile),
                        )
                        now = time.time()

                        async def rescue_candidates():
                            return await preparation.annihilation_candidates(cancel, now=now)

                        inputs = await prepare_daily_inputs(
                            settings,
                            binding.profile,
                            schedule,
                            evidence,
                            now=now,
                            activity=environment.activity,
                            store=self.store,
                            load_catalog=preparation.stage_catalog,
                            load_sets=preparation.copilot_sets,
                            load_navigation=preparation.activity_navigation,
                            load_annihilation_candidates=rescue_candidates,
                            cancel=cancel,
                            pending=pending_business,
                        )
                        if cancel.is_set():
                            raise CancelledRun("日常准备已取消")
                        for stage, issue in inputs.issues.items():
                            details = None
                            if issue is not None:
                                details = {"reason": str(issue)}
                                if stage == "activity_shop_prepare":
                                    details["kind"] = (
                                        "catalog"
                                        if isinstance(issue, ActivityCatalogError)
                                        else "unknown"
                                    )
                                elif stage == "infrast_processing_prepare":
                                    details = {"error": str(issue)}
                            self.store.observe_alert(
                                occurrence.profile,
                                stage,
                                "automatic:7661"
                                if stage == "activity_copilot_prepare"
                                else schedule.id,
                                details,
                                now=now,
                            )
                        self.store.observe_alert(
                            occurrence.profile,
                            "infrast_processing",
                            schedule.id,
                            {"reason": "unsupported_operator", "operators": ["九色鹿"]}
                            if any(
                                "九色鹿" in t.processing_operators
                                for t in inputs.configured_processing
                            )
                            else None,
                            now=now,
                        )
                        try:
                            plan = await asyncio.to_thread(
                                build_daily_plan,
                                schedule,
                                **inputs.plan_arguments(),
                                manual_entry=self.orundum.manual_entry(occurrence.profile),
                            )
                        except FallbackUnavailable as exception:
                            self.store.observe_alert(
                                occurrence.profile,
                                "farming_plan",
                                schedule.id,
                                {
                                    "reason": str(exception),
                                    "kind": "fallback_unavailable",
                                },
                                now=now,
                            )
                            raise
                        self.store.observe_alert(
                            occurrence.profile,
                            "farming_plan",
                            schedule.id,
                            None,
                            now=now,
                        )
                        if plan.tasks is not None:
                            request = RunRequest(
                                occurrence.id,
                                f"{occurrence.id}-retry-{retry['number']}"
                                if retry
                                else occurrence.id,
                                snapshot.revision,
                                binding,
                                RunOptions(
                                    allow_emulator_start=self.control.is_temporary(occurrence.id),
                                    execution_timeout=schedule.execution_timeout,
                                    resume_game_session=retry is not None,
                                ),
                                CoreRun(environment.runtime, plan.tasks, schedule.options_json),
                            )
                except asyncio.CancelledError:
                    cancel.set()
                    raise
                except Exception as exception:
                    error = str(exception) or type(exception).__name__
                await daemon._changes.put(
                    ("scheduled_ready", (occurrence.id, request, plan, error, busy_unit))
                )
                daemon.wakeup.set()

            self.preparing[identifier] = asyncio.create_task(prepare())

    def ready(self, payload) -> None:
        identifier, request, plan, error, busy_unit = payload
        self.preparing.pop(identifier, None)
        self.preparation_cancels.pop(identifier, None)
        preparation = self.preparations.pop(identifier, None)
        self.daemon.release_resources("daily:" + identifier)
        value = self.pending.get(identifier)
        if value is None:
            return
        occurrence, snapshot = value
        if not self.allowed(occurrence, snapshot):
            status = "expired" if occurrence.expires_at <= time.time() else "cancelled"
            self.store.schedule_update(identifier, status, error="排期已失效，未启动 runner")
            retry = self.retries.current(identifier)
            if retry and retry["status"] == "running":
                self.retries.update_status(retry, status)
        elif busy_unit is not None:
            self.waiting_units[identifier] = busy_unit
            self.store.schedule_update(identifier, "pending")
            return
        elif error is not None:
            self.store.schedule_update(identifier, "failed", error=error)
            retry = self.retries.current(identifier)
            if retry and "pending" in retry:
                self.retries.preparation_failed(occurrence, snapshot, retry["pending"], error)
        elif request is None and plan and plan.failures:
            self.retries.finished(
                occurrence,
                snapshot,
                None,
                RunResult(
                    occurrence.id,
                    "failed",
                    business_results=plan.failures,
                ),
            )
        elif request is None:
            retry = self.retries.current(identifier)
            if retry and retry["status"] == "running":
                self.retries.update_status(retry, "completed")
            reason = (
                plan.annihilation.reason if plan and plan.annihilation else "当前日期没有启用的任务"
            )
            self.store.schedule_update(identifier, "skipped", error=reason)
            if plan and plan.annihilation:
                if plan.annihilation.completed:
                    self.store.reconcile_alerts(
                        occurrence.profile, "annihilation_copilot", {}, now=time.time()
                    )
                self.store.event(
                    "annihilation_decision",
                    {
                        "profile": occurrence.profile,
                        "occurrence_id": identifier,
                        **asdict(plan.annihilation),
                    },
                )
        else:
            if plan.annihilation is not None:
                if plan.annihilation.completed:
                    self.store.reconcile_alerts(
                        occurrence.profile, "annihilation_copilot", {}, now=time.time()
                    )
                self.store.event(
                    "annihilation_decision",
                    {
                        "profile": occurrence.profile,
                        "occurrence_id": identifier,
                        **asdict(plan.annihilation),
                    },
                )
                if plan.annihilation.run:
                    self.annihilation_plans[request.attempt_id] = plan.annihilation
            for decision in plan.farming:
                self.store.event(
                    "farming_decision",
                    {
                        "occurrence_id": identifier,
                        "profile": occurrence.profile,
                        **asdict(decision),
                    },
                )
            for decision in plan.decisions:
                self.store.event(
                    "infrast_decision",
                    {
                        "occurrence_id": identifier,
                        "profile": occurrence.profile,
                        "mode": decision.mode.value,
                        "file": str(decision.file),
                        "reason": decision.reason,
                        "warnings": [asdict(w) for w in decision.warnings],
                    },
                )
            if plan.decisions:
                self.store.reconcile_alerts(
                    occurrence.profile,
                    "infrast_material",
                    {
                        warning.item_id: {**asdict(warning), "occurrence_id": identifier}
                        for decision in plan.decisions
                        for warning in decision.warnings
                    },
                    now=time.time(),
                )
            self.attempts[request.attempt_id] = occurrence, snapshot
            self.store.schedule_update(identifier, "preparing", attempt_id=request.attempt_id)
            self.daemon._launch(request, preparation=preparation)
        self.pending.pop(identifier)
        self.control.reconcile()

    def _discard(self, identifier: str) -> None:
        retry = self.retries.current(identifier)
        state = self.store.scheduled_run(identifier)
        if (
            retry
            and retry["status"] == "running"
            and state
            and state["status"] in {"expired", "cancelled"}
        ):
            self.retries.update_status(retry, state["status"])
        task = self.preparing.get(identifier)
        if cancel := self.preparation_cancels.get(identifier):
            cancel.set()
        # A thread-backed preparation still owns its dependencies until it reports ready.
        if task is None:
            self.daemon.release_resources("daily:" + identifier)
        self.pending.pop(identifier, None)
        if task is None:
            self.preparations.pop(identifier, None)
        self.waiting_units.pop(identifier, None)

    def finished(self, result) -> None:
        self.orundum.finished(result)
        value = self.attempts.pop(result.attempt_id, None)
        decision = self.annihilation_plans.pop(result.attempt_id, None)
        if value is not None:
            occurrence = value[0]
            from maa_planner.daemon.execution.activity_copilot import ClearanceReport

            request = self.store.prepared_execution(result.attempt_id)
            for report in result.activity_copilot_results:
                if self.clearance_reports.get(result.attempt_id) != report:
                    self.store.record_clearance(
                        occurrence.profile,
                        report["activity_id"],
                        ClearanceReport.from_dict(report),
                        now=time.time(),
                        attempt_id=result.attempt_id,
                        log_directory=result.log_directory,
                        story_stages=self.story_stages(request),
                    )
            for report in result.activity_reward_results:
                from maa_planner.daemon.tools.reward_roi import save_claim_roi

                try:
                    settings = settings_from_bytes(value[1].content, self.daemon.config)
                    save_claim_roi(settings.cache_directory, report)
                except (OSError, ValueError, BusyError) as error:
                    self.store.event(
                        "activity_reward_roi_cache_error",
                        {
                            "profile": occurrence.profile,
                            "attempt_id": result.attempt_id,
                            "activity_id": report.get("activity_id"),
                            "reason": str(error),
                        },
                    )
                self.store.event(
                    "activity_reward_result",
                    {"profile": occurrence.profile, "attempt_id": result.attempt_id, **report},
                )
            for report in result.activity_shop_results:
                if request is not None and report not in self.shop_reports.get(
                    result.attempt_id, []
                ):
                    self.shop_report(request, report)
            if decision is not None:
                observed = []
                for report in result.annihilation_results:
                    if (
                        report.observed_at is not None
                        and decision.week_start <= report.observed_at < decision.week_end
                    ):
                        observed.append(report)
                warnings = {
                    str(int(decision.week_start)): {
                        "attempt_id": result.attempt_id,
                        "reason": report.reason,
                        "stage": report.stage,
                    }
                    for report in observed
                    if report.outcome in {"copilot_required", "unknown"}
                }
                self.store.reconcile_alerts(
                    occurrence.profile, "annihilation_copilot", warnings, now=time.time()
                )
                self.store.event(
                    "annihilation_result",
                    {
                        "profile": occurrence.profile,
                        "attempt_id": result.attempt_id,
                        "week_start": decision.week_start,
                        "results": [asdict(r) for r in result.annihilation_results],
                        "retry": "next_batch",
                    },
                )
            if self.control.cancel_requested(occurrence.id):
                result = replace(result, status="cancelled", reason="用户取消临时日常")
            self.store.schedule_update(value[0].id, result.status, error=result.reason or None)
            if request is not None:
                self.retries.finished(occurrence, value[1], request, result)
            self.control.reconcile()
        self.clearance_reports.pop(result.attempt_id, None)
        self.shop_reports.pop(result.attempt_id, None)
        self.shop_probes = {
            key: owner for key, owner in self.shop_probes.items() if owner != result.attempt_id
        }

    def shop_permission(self, request: RunRequest, query: dict) -> str:
        plan = self._shop_plan(request, query)
        state = self.store.shop_navigation_state(plan).get("status", "untried")
        if state != "untried":
            return state
        key = plan["activity_id"], plan["available_from"]
        if self.shop_probes.get(key, request.attempt_id) != request.attempt_id:
            return "pending"
        self.shop_probes[key] = request.attempt_id
        return "untried"

    def _shop_plan(self, request: RunRequest, report: dict) -> dict:
        import json

        assert request.core is not None
        plans = json.loads(request.core.tasks.document).get("activity_shops", [])
        plan = next(
            (
                p
                for p in plans
                if p["activity_id"] == report.get("activity_id")
                and p["fingerprint"] == report.get("fingerprint")
                and p["after_task"] == report.get("after_task")
            ),
            None,
        )
        if plan is None:
            raise ValueError("worker 商店报告与固定计划不匹配")
        return plan

    def shop_report(self, request: RunRequest, report: dict) -> None:
        plan = self._shop_plan(request, report)
        self.store.record_shop(
            request.binding.profile.name, plan, report, attempt_id=request.attempt_id
        )
        self.shop_reports.setdefault(request.attempt_id, []).append(report)
        key = plan["activity_id"], plan["available_from"]
        if self.shop_probes.get(key) == request.attempt_id:
            self.shop_probes.pop(key)

    @staticmethod
    def story_stages(request):
        if request is None or request.core is None:
            return frozenset()
        import json

        plan = json.loads(request.core.tasks.document).get("activity_copilot", {})
        return frozenset(
            e["stage_id"] for e in plan.get("targets", []) if e.get("kind") == "tail_story"
        )

    def clearance_report(self, request: RunRequest, report: dict) -> None:
        import json

        from maa_planner.daemon.execution.activity_copilot import ClearanceReport

        assert request.core is not None
        plan = json.loads(request.core.tasks.document).get("activity_copilot")
        if plan is None or report.get("activity_id") != plan["activity_id"]:
            raise ValueError("worker 通关报告与固定计划不匹配")
        self.store.record_clearance(
            request.binding.profile.name,
            report["activity_id"],
            ClearanceReport.from_dict(report),
            now=time.time(),
            attempt_id=request.attempt_id,
            story_stages=self.story_stages(request),
        )
        self.clearance_reports[request.attempt_id] = report

    def running(self, attempt_id: str) -> None:
        value = self.attempts.get(attempt_id)
        if value is not None:
            self.store.schedule_update(value[0].id, "running")
            self.control.reconcile()

    def wake_at(self) -> float | None:
        deadlines = [o.expires_at for o, _ in self.pending.values()]
        now = time.time()
        deadlines.extend(
            o.not_before for o, _ in self.pending.values() if o.not_before and o.not_before > now
        )
        deadlines.extend(self.control.wake_times())
        if (retry_at := self.retries.wake_at()) is not None:
            deadlines.append(retry_at)
        return min(deadlines, default=None)

    async def close(self) -> None:
        for cancel in self.preparation_cancels.values():
            cancel.set()
        if self.preparing:
            await asyncio.gather(*self.preparing.values(), return_exceptions=True)
        for identifier in self.preparing:
            self.daemon.release_resources("daily:" + identifier)
        for key in self.batch_preparations:
            self.daemon.release_preparation("daily-batch:" + repr(key))
        self.batch_preparations.clear()
        self.preparing.clear()
        self.preparation_cancels.clear()
        self.preparations.clear()
        self.waiting_units.clear()
        for identifier in self.pending:
            if self.control.is_temporary(identifier):
                continue
            self.store.schedule_update(identifier, "cancelled", error="daemon 停止，未启动本次日常")
            retry = self.retries.current(identifier)
            if retry and retry["status"] == "running":
                self.retries.update_status(retry, "cancelled")
        self.pending.clear()

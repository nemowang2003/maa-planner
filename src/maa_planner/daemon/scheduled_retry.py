"""Retry failed daily business tasks by planning them again from current evidence."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, replace

from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.planning.schedule import ScheduledOccurrence
from maa_planner.daemon.tools.locks import emulator_resource

RETRY_DELAYS = (300, 600, 900)


def business_results(request, result) -> list[dict]:
    """Old worker reports can be read without restoring an old execution plan."""
    if result.business_results:
        return list(result.business_results)
    if request is None or request.core is None:
        return []
    doc = json.loads(request.core.tasks.document)
    keys = doc.get("business_ids")
    if keys is None:
        return []
    states = {p["index"]: p for p in result.task_checkpoints}
    return [
        {
            "id": key,
            "status": states.get(i, {}).get("status", "pending"),
            **(
                {"error": states[i]}
                if states.get(i, {}).get("status") in {"error", "failed"}
                else {}
            ),
        }
        for i, key in enumerate(keys)
    ]


class ScheduledRetries:
    def __init__(self, scheduler):
        self.scheduler = scheduler

    @property
    def store(self):
        return self.scheduler.store

    def current(self, identifier):
        return next((r for r in self.store.scheduled_retries() if r["id"] == identifier), None)

    def update_status(self, retry, status):
        updated = {**retry, "status": status}
        if status not in {"waiting", "running", "recovery_required"}:
            updated = {k: v for k, v in updated.items() if k not in {"due_at", "pending", "unit"}}
        self.store.save_scheduled_retry(updated)
        self.store.observe_alert(
            retry["profile"],
            "task_retry",
            retry["id"],
            None
            if status == "completed"
            else {k: v for k, v in updated.items() if k not in {"occurrence", "request"}},
            now=time.time(),
        )

    def finished(self, occurrence, snapshot, request, result):
        previous = self.current(occurrence.id)
        archived = self.store.history.get("retry", occurrence.id)
        if previous is None and archived and archived["status"] == "exhausted":
            return
        if result.status in {"cancelled", "recovery_required", "blocked"}:
            if previous:
                self.update_status(previous, result.status)
            return
        records = business_results(request, result)
        errors = [r for r in records if r["status"] in {"error", "failed"}]
        if not errors:
            if previous:
                self.update_status(
                    previous, "completed" if result.status == "succeeded" else "failed"
                )
            return
        pending = {r["id"] for r in records if r["status"] not in {"completed", "skipped"}}
        unit = previous.get("unit") if previous else None
        if request is not None and request.core is not None:
            unit = emulator_resource(request.binding.mumu_root, request.binding.instance_index).key
        elif previous:
            pending.update(previous["pending"])
        number = (previous["number"] if previous else 0) + 1
        now = time.time()
        due = now + RETRY_DELAYS[min(number, len(RETRY_DELAYS)) - 1]
        status = (
            "waiting"
            if number <= len(RETRY_DELAYS) and due < occurrence.expires_at
            else "exhausted"
        )
        retry = {
            "id": occurrence.id,
            "profile": occurrence.profile,
            "number": number if status == "waiting" else number - 1,
            "status": status,
            **(
                {"due_at": due, "pending": sorted(pending), "unit": unit}
                if status == "waiting"
                else {}
            ),
            "expires_at": occurrence.expires_at,
            "occurrence": asdict(occurrence),
            "revision": snapshot.revision,
            "failed_attempt": result.attempt_id,
            "log_directory": result.log_directory,
            "errors": errors,
        }
        with self.store.db:
            self.store.save_scheduled_retry(retry)
            self.store.observe_alert(
                occurrence.profile,
                "task_retry",
                occurrence.id,
                {k: v for k, v in retry.items() if k not in {"occurrence", "request"}},
                now=now,
            )
            self.store.schedule_update(
                occurrence.id,
                "retry_waiting" if status == "waiting" else "failed",
                error="业务任务失败；已安排重新规划"
                if status == "waiting"
                else "恢复重试达到上限或时间窗口不足",
            )

    def preparation_failed(self, occurrence, snapshot, pending, error):
        retry = self.current(occurrence.id)
        attempt = f"{occurrence.id}-retry-{retry['number']}" if retry else occurrence.id
        self.finished(
            occurrence,
            snapshot,
            None,
            RunResult(
                attempt,
                "failed",
                str(error),
                business_results=tuple(
                    {
                        "id": key,
                        "status": "error",
                        "error": {"kind": "preparation", "reason": str(error)},
                    }
                    for key in pending
                ),
            ),
        )

    def dispatch(self):
        daemon = self.scheduler.daemon
        now = time.time()
        for retry in self.store.scheduled_retries():
            if retry["status"] != "waiting":
                continue
            occurrence = ScheduledOccurrence(**retry["occurrence"])
            state = self.store.scheduled_run(occurrence.id)
            if (
                now >= retry["expires_at"]
                or (
                    not self.scheduler.control.is_temporary(occurrence.id)
                    and (
                        not daemon.configuration
                        or daemon.configuration.revision != retry["revision"]
                    )
                )
                or not state
                or state["status"] != "retry_waiting"
            ):
                status = "expired" if now >= retry["expires_at"] else "cancelled"
                self.update_status(retry, status)
                self.store.schedule_update(
                    occurrence.id, status, error="恢复排期已过期或配置／排期已改变"
                )
                continue
            if "pending" not in retry:
                self.update_status(retry, "recovery_required")
                self.store.schedule_update(
                    occurrence.id,
                    "recovery_required",
                    error="旧恢复排期缺少业务任务标识，未重放旧快照",
                )
                continue
            if (
                (
                    not self.scheduler.control.is_temporary(occurrence.id)
                    and self.store.daily_paused(occurrence.profile)
                )
                or retry["due_at"] > now
                or (daemon.config_error and not self.scheduler.control.is_temporary(occurrence.id))
                or daemon.stopping.is_set()
                or daemon.draining
                or daemon.maintenance_pending
                or daemon.maintenance is not None
                or (retry.get("unit") and daemon._unit_busy(retry["unit"]))
                or any(
                    w.request.binding.profile.name == occurrence.profile
                    for w, _ in daemon.running.values()
                )
                or any(
                    self.scheduler.pending[k][0].profile == occurrence.profile
                    for k in self.scheduler.preparing
                    if k in self.scheduler.pending
                )
            ):
                continue
            self.store.save_scheduled_retry({**retry, "status": "running"})
            self.store.schedule_update(occurrence.id, "pending")
            self.scheduler.pending[occurrence.id] = (
                replace(occurrence, not_before=retry["due_at"]),
                self.scheduler.control.snapshot(occurrence.id),
            )

    def wake_at(self):
        now = time.time()
        return min(
            (
                r["due_at"] if r["due_at"] > now else r["expires_at"]
                for r in self.store.scheduled_retries()
                if r["status"] == "waiting"
            ),
            default=None,
        )

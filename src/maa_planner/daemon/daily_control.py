"""One-shot controls and temporary definitions owned by the daily scheduler."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import tomllib
from dataclasses import asdict, replace
from pathlib import Path

from maa_planner.daemon.config import Profile
from maa_planner.daemon.execution.context import UnitBinding
from maa_planner.daemon.planning.schedule import ScheduledOccurrence, parse_schedules
from maa_planner.daemon.policy import ConfigSnapshot
from maa_planner.schema import TERMINAL, APIError


def resolve_daily_binding(settings, profile):
    from maa_planner.daemon.adapters.mumu import MuMu, match_profiles

    if settings.mumu_path is None:
        raise ValueError("需要配置 [mumu]")
    account = next(p for p in settings.profiles if p.name == profile)
    instance = match_profiles((account,), MuMu(settings.mumu_path).instances())[account]
    return UnitBinding(account, settings.mumu_path, instance.index, instance.name)


class DailyControl:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.lock = asyncio.Lock()
        self.restored = False

    @property
    def daemon(self):
        return self.scheduler.daemon

    @property
    def store(self):
        return self.scheduler.store

    def is_temporary(self, identifier):
        value = self.store.daily_control(identifier)
        return bool(value and value["kind"] == "temporary")

    def cancel_requested(self, identifier):
        value = self.store.daily_control(identifier)
        if value and value["kind"] == "temporary":
            operation = self.store.operation(value["operation_id"])
            return bool(operation and operation["cancel_requested"])
        return False

    def wake_times(self):
        now = time.time()
        return [
            t
            for c in self.store.daily_controls()
            if c["kind"] != "pause"
            for t in (c["occurrence"]["scheduled_at"], c["due_at"], c["occurrence"]["expires_at"])
            if t > now
        ]

    def _settings(self, profile):
        settings = self.daemon.control_configuration.settings()
        if not any(p.name == profile and p.enabled for p in settings.profiles):
            raise ValueError("未知或已禁用的 profile")
        return settings

    def candidates(self, profile):
        settings = self._settings(profile)
        now = time.time()
        controls = {
            c["id"]: c for c in self.store.daily_controls() if c["kind"] in {"skip", "postpone"}
        }
        values = {}
        # Pending live windows are considered before future, independently of
        # the mutable policy cursor. Queries never claim schedules.
        for occurrence, snapshot in self.scheduler.pending.values():
            if (
                occurrence.profile == profile
                and not self.is_temporary(occurrence.id)
                and snapshot.revision == self.daemon.configuration.revision
            ):
                values[occurrence.id] = occurrence
        for schedule in settings.schedules:
            if not schedule.enabled or profile not in schedule.profiles:
                continue
            due = schedule.next_after(now, inclusive=True)
            # At most the explicitly skipped opportunities plus one successor.
            for _ in range(len(controls) + 2):
                occurrence = ScheduledOccurrence(
                    schedule.id, profile, due, schedule.next_after(due)
                )
                control = controls.get(occurrence.id)
                state = self.store.scheduled_run(occurrence.id)
                if (not control or control["kind"] != "skip") and (
                    not state or state["status"] == "pending"
                ):
                    values[occurrence.id] = occurrence
                    break
                due = schedule.next_after(due)
        for control in controls.values():
            if (
                control["profile"] == profile
                and control["kind"] == "postpone"
                and control["revision"] == self.daemon.configuration.revision
            ):
                o = ScheduledOccurrence(**control["occurrence"])
                values[o.id] = o
        rows = []
        for occurrence in values.values():
            control = controls.get(occurrence.id)
            state = self.store.scheduled_run(occurrence.id)
            if (control and control["kind"] == "skip") or occurrence.expires_at <= now:
                continue
            if occurrence.id in self.scheduler.preparing or any(
                o.id == occurrence.id for o, _ in self.scheduler.attempts.values()
            ):
                continue
            if state and state["status"] != "pending":
                continue
            due = (
                control["due_at"]
                if control and control["kind"] == "postpone"
                else occurrence.scheduled_at
            )
            rows.append(
                {
                    "occurrence_id": occurrence.id,
                    "schedule_id": occurrence.schedule_id,
                    "profile": profile,
                    "scheduled_at": occurrence.scheduled_at,
                    "due_at": due,
                    "expires_at": occurrence.expires_at,
                    "state_version": control["version"] if control else 0,
                    "status": "pending",
                    "source": "periodic",
                }
            )
        return rows

    def get(self, profile, identifier):
        self._settings(profile)
        state = self.store.scheduled_run(identifier)
        if not state or state["profile"] != profile:
            raise APIError("not_found", "未知日常身份")
        attempt = (
            self.store.history.get("attempt", state["attempt_id"]) if state["attempt_id"] else None
        )
        return {
            **state,
            "business_results": attempt["result"].get("business_results", []) if attempt else [],
        }

    def status(self, profile):
        self._settings(profile)
        return {"profile": profile, "paused": self.store.daily_paused(profile)}

    def next(self, profile):
        rows = self.candidates(profile)
        earliest = min((r["due_at"] for r in rows), default=None)
        return {
            "revision": self.daemon.configuration.revision,
            "occurrences": [r for r in rows if r["due_at"] == earliest],
        }

    async def accept(self, identifier, command, key):
        async with self.lock:
            identifier = self.store.find_operation_key(key) or identifier
            payload = {**command, "idempotency_key": key}
            previous = self.store.operation(identifier)
            if previous:
                if any(previous.get(k) != v for k, v in payload.items()):
                    raise APIError(
                        "conflict", "去重键已存在但参数不同", {"operation_id": identifier}
                    )
                return self.daemon.operations.get(identifier)
            if self.daemon.stopping.is_set() or self.daemon.draining:
                raise RuntimeError("daemon 正在停止或排空")
            if command["action"] in {"daily-run", "daily-retry"}:
                self.daemon.available()
            settings = self._settings(command["target"])
            revision = self.daemon.configuration.revision
            args = command["arguments"]
            if command["action"] in {"daily-run", "daily-retry"}:
                self.daemon.reserve_resources("daily-accept:" + identifier)
                try:
                    source = (
                        self._retry(command, revision)
                        if command["action"] == "daily-retry"
                        else None
                    )
                    temporary_args = {**args, "inline": source} if source is not None else args
                    value = await self._temporary(
                        identifier, command["target"], settings, temporary_args
                    )
                    if source is not None:
                        value["retry_of"] = args["occurrence_id"]
                        value["task_ids"] = args["task_ids"]
                finally:
                    self.daemon.release_resources("daily-accept:" + identifier)
                try:
                    # File/binding resolution yields; recheck lifecycle, but keep
                    # the configuration accepted at the start of this request.
                    self.daemon.available()
                    if args["expires_at"] <= max(time.time(), value["due_at"]):
                        raise ValueError("准备接纳期间请求已经过期")
                    # Other operation methods do not share this daily lock and
                    # may have won the key while binding resolution yielded.
                    winner = self.store.find_operation_key(key)
                    if winner is not None and winner != identifier:
                        raise APIError(
                            "conflict", "去重键已被另一操作接纳", {"operation_id": winner}
                        )
                    result = self.result(value)
                    self.store.accept_daily_control(identifier, payload, value, result, "waiting")
                except BaseException:
                    if self.store.operation(identifier) is None:
                        shutil.rmtree(value["directory"], ignore_errors=True)
                    else:
                        self.daemon.wakeup.set()
                    raise
            else:
                if command["action"] == "daily-pause":
                    value = {
                        "id": "pause:" + command["target"],
                        "kind": "pause",
                        "profile": command["target"],
                        "paused": args["paused"],
                    }
                    result = {"profile": command["target"], "paused": args["paused"]}
                else:
                    value, result = self._change(command, revision)
                self.store.accept_daily_control(identifier, payload, value, result, "succeeded")
                if command["action"] == "daily-skip" and value["id"] in self.scheduler.pending:
                    self.store.schedule_update(value["id"], "skipped", error="用户一次跳过")
                    self.scheduler._discard(value["id"])
                    self.store.archive_daily_control(value)
            self.daemon.wakeup.set()
            return self.daemon.operations.get(identifier)

    def _retry(self, command, revision):
        args, profile = command["arguments"], command["target"]
        state = self.store.scheduled_run(args["occurrence_id"])
        if not state or state["profile"] != profile:
            raise APIError("not_found", "未知日常身份")
        if state["status"] not in {"failed", "expired"} or self.scheduler.retries.current(
            state["id"]
        ):
            raise APIError("conflict", "只允许重试已经终结的明确失败日常")
        if args["expected_revision"] != revision or state["revision"] != revision:
            raise APIError("conflict", "配置已改变，不能按旧任务索引重试")
        attempt = self.store.history.get("attempt", state["attempt_id"])
        records = attempt["result"].get("business_results", []) if attempt else []
        eligible = {r["id"] for r in records if r["status"] in {"error", "failed", "pending"}}
        if (
            not any(r["status"] in {"error", "failed"} for r in records)
            or not set(args["task_ids"]) <= eligible
        ):
            raise APIError("conflict", "选定任务缺少失败或未执行证据")
        control = self.store.history.get("daily_control", state["id"])
        if control and control["kind"] == "temporary":
            raw = control["retry_definition"]
        else:
            raw = next(
                (
                    row
                    for row in tomllib.loads(
                        self.daemon.configuration.content.decode("utf-8-sig")
                    ).get("schedules", [])
                    if row["id"] == state["schedule_id"] and profile in row["profiles"]
                ),
                None,
            )
            if raw is None:
                raise APIError("conflict", "原日程不存在")
        raw = json.loads(json.dumps(raw))
        for key in ("id", "profiles", "times", "weekdays", "enabled"):
            raw.pop(key, None)
        for index, task in enumerate(raw["tasks"]):
            task["enabled"] = task.get("enabled", True) and f"task:{index}" in args["task_ids"]
            task["weekdays"] = [1, 2, 3, 4, 5, 6, 7]
        return raw

    def _change(self, command, revision):
        args, profile = command["arguments"], command["target"]
        identifier = args["occurrence_id"]
        previous = self.store.daily_control(identifier)
        archived = self.store.history.get("daily_control", identifier)
        if (
            command["action"] == "daily-skip"
            and (previous or archived)
            and (previous or archived)["kind"] == "skip"
        ):
            value = previous or archived
            if value["profile"] != profile:
                raise APIError("not_found", "未知日常身份")
            return value, self.result(value)
        if args["expected_revision"] != revision:
            raise APIError("conflict", "配置版本已改变")
        candidates = self.next(profile)["occurrences"]
        target = next((c for c in candidates if c["occurrence_id"] == identifier), None)
        if target is None:
            state = self.store.scheduled_run(identifier)
            if (state and state["profile"] == profile) or (
                previous and previous["profile"] == profile
            ):
                raise APIError("conflict", "目标不再是尚未准备的下一次日常")
            raise APIError("not_found", "未知日常身份")
        if (
            args["expected_revision"] != revision
            or args["expected_state_version"] != target["state_version"]
        ):
            raise APIError("conflict", "配置或日常状态版本已改变")
        kind = "skip" if command["action"] == "daily-skip" else "postpone"
        due = args.get("not_before", target["due_at"])
        if kind == "postpone" and not target["due_at"] < due < target["expires_at"]:
            raise ValueError("推迟必须晚于当前到期时刻且早于原下一次排期")
        occurrence = ScheduledOccurrence(
            target["schedule_id"], profile, target["scheduled_at"], target["expires_at"]
        )
        value = {
            "id": identifier,
            "kind": kind,
            "profile": profile,
            "revision": revision,
            "version": target["state_version"] + 1,
            "due_at": due,
            "occurrence": asdict(occurrence),
        }
        return value, self.result(value)

    async def _temporary(self, identifier, profile, settings, args):
        now = time.time()
        due = args.get("not_before", now)
        if args["expires_at"] <= max(now, due):
            raise ValueError("expires_at 必须晚于当前时刻和 not_before")
        snapshot = self.daemon.configuration
        raw_settings = tomllib.loads(snapshot.content.decode("utf-8-sig"))
        if "schedule_id" in args:
            row = next(
                (
                    s
                    for s in raw_settings.get("schedules", [])
                    if s["id"] == args["schedule_id"] and profile in s["profiles"]
                ),
                None,
            )
            if row is None:
                raise ValueError("日程不存在或不属于目标账号")
            raw = {
                k: v
                for k, v in row.items()
                if k not in {"id", "profiles", "times", "weekdays", "enabled"}
            }
        else:
            raw = args["inline"]
            if set(raw) & {"id", "profiles", "times", "weekdays", "enabled"}:
                raise ValueError("inline 不接受周期日程字段")
        raw = json.loads(json.dumps(raw))
        raw.update(id="temporary-" + identifier, profiles=[profile], times=["00:00"])
        retry_definition = json.loads(json.dumps(raw))
        definition = parse_schedules([raw], {profile}, self.daemon.config)[0]
        # Resolve the identity now without acquiring execution resources or
        # requiring an already idle instance. No emulator is started here.
        binding = await asyncio.to_thread(resolve_daily_binding, settings, profile)
        directory = self.daemon.root / "daily-inputs" / identifier
        try:
            directory.mkdir(parents=True, exist_ok=False)
            count = 0

            def freeze(path):
                nonlocal count
                count += 1
                target = directory / f"{count}{path.suffix}"
                target.write_bytes(path.read_bytes())
                return str(target)

            for task_raw, task in zip(raw["tasks"], definition.tasks, strict=True):
                if not task.enabled:
                    continue
                for attr in ("file", "resource_file"):
                    if (path := getattr(task, attr)) is not None:
                        task_raw[attr] = freeze(path)
                if task.files:
                    task_raw["files"] = {name: freeze(path) for name, path in task.files}
                if task.resource_images:
                    task_raw["resource_images"] = {
                        name: freeze(path) for name, path in task.resource_images
                    }
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        binding_json = asdict(binding)
        binding_json["mumu_root"] = str(binding.mumu_root)
        binding_json["profile"]["gui_path"] = (
            str(binding.profile.gui_path) if binding.profile.gui_path else None
        )
        occurrence = ScheduledOccurrence(
            raw["id"], profile, due, args["expires_at"], "temporary-" + identifier, due
        )
        return {
            "id": occurrence.id,
            "kind": "temporary",
            "operation_id": identifier,
            "profile": profile,
            "revision": snapshot.revision,
            "configuration": snapshot.content.decode("utf-8-sig"),
            "definition": raw,
            "retry_definition": retry_definition,
            "binding": binding_json,
            "directory": str(directory),
            "version": 0,
            "due_at": due,
            "occurrence": asdict(occurrence),
        }

    def definition(self, identifier, settings):
        value = self.store.daily_control(identifier)
        if value and value["kind"] == "temporary":
            return parse_schedules([value["definition"]], {value["profile"]}, self.daemon.config)[0]
        return None

    def binding(self, identifier):
        value = self.store.daily_control(identifier)
        if not value or value["kind"] != "temporary":
            return None
        data = value["binding"]
        p = data["profile"]
        profile = Profile(
            p["name"],
            Path(p["gui_path"]) if p["gui_path"] else None,
            p["emulator_name"],
            p["auth_name"],
            p["character_name"],
            p["enabled"],
        )
        return UnitBinding(
            profile,
            Path(data["mumu_root"]),
            data["instance_index"],
            data["instance_name"],
        )

    def snapshot(self, identifier):
        value = self.store.daily_control(identifier)
        return (
            ConfigSnapshot(value["revision"], value["configuration"].encode())
            if value and value["kind"] == "temporary"
            else self.daemon.configuration
        )

    def result(self, value):
        o = value["occurrence"]
        result = {
            "occurrence_id": value["id"],
            "source": "temporary" if value["kind"] == "temporary" else "periodic",
            "scheduled_at": o["scheduled_at"],
            "due_at": value["due_at"],
            "expires_at": o["expires_at"],
            "configuration_revision": value["revision"],
            "state_version": value["version"],
        }
        if "retry_of" in value:
            result.update(retry_of=value["retry_of"], task_ids=value["task_ids"])
        if value["kind"] != "temporary":
            result["control"] = value["kind"]
        return result

    def reconcile(self):
        if not self.restored:
            published = {
                c["directory"] for c in self.store.daily_controls() if c["kind"] == "temporary"
            }
            root = self.daemon.root / "daily-inputs"
            if root.is_dir():
                for path in root.iterdir():
                    if path.is_dir() and not path.is_symlink() and str(path) not in published:
                        shutil.rmtree(path, ignore_errors=True)
            self.restored = True
        now = time.time()
        for value in self.store.daily_controls():
            if value["kind"] == "pause":
                continue
            identifier = value["id"]
            occurrence = ScheduledOccurrence(**value["occurrence"])
            if value["kind"] == "skip":
                if occurrence.scheduled_at <= now:
                    # Normal claiming at this point preserves earlier opportunities.
                    self.store.claim_schedule(occurrence, value["revision"])
                    self.store.schedule_update(identifier, "skipped", error="用户一次跳过")
                    self.scheduler._discard(identifier)
                    self.store.archive_daily_control(value)
                continue
            if value["kind"] == "postpone" and self.daemon.configuration is None:
                if occurrence.expires_at <= now:
                    self.store.event("daily_control_expired", {"occurrence_id": identifier})
                    self.store.archive_daily_control(value)
                continue
            if (
                value["kind"] == "postpone"
                and self.daemon.configuration is not None
                and value["revision"] != self.daemon.configuration.revision
            ):
                self.store.event(
                    "daily_control_invalidated",
                    {"occurrence_id": identifier, "reason": "configuration_changed"},
                )
                self.store.archive_daily_control(value)
                continue
            state = self.store.scheduled_run(identifier)
            if value["kind"] == "postpone" and state and state["status"] != "pending":
                self.store.archive_daily_control(value)
                continue
            if value["kind"] == "temporary":
                retry = self.scheduler.retries.current(identifier) or self.store.history.get(
                    "retry", identifier
                )
                status = state["status"] if state else "recovery_required"
                if retry and retry["status"] == "waiting":
                    status = "retry_waiting"
                operation = self.store.operation(value["operation_id"])
                result = {
                    **self.result(value),
                    "attempt_id": state.get("attempt_id") if state else None,
                    "retry_number": retry["number"] if retry else 0,
                    "retry_due_at": retry.get("due_at") if retry else None,
                    "reason": state.get("error") if state else "日常状态缺失",
                }
                final = status in TERMINAL | {"expired", "skipped"}
                operation_status = {
                    "pending": "waiting",
                    "retry_waiting": "waiting",
                    "expired": "failed",
                    "skipped": "succeeded",
                }.get(status, status)
                occupied = identifier in self.scheduler.preparing or any(
                    o.id == identifier for o, _ in self.scheduler.attempts.values()
                )
                if final and occupied:
                    # A terminal scheduling decision does not prove preparation
                    # or worker resources have been released.
                    final = False
                    operation_status = (
                        "cancelling" if operation and operation["cancel_requested"] else "preparing"
                    )
                if operation and (
                    operation["status"] != operation_status or operation["result"] != result
                ):
                    self.store.operation_update(
                        value["operation_id"],
                        operation_status,
                        result=result,
                        error=result["reason"],
                    )
                if final:
                    if status != "recovery_required":
                        self.store.archive_daily_control(value)
                        shutil.rmtree(value["directory"], ignore_errors=True)
                    continue
                if status in TERMINAL | {"expired", "skipped"}:
                    continue
            if state and state["status"] == "running":
                continue
            if occurrence.expires_at <= now:
                self.store.schedule_update(identifier, "expired", error="日常启动窗口已结束")
                self.scheduler._discard(identifier)
                if value["kind"] == "postpone":
                    self.store.archive_daily_control(value)
                continue
            if state is None:
                if occurrence.scheduled_at > now:
                    continue
                if not self.store.claim_schedule(occurrence, value["revision"]):
                    continue
                state = self.store.scheduled_run(identifier)
            if (
                state
                and state["status"] == "pending"
                and value["kind"] == "postpone"
                and identifier in self.scheduler.pending
                and identifier not in self.scheduler.preparing
            ):
                pending, snapshot = self.scheduler.pending[identifier]
                self.scheduler.pending[identifier] = (
                    replace(pending, not_before=value["due_at"]),
                    snapshot,
                )
            if (
                state
                and state["status"] == "pending"
                and identifier not in self.scheduler.pending
                and identifier not in self.scheduler.preparing
                and not any(o.id == identifier for o, _ in self.scheduler.attempts.values())
            ):
                self.scheduler.pending[identifier] = (
                    replace(occurrence, not_before=value["due_at"]),
                    self.snapshot(identifier),
                )

    async def cancel(self, identifier):
        operation = self.store.operation(identifier)
        occurrence_id = operation["result"]["occurrence_id"]
        self.store.request_cancellation(identifier)
        retry = self.scheduler.retries.current(occurrence_id)
        if retry:
            self.scheduler.retries.update_status(retry, "cancelled")
        preparing = self.scheduler.preparing.get(occurrence_id)
        if preparing is not None:
            self.store.operation_update(identifier, "cancelling", result=operation["result"])
        self.scheduler._discard(occurrence_id)
        if preparing is not None:
            await asyncio.gather(preparing, return_exceptions=True)
        active = [
            (aid, self.daemon.running.get(aid))
            for aid, (o, _) in self.scheduler.attempts.items()
            if o.id == occurrence_id
        ]
        if active:
            for _, worker in active:
                if worker:
                    await worker[0].cancel()
        else:
            self.store.schedule_update(occurrence_id, "cancelled", error="用户取消临时日常")
            self.reconcile()
        self.daemon.wakeup.set()
        return self.daemon.operations.get(identifier)

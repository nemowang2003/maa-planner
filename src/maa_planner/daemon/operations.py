"""Operation lifecycle, admission and result publication; no transport dependencies."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from maa_planner.daemon.commands import MUTATING, BatchSubmission, Submission
from maa_planner.daemon.copilot_command import CopilotCommand
from maa_planner.schema import ADMIN_CHANNEL, TERMINAL, APIError


def json_value(value):
    from maa_planner.daemon.planning.skland import json_default

    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            default=lambda item: str(item) if isinstance(item, Path) else json_default(item),
        )
    )


class OperationService:
    def __init__(
        self,
        *,
        store,
        configuration,
        execution,
        observations,
        notifications,
        wakeup,
        update_game,
        daily=None,
    ):
        self.daily = daily
        self._store = store
        self.configuration = configuration
        self.execution = execution
        self.observations = observations
        self.notifications = notifications
        self.wakeup = wakeup
        self.update_game = update_game
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.submissions: set[str] = set()
        self._apk_results: dict[str, tuple[object, str | None, float]] = {}

    @property
    def store(self):
        return self._store()

    def settings(self):
        return self.configuration.settings()

    def find_key(self, key):
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError("idempotency_key 必须为 1～200 字符字符串")
        return self.store.find_operation_key(key)

    def find(self, key):
        identifier = self.find_key(key)
        return self.get(identifier) if identifier else None

    async def accept(self, command, *, idempotency_key):
        identifier = self.find_key(idempotency_key) or uuid.uuid4().hex
        if command["action"].startswith("daily-"):
            assert self.daily is not None
            return await self.daily().control.accept(identifier, command, idempotency_key)
        return await self.submit(
            {"request_id": identifier, **command}, idempotency_key=idempotency_key
        )

    def accept_orundum(self, params):
        # Resolve committed repeats before consulting current configuration.
        existing = self.find_key(params["idempotency_key"])
        if existing:
            previous = self.get(existing)
            if previous["action"] != "orundum-request" or previous["target"] != params["profile"]:
                raise APIError("conflict", "去重键已存在但参数不同")
            return previous
        if self.execution.stopping or self.execution.draining:
            raise APIError("unavailable", "daemon 正在停止或排空")
        assert self.daily is not None
        return self.daily().orundum.register(
            self.settings(), profile=params["profile"], key=params["idempotency_key"]
        )

    async def accept_batch(self, jobs, *, idempotency_key):
        identifier = self.find_key(idempotency_key) or uuid.uuid4().hex
        return await self.submit_batch(
            {"request_id": identifier, "jobs": jobs}, idempotency_key=idempotency_key
        )

    def state_event(self, kind: str, data: dict) -> None:
        from maa_planner.daemon.control_contract import public_operation

        if kind == "alert":
            transition = data["transition"]
            self.notifications.publish(
                {
                    "kind": "alert." + transition,
                    "profile": data["profile"],
                    "message": (
                        "管理员" if data["profile"] == ADMIN_CHANNEL else f"账号 {data['profile']}"
                    )
                    + f"：{data['category']} 告警"
                    + ("已解除" if transition == "resolved" else "已触发"),
                    "data": data,
                }
            )
        elif kind == "operation":
            identifier = data["operation_id"]
            if owner := self.notifications.owners.get(identifier):
                operation = self.store.operation(identifier)
                if operation is not None:
                    if operation["action"] in {"game-check", "game-update"}:
                        operation = self.get(identifier)
                    operation.pop("arguments", None)
                    self.notifications.notify(
                        owner,
                        {
                            "kind": "operation.updated",
                            "profile": operation.get("target"),
                            "data": public_operation(operation),
                        },
                    )
                    if operation["status"] in TERMINAL:
                        self.notifications.owners.pop(identifier, None)
        self.wakeup()

    def refresh_batches(self) -> None:
        for identifier in self.store.unfinished_batches():
            self.refresh(identifier)

    def remember_apk_result(self, identifier: str, result, error=None) -> None:
        now = time.monotonic()
        self._apk_results = {
            key: value for key, value in self._apk_results.items() if value[2] > now
        }
        if len(self._apk_results) >= 16:
            self._apk_results.pop(next(iter(self._apk_results)))
        self._apk_results[identifier] = (result, error, now + 300)

    def get(self, identifier: str) -> dict:
        return self._read(identifier, reconcile=False)

    def refresh(self, identifier: str) -> dict:
        return self._read(identifier, reconcile=True)

    def _read(self, identifier: str, *, reconcile: bool) -> dict:
        operation = self.store.operation(identifier)
        if operation is None:
            raise APIError("not_found", "未知 operation_id", {"operation_id": identifier})
        if operation["action"] == "batch":
            children = (operation["result"] or {}).get("operations", [])
            members = [
                self.get(child)
                if self.store.operation(child) is not None
                else {"operation_id": child, "status": "preparing"}
                for child in children
            ]
            statuses = [member["status"] for member in members]
            if statuses and all(status in TERMINAL for status in statuses):
                status = (
                    "succeeded"
                    if all(s == "succeeded" for s in statuses)
                    else "cancelled"
                    if all(s == "cancelled" for s in statuses)
                    else "failed"
                )
                if reconcile and operation["status"] != status:
                    self.store.operation_update(identifier, status, result={"operations": children})
                operation["status"] = status
            operation["result"] = {
                "operations": children,
                "members": [
                    {
                        key: member[key]
                        for key in ("operation_id", "status", "attempt_id")
                        if key in member
                    }
                    for member in members
                ],
            }
        if operation["action"] in {"copilot", "run"}:
            delegated = self.store.copilot(identifier)
            if delegated is not None:
                status = delegated["status"]
                if (
                    operation["cancel_requested"]
                    and status == "failed"
                    and delegated.get("error") is not None
                ):
                    status = "cancelled"
                error = delegated.get("error")
                result = delegated.get("result")
                if reconcile and (
                    status != operation["status"]
                    or result != operation["result"]
                    or error != operation["error"]
                ):
                    self.store.operation_update(identifier, status, result=result, error=error)
                    operation = self.store.operation(identifier)
                    assert operation is not None
                operation["attempt_id"] = delegated["attempt_id"]
                operation["runtime"] = self.store.prepared_runtime(delegated["attempt_id"])
        if operation["action"] in {"game-check", "game-update"}:
            details = self._apk_results.get(identifier)
            if details is not None and details[2] <= time.monotonic():
                self._apk_results.pop(identifier, None)
                details = None
            operation["result_available"] = details is not None
            operation["result"] = details[0] if details is not None else None
            if details is not None:
                operation["error"] = details[1]
        operation.pop("arguments", None)
        return operation

    @property
    def maintenance(self):
        return self.execution.maintenance

    async def submit_batch(self, params: dict, *, idempotency_key=None) -> dict:
        batch = BatchSubmission.from_dict(params)
        payload = {
            "action": "batch",
            "target": None,
            "arguments": {
                "jobs": [
                    {
                        "action": j.action,
                        "target": j.target,
                        "arguments": j.arguments,
                        "expires_at": j.expires_at,
                    }
                    for j in batch.jobs
                ]
            },
            "expires_at": None,
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        if self.store.operation(batch.request_id) is not None:
            self.store.reserve_operation(batch.request_id, payload)
            return self.get(batch.request_id)
        self.execution.available()
        settings = self.settings()
        snapshot = self.configuration.snapshot
        assert snapshot is not None
        for job in batch.jobs:
            if not any(p.name == job.target and p.enabled for p in settings.profiles):
                raise ValueError("批次包含未知或禁用账号")
        preparation = self.execution.select_preparation(settings)
        self.execution.reserve(batch.request_id)
        try:
            payload["configuration_revision"] = snapshot.revision
            self.store.reserve_operation(batch.request_id, payload)
            children = [uuid.uuid4().hex for _ in batch.jobs]
            self.store.operation_update(
                batch.request_id, "running", result={"operations": children}
            )
            for identifier, job in zip(children, batch.jobs):
                self.store.reserve_operation(
                    identifier,
                    {
                        "action": job.action,
                        "target": job.target,
                        "arguments": job.arguments,
                        "expires_at": job.expires_at,
                        "configuration_revision": snapshot.revision,
                    },
                )
            self.submissions.add(batch.request_id)
            self.execution.retain_preparation(batch.request_id, preparation)

            async def submit_child(identifier, job):
                try:
                    await self.submit(
                        {
                            "request_id": identifier,
                            "action": job.action,
                            "target": job.target,
                            "arguments": job.arguments,
                            "expires_at": job.expires_at,
                        },
                        preparation=preparation,
                        accepted=(settings, snapshot),
                        reserved=True,
                    )
                    parent = self.store.operation(batch.request_id)
                    if parent and parent["cancel_requested"]:
                        await self.cancel(identifier)
                except Exception as error:
                    # A failed member does not cancel independently valid members.
                    if self.store.operation(identifier) is None:
                        self.store.reserve_operation(
                            identifier,
                            {
                                "action": job.action,
                                "target": job.target,
                                "arguments": job.arguments,
                                "expires_at": job.expires_at,
                                "configuration_revision": snapshot.revision,
                            },
                        )
                    self.store.operation_update(identifier, "failed", error=str(error))

            async def accept_members():
                try:
                    await asyncio.gather(
                        *(
                            submit_child(identifier, job)
                            for identifier, job in zip(children, batch.jobs)
                        )
                    )
                finally:
                    self.submissions.discard(batch.request_id)
                    self.execution.release(batch.request_id)
                    self.tasks.pop(batch.request_id, None)
                    self.wakeup()

            self.tasks[batch.request_id] = asyncio.create_task(accept_members())
        except BaseException:
            self.submissions.discard(batch.request_id)
            self.execution.release(batch.request_id)
            raise
        return self.get(batch.request_id)

    async def submit(
        self,
        params: dict,
        *,
        preparation=None,
        accepted=None,
        reserved=False,
        automatic=False,
        idempotency_key=None,
    ) -> dict:
        submission = Submission.from_dict(params)
        identifier, action = submission.request_id, submission.action
        target, arguments, deadline = submission.target, submission.arguments, submission.expires_at
        payload = {
            "action": action,
            "target": target,
            "arguments": arguments,
            "expires_at": deadline,
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        existing = self.store.operation(identifier)
        if existing is not None and not reserved:
            self.store.reserve_operation(identifier, payload)
            return self.get(identifier)
        if reserved and existing is not None and existing["cancel_requested"]:
            self.store.operation_update(identifier, "cancelled")
            return self.get(identifier)
        if self.execution.stopping or (
            self.execution.draining and not (reserved and accepted is not None)
        ):
            raise RuntimeError("daemon 正在停止或排空，拒绝新操作")
        if deadline is not None and deadline <= time.time():
            raise ValueError("请求已过期，未执行")
        if self.maintenance is not None:
            raise APIError("busy", "维护进行中，拒绝新操作", {"operation_id": self.maintenance})
        settings, snapshot = accepted or (self.settings(), self.configuration.snapshot)
        assert snapshot is not None
        payload["configuration_revision"] = snapshot.revision
        preparation_settings = settings
        if target is not None:
            profiles = tuple(p for p in settings.profiles if p.name == target)
            if not profiles:
                raise ValueError("未知 target")
            if not profiles[0].enabled:
                raise ValueError("目标账号已禁用")
            settings = replace(settings, profiles=profiles)
        if action in {"copilot", "run"}:
            preparation = preparation or self.execution.select_preparation(preparation_settings)
        if action == "copilot":
            command = CopilotCommand.from_dict(
                {"request_id": identifier, "profile": target, **arguments}
            )
            if "request_id" in arguments or "profile" in arguments:
                raise ValueError("arguments 不允许覆盖 request_id 或 target")
            self.execution.reserve(identifier, accepted=reserved and accepted is not None)
            self.execution.retain_preparation(identifier, preparation)
            try:
                self.store.reserve_operation(identifier, payload)
                self.submissions.add(identifier)
                await self.execution.submit_copilot(
                    command,
                    expires_at=deadline,
                    preparation=preparation,
                    accepted_snapshot=snapshot,
                )
            except Exception as error:
                operation = self.store.operation(identifier)
                if operation is not None:
                    self.store.operation_update(
                        identifier,
                        "cancelled" if operation["cancel_requested"] else "failed",
                        error=str(error),
                    )
                raise
            finally:
                self.submissions.discard(identifier)
                self.execution.release(identifier)
                self.wakeup()
            return self.get(identifier)
        if action == "run":
            self.execution.reserve(identifier, accepted=reserved and accepted is not None)
            self.execution.retain_preparation(identifier, preparation)
            try:
                self.store.reserve_operation(identifier, payload)
                self.submissions.add(identifier)
                await self.execution.submit_run(
                    identifier,
                    settings,
                    target,
                    arguments,
                    deadline,
                    preparation=preparation,
                    accepted_snapshot=snapshot,
                )
            except Exception as error:
                operation = self.store.operation(identifier)
                if operation is not None:
                    self.store.operation_update(
                        identifier,
                        "cancelled" if operation["cancel_requested"] else "failed",
                        error=str(error),
                    )
                raise
            finally:
                self.submissions.discard(identifier)
                self.execution.release(identifier)
                self.wakeup()
            return self.get(identifier)
        self.execution.reserve(identifier, accepted=automatic, exclusive=action in MUTATING)
        try:
            self.store.reserve_operation(identifier, payload)
            assert self.configuration.snapshot is not None
            cancel = threading.Event()
            self.cancel_events[identifier] = cancel

            async def execute():
                try:
                    if cancel.is_set():
                        raise asyncio.CancelledError()
                    if deadline is not None and time.time() >= deadline:
                        raise ValueError("请求已过期，未执行")
                    self.store.operation_update(identifier, "running")
                    if action == "game-update":
                        result = json_value(
                            await self.update_game(
                                settings,
                                cancel,
                                automatic=automatic,
                                revision=snapshot.revision,
                            )
                        )
                    else:
                        result = json_value(
                            await self.observations.execute(action, settings, cancel, arguments)
                        )
                    if action in {"game-check", "game-update"}:
                        self.remember_apk_result(identifier, result)
                        result = {"ephemeral": True}
                    self.store.operation_update(
                        identifier,
                        "cancelled" if cancel.is_set() else "succeeded",
                        result=result,
                    )
                except asyncio.CancelledError:
                    self.store.operation_update(identifier, "cancelled")
                except Exception as error:
                    reason = str(error)
                    if action in {"game-check", "game-update"}:
                        report = getattr(error, "report", None)
                        self.remember_apk_result(
                            identifier, asdict(report) if report is not None else None, reason
                        )
                        reason = "APK 操作失败；具体错误仅在当前进程有效"
                    self.store.operation_update(
                        identifier, "cancelled" if cancel.is_set() else "failed", error=reason
                    )
                finally:
                    self.tasks.pop(identifier, None)
                    self.cancel_events.pop(identifier, None)
                    self.execution.release(identifier)
                    self.wakeup()

            self.tasks[identifier] = asyncio.create_task(execute())
        except BaseException:
            self.cancel_events.pop(identifier, None)
            self.execution.release(identifier)
            raise
        return self.get(identifier)

    async def cancel(self, identifier: str) -> dict:
        operation = self.get(identifier)
        if operation["status"] in TERMINAL:
            return operation
        if operation["action"] in {"daily-run", "daily-retry"}:
            assert self.daily is not None
            return await self.daily().control.cancel(identifier)
        if operation["action"] in MUTATING:
            raise APIError(
                "conflict", "维护操作不能中途取消；请等待安全完成", {"operation_id": identifier}
            )
        self.store.request_cancellation(identifier)
        if operation["action"] == "batch":
            for child in operation["result"]["operations"]:
                if self.store.operation(child) is not None:
                    await self.cancel(child)
            return self.get(identifier)
        if identifier in self.cancel_events:
            self.cancel_events[identifier].set()
        else:
            await self.execution.cancel_execution(identifier)
        return self.get(identifier)

    async def close(self):
        for identifier, cancel in self.cancel_events.items():
            operation = self.store.operation(identifier)
            if operation is not None and operation["action"] not in MUTATING:
                cancel.set()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

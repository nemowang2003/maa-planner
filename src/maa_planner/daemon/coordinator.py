"""Event-driven coordinator with configured daily scheduling and explicit operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from maa_planner.daemon.config import Settings, settings_from_bytes
from maa_planner.daemon.config_watch import ConfigWatch
from maa_planner.daemon.copilot_command import CopilotCommand
from maa_planner.daemon.execution.context import CancelledRun, RunRequest, RunResult
from maa_planner.daemon.execution.copilot import preflight_copilot, prepare_copilot
from maa_planner.daemon.notifications import NotificationHub
from maa_planner.daemon.policy import ConfigSnapshot, Context, DailyPolicy, Policy
from maa_planner.daemon.preparation import Preparation, prepare_environment
from maa_planner.daemon.scheduling import DailyScheduler
from maa_planner.daemon.state import StateStore
from maa_planner.daemon.tools.game_update import GameUpdateReport
from maa_planner.daemon.tools.game_update import check_game_updates as inspect_game_updates
from maa_planner.daemon.tools.locks import emulator_resource, lock_file, state_directory
from maa_planner.daemon.transport import ControlServer, Handler
from maa_planner.daemon.worker import WorkerClient
from maa_planner.schema import APIError

log = logging.getLogger(__name__)


class PendingExecution:
    """Cancellation handle while the daemon selects immutable runtime dependencies."""

    def __init__(self, request: RunRequest):
        self.request = request
        self.cancel_requested = False

    async def cancel(self):
        self.cancel_requested = True


class Daemon:
    def __init__(
        self,
        config: Path,
        endpoint_path: Path,
        *,
        root: Path | None = None,
        policy: Policy | None = None,
        handler: Handler | None = None,
        worker_factory: Callable = WorkerClient,
        reconcile_interval: float | None = None,
    ):
        if reconcile_interval is not None and reconcile_interval <= 0:
            raise ValueError("协调时间间隔必须大于零")
        self.config = config
        self.root = root or state_directory()
        self.policy = policy or DailyPolicy()
        self.notifications = NotificationHub()
        self.server = ControlServer(
            endpoint_path, handler or self.handle_message, notifications=self.notifications
        )
        self.worker_factory = worker_factory
        self._config_dirty = True
        self._config_lock = asyncio.Lock()
        self.reconcile_interval = reconcile_interval
        self.hosted = False
        self.configuration: ConfigSnapshot | None = None
        self.config_error: str | None = None
        self._settings: Settings | None = None
        self.store: StateStore | None = None
        self.running: dict[str, tuple[WorkerClient | PendingExecution, asyncio.Task]] = {}
        self.wakeup = asyncio.Event()
        self.stopping = asyncio.Event()
        self.game_updates: GameUpdateReport | None = None
        self.game_update_error: str | None = None
        self._apk_cancel = threading.Event()
        self._apk_checks: dict[tuple, asyncio.Task] = {}
        self._runtime_checks: dict[str, asyncio.Task] = {}
        self._preparations: dict[str, Preparation] = {}
        self._active_preparations: dict[str, Preparation] = {}
        self._resource_users: dict[str, str | None] = {}
        self._maintenance_owner: str | None = None
        self._maintenance_pending = False
        self._copilot_preparations: dict[str, Preparation] = {}
        self.activity = None
        self.preparation_status: dict = {}
        self._runtime_update_lock = asyncio.Lock()
        self._default_deadlines: set[str] = set()
        self._changes: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=256)
        self._preparation_cancel: dict[str, threading.Event] = {}
        self._deadlines: dict[str, float] = {}
        self.draining = False
        self.restart_requested = False
        self.started_at = time.time()
        from maa_planner.daemon.api import ControlAPI
        from maa_planner.daemon.control_services import (
            ConfigurationService,
            ControlService,
            ExecutionAccess,
        )
        from maa_planner.daemon.observations import ObservationService
        from maa_planner.daemon.operations import OperationService

        self.control_configuration = ConfigurationService(
            snapshot=lambda: self.configuration,
            error=lambda: self.config_error,
            path=self.config,
            replace=self.replace_configuration,
        )
        self.observations = ObservationService(
            store=self.control_store,
            activity_snapshot=self.activity_snapshot,
            check_game_updates=self.check_game_updates,
            observe_preparation=self.observe_preparation,
            recovery_root=self.root / "recovery",
        )
        self.operations = OperationService(
            store=self.control_store,
            configuration=self.control_configuration,
            execution=ExecutionAccess(self),
            observations=self.observations,
            notifications=self.notifications,
            wakeup=self.wakeup.set,
            update_game=self.perform_game_update,
            daily=lambda: self.daily,
        )
        self.api = ControlAPI(
            operations=self.operations,
            notifications=self.notifications,
            control=ControlService(
                store=self.control_store,
                configuration=self.control_configuration,
                status=self.control_status,
                drain=self.request_drain,
                daily=lambda: self.daily,
            ),
        )
        self._copilot_tasks: dict[str, asyncio.Task] = {}
        self._immediate: dict[str, str] = {}
        self.daily = DailyScheduler(self)
        from maa_planner.daemon.game_maintenance import GameUpdateScheduler

        self.game_maintenance = GameUpdateScheduler(self)
        from maa_planner.daemon.cache_maintenance import CacheMaintenance

        self.cache_maintenance = CacheMaintenance(self)

    def control_store(self):
        if self.store is None:
            raise RuntimeError("daemon 尚未就绪")
        return self.store

    async def perform_game_update(self, settings, cancel, **kwargs):
        from maa_planner.daemon.game_maintenance import perform_update

        return await perform_update(self, settings, cancel, **kwargs)

    def control_status(self) -> dict:
        import os

        daemon = self
        return {
            "pid": os.getpid(),
            "hosting": "windows-task" if daemon.hosted else "manual",
            "started_at": daemon.started_at,
            "draining": daemon.draining,
            "stopping": daemon.stopping.is_set(),
            "restart_requested": daemon.restart_requested,
            "configuration_revision": daemon.configuration.revision
            if daemon.configuration
            else None,
            "configuration_error": daemon.config_error,
            "running": list(daemon.running),
            "preparing": list(daemon._copilot_tasks) + list(daemon.daily.preparing),
            "scheduled_waiting": [
                key for key in daemon.daily.pending if key not in daemon.daily.preparing
            ],
            "daily_paused_profiles": [
                c["profile"]
                for c in self.control_store().daily_controls()
                if c["kind"] == "pause" and c["paused"]
            ],
            "scheduled_retries": [
                {k: v for k, v in retry.items() if k not in {"request", "occurrence"}}
                for retry in self.control_store().scheduled_retries()
                if retry["status"] in {"waiting", "running", "recovery_required"}
            ],
            "operations": list(self.operations.tasks),
            "submitting": sorted(self.operations.submissions),
            "maintenance": self.operations.maintenance,
            "game_update": daemon.game_maintenance.status(),
            "preparation": daemon.preparation_status,
        }

    async def handle_message(self, sid: str, message: dict) -> dict:
        return await self.api.handle(message, session_id=sid)

    def request_drain(self, *, restart: bool = False, force: bool = False) -> None:
        self.draining = True
        self.restart_requested = restart
        if force:
            self.request_stop(reason="restart --force" if restart else "stop --force")
        self.wakeup.set()

    def available(self, *, accepted=False, accepted_configuration=False) -> None:
        if self.stopping.is_set() or (self.draining and not accepted):
            raise RuntimeError("daemon 正在停止或排空")
        if self._maintenance_owner is not None or (self._maintenance_pending and not accepted):
            raise APIError("busy", "维护进行中或已到期，拒绝新任务")
        if self.config_error is not None and not (accepted or accepted_configuration):
            raise ValueError("配置当前无效，拒绝新任务")

    async def cancel_execution(self, identifier: str) -> None:
        if event := self._preparation_cancel.get(identifier):
            event.set()
        value = self.operations.store.copilot(identifier)
        if value is not None and value["attempt_id"] in self.running:
            await self.running[value["attempt_id"]][0].cancel()
        self.wakeup.set()

    async def submit_run(
        self,
        identifier,
        settings,
        target,
        arguments,
        deadline,
        *,
        preparation=None,
        accepted_snapshot=None,
    ):
        from maa_planner.daemon.execution.context import RunOptions
        from maa_planner.daemon.execution.copilot import resolve_binding
        from maa_planner.daemon.execution.runtime import CoreRun
        from maa_planner.daemon.execution.tasks import CoreTasks

        if set(arguments) - {"plan", "options", "execution_timeout"} or "plan" not in arguments:
            raise ValueError("run 需要内嵌 plan，可选 options、execution_timeout")
        if not isinstance(arguments["plan"], dict):
            raise ValueError("plan 必须为内嵌 JSON 对象")
        plan = dict(arguments["plan"])
        files = plan.pop("attachments", {})
        tasks = CoreTasks(json.dumps(plan), json.dumps(files))
        pure_copilot = all(
            t["type"] in {"Copilot", "SSSCopilot", "ParadoxCopilot"}
            for t in json.loads(tasks.document)["tasks"]
        )
        self.available(accepted=accepted_snapshot is not None)
        snapshot = accepted_snapshot or self.configuration
        assert snapshot is not None
        binding = await asyncio.to_thread(resolve_binding, settings, target, pure_copilot)
        self.available(accepted=accepted_snapshot is not None)
        operation = self.operations.store.operation(identifier)
        if operation is not None and operation["cancel_requested"]:
            raise RuntimeError("请求准备已取消")
        if deadline is not None and deadline <= time.time():
            raise ValueError("请求已过期，未执行")
        payload = {"action": "run", "target": target, "arguments": arguments}
        if self.operations.store.copilot(identifier, payload) is not None:
            return
        unit = emulator_resource(binding.mumu_root, binding.instance_index).key
        attempt = "run-" + uuid.uuid4().hex
        if not self.claim_unit("attempt:" + attempt, unit, accepted=accepted_snapshot is not None):
            raise APIError("busy", "实例已占用；即时请求不排队", {"profile": target})
        request = RunRequest(
            attempt,
            attempt,
            snapshot.revision,
            binding,
            RunOptions(
                allow_emulator_start=True,
                execution_timeout=arguments.get("execution_timeout", 7200),
            ),
            core=CoreRun(
                settings.runtime_template(), tasks, json.dumps(arguments.get("options", {}))
            ),
        )
        try:
            self.operations.store.reserve_copilot(identifier, payload, attempt)
            self._deadlines[identifier] = deadline if deadline is not None else time.time() + 60
            if deadline is None:
                self._default_deadlines.add(identifier)
            self._immediate[attempt] = identifier
            self._launch(request, preparation=preparation or self.select_preparation(settings))
        except BaseException:
            self.release_resources("attempt:" + attempt)
            self._deadlines.pop(identifier, None)
            self._default_deadlines.discard(identifier)
            self._immediate.pop(attempt, None)
            raise
        self.wakeup.set()

    async def submit_copilot(
        self,
        command: CopilotCommand,
        *,
        expires_at: float | None = None,
        preparation=None,
        accepted_snapshot=None,
    ) -> dict:
        """Explicit immediate action, independent of configured daily schedules."""
        if self.store is None or self.configuration is None or self.stopping.is_set():
            raise RuntimeError("daemon 尚未运行或正在停止")
        prior = self.store.copilot(command.request_id, command.to_dict())
        if prior is not None:
            return prior
        self.available(accepted=accepted_snapshot is not None)
        snapshot = accepted_snapshot or self.configuration
        settings = settings_from_bytes(snapshot.content, self.config)
        preparation = preparation or self.select_preparation(settings)
        try:
            binding = await asyncio.wait_for(
                asyncio.to_thread(preflight_copilot, settings, command), 8
            )
        except TimeoutError as error:
            raise RuntimeError("copilot 游戏运行状态检查超时；未启动任务") from error
        self.available(accepted=accepted_snapshot is not None)
        operation = self.operations.store.operation(command.request_id)
        if operation is not None and operation["cancel_requested"]:
            raise RuntimeError("请求准备已取消")
        if expires_at is not None and expires_at <= time.time():
            raise ValueError("请求已过期，未执行")
        # Recheck after yielding: another socket client may have claimed this slot/ID.
        prior = self.store.copilot(command.request_id, command.to_dict())
        if prior is not None:
            return prior
        unit = emulator_resource(binding.mumu_root, binding.instance_index).key
        if not self.claim_unit(
            "copilot:" + command.request_id, unit, accepted=accepted_snapshot is not None
        ):
            raise APIError(
                "busy", "实例已被占用；即时 copilot 不排队", {"profile": command.profile}
            )
        attempt_id = "copilot-" + uuid.uuid4().hex
        try:
            self.store.reserve_copilot(command.request_id, command.to_dict(), attempt_id)
        except BaseException:
            self.release_resources("copilot:" + command.request_id)
            raise
        self.retain_preparation("copilot:" + command.request_id, preparation)
        self._deadlines[command.request_id] = (
            expires_at if expires_at is not None else time.time() + 60
        )
        if expires_at is None:
            self._default_deadlines.add(command.request_id)
        cancel = threading.Event()
        self._preparation_cancel[command.request_id] = cancel
        self._copilot_preparations[command.request_id] = preparation

        async def prepare():
            try:
                began = time.monotonic()
                environment = await preparation.get(self.prepare_dependencies)
                if command.request_id in self._default_deadlines:
                    self._deadlines[command.request_id] += time.monotonic() - began
                if cancel.is_set():
                    raise CancelledRun("准备已取消")
                request = await asyncio.to_thread(
                    prepare_copilot,
                    replace(
                        settings, core_runtime=self.runtime_options(environment.runtime, settings)
                    ),
                    command,
                    binding,
                    snapshot.revision,
                    attempt_id,
                    cancel,
                )
                payload = (command.request_id, request, None)
            except Exception as error:
                payload = (command.request_id, None, str(error))
            await self._changes.put(("copilot_ready", payload))
            self.wakeup.set()

        self._copilot_tasks[command.request_id] = asyncio.create_task(prepare())
        self.wakeup.set()
        result = self.store.copilot(command.request_id)
        assert result is not None
        return result

    def context(self) -> Context:
        assert self.configuration is not None
        return Context(
            self.configuration,
            tuple(self.running),
            time.time(),
            self.game_updates,
            self.game_update_error,
            self.activity,
            self._settings.schedules if self._settings else (),
            tuple(p.name for p in self._settings.profiles if p.enabled) if self._settings else (),
        )

    def request_stop(self, *, reason: str | None = None) -> None:
        interrupted = reason is not None and not self.stopping.is_set()
        self.stopping.set()
        self._apk_cancel.set()
        for event in self._preparation_cancel.values():
            event.set()
        self.wakeup.set()
        if interrupted:
            log.warning("daemon 中断，停止执行并保留模拟器现场: %s", reason)
            if self.store is not None:
                self.store.event("daemon_interrupted", {"reason": reason})
                for identifier, (worker, _) in self.running.items():
                    self.store.observe_alert(
                        worker.request.binding.profile.name,
                        "execution_interrupted",
                        identifier,
                        {"reason": reason, "attempt_id": identifier},
                        now=time.time(),
                    )

    async def check_game_updates(self, settings: Settings) -> GameUpdateReport:
        """Daemon-owned single-flight observation; the 11:00 maintenance schedule is separate."""
        if self.store is None or self.stopping.is_set():
            raise RuntimeError("daemon 尚未运行或正在停止")
        key = (
            settings.config_file,
            settings.profiles,
            settings.mumu_path,
            settings.game_cache,
        )
        if key not in self._apk_checks:

            async def refresh():
                try:
                    report = await asyncio.to_thread(
                        inspect_game_updates,
                        settings,
                        cancel=self._apk_cancel,
                    )
                except Exception as error:
                    if not self.stopping.is_set():
                        await self._changes.put(("apk_error", str(error)))
                        self.wakeup.set()
                    raise
                if not self.stopping.is_set():
                    await self._changes.put(("apk_report", report))
                    self.wakeup.set()
                return report

            task = asyncio.create_task(refresh())
            self._apk_checks[key] = task

            def completed(done):
                self._apk_checks.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Retrieve an error even if its caller was cancelled.

            task.add_done_callback(completed)
        return await asyncio.shield(self._apk_checks[key])

    def configuration_changed(self) -> None:
        self._config_dirty = True
        self.wakeup.set()

    def _accept_configuration(self, content: bytes) -> None:
        settings = settings_from_bytes(content, self.config)
        revision = hashlib.sha256(content).hexdigest()
        if self.configuration is None or revision != self.configuration.revision:
            assert self.store is not None
            self.store.configuration(revision, content)
            if self.configuration is not None:
                self.store.event("configuration", {"revision": revision})
            self.configuration = ConfigSnapshot(revision, content)
            log.info("配置已接纳: %s", revision)
        self.config_error = None
        self._settings = settings

    async def _read_configuration(self) -> None:
        try:
            content = await asyncio.to_thread(self.config.read_bytes)
            self._accept_configuration(content)
        except (OSError, UnicodeError, ValueError) as error:
            message = str(error)
            if message != self.config_error:
                log.warning("配置未接纳，保留当前有效快照: %s", error)
            self.config_error = message

    async def _configuration(self) -> None:
        async with self._config_lock:
            await self._read_configuration()

    async def replace_configuration(self, content: str, expected_revision: str | None) -> None:
        from maa_planner.daemon.tools.storage import atomic_write

        async with self._config_lock:
            if self.stopping.is_set() or self.draining:
                raise RuntimeError("daemon 正在停止或排空")
            encoded = content.encode("utf-8")
            settings_from_bytes(encoded, self.config)
            # Observe any pending external edit before checking a client's revision.
            await self._read_configuration()
            current = self.configuration.revision if self.configuration else None
            if expected_revision is not None and expected_revision != current:
                raise APIError(
                    "conflict",
                    "配置版本冲突，请重新读取配置",
                    {"expected_revision": expected_revision, "actual_revision": current},
                )
            await asyncio.to_thread(atomic_write, self.config, encoded)
            self._accept_configuration(encoded)
            self.wakeup.set()

    @property
    def resources_busy(self) -> bool:
        return bool(self._resource_users or self._maintenance_owner)

    @property
    def maintenance(self) -> str | None:
        owner = self._maintenance_owner
        return owner.removeprefix("operation:") if owner is not None else None

    @property
    def maintenance_pending(self) -> bool:
        return self._maintenance_pending

    def set_maintenance_pending(self, pending: bool) -> None:
        self._maintenance_pending = pending

    def reserve_resources(
        self, owner: str, *, accepted=False, accepted_configuration=False, exclusive=False
    ) -> None:
        """Check and register without yielding; callers own their task and cancellation."""
        self.available(
            accepted=accepted or owner in self._resource_users,
            accepted_configuration=accepted_configuration,
        )
        if exclusive:
            if self.resources_busy:
                raise APIError("busy", "存在执行或准备中的操作，维护不排队")
            self._maintenance_owner = owner
        else:
            self._resource_users.setdefault(owner, None)

    def release_resources(self, owner: str) -> None:
        self._resource_users.pop(owner, None)
        self._active_preparations.pop(owner, None)
        if self._maintenance_owner == owner:
            self._maintenance_owner = None
        self.wakeup.set()

    def claim_unit(self, owner: str, unit: str, *, accepted=False) -> bool:
        self.available(accepted=accepted or owner in self._resource_users)
        if any(key != owner and value == unit for key, value in self._resource_users.items()):
            return False
        self._resource_users[owner] = unit
        return True

    def _unit_busy(self, unit: str) -> bool:
        return unit in self._resource_users.values()

    def retain_preparation(self, owner: str, preparation: Preparation) -> None:
        self._active_preparations[owner] = preparation

    def release_preparation(self, owner: str) -> None:
        self._active_preparations.pop(owner, None)

    def select_preparation(self, settings: Settings, *, batch_key=None) -> Preparation:
        # Existing users pin the dependency set. New work never refreshes shared files under them.
        active = list(self._active_preparations.values())
        for preparation in active:
            if not preparation.compatible(settings):
                raise ValueError("运行中的依赖配置不同；请等待任务结束后再切换运行环境")
        if batch_key is not None:
            for preparation in active:
                if preparation.batch_key == batch_key:
                    return preparation
        return Preparation(
            settings,
            observer=self.observe_preparation,
            shared=active[0] if active else None,
            batch_key=batch_key,
        )

    def observe_preparation(self, stage: str, report: dict) -> None:
        report = {**report, "observed_at": report.get("observed_at", time.time())}
        self.preparation_status[stage] = report
        if self.store is not None:
            self.store.event("preparation", {"stage": stage, **report})

    async def prepare_dependencies(self, settings, *, pinned=None):
        async with self._runtime_update_lock:
            subject = "shared-environment" if pinned is None else "batch-public"
            self.observe_preparation("environment", {"status": "running"})
            task = asyncio.create_task(
                asyncio.to_thread(
                    prepare_environment,
                    settings,
                    **({"pinned": pinned} if pinned is not None else {}),
                )
            )
            key = str(id(task))
            self._runtime_checks[key] = task
            try:
                result = await asyncio.shield(task)
                self.remember_activity(result.activity)
            except Exception as error:
                for stage, report in getattr(error, "preparation_observations", {}).items():
                    self.observe_preparation(stage, report)
                stage = getattr(error, "preparation_stage", "environment")
                self.observe_preparation(stage, {"status": "failed", "error": str(error)})
                self.observe_preparation("environment", {"status": "failed", "failed_stage": stage})
                if self.store is not None:
                    self._observe_preparation_error(error, subject=subject)
                raise
            else:
                for stage, report in result.observations.items():
                    self.observe_preparation(stage, report)
                self.observe_preparation("environment", {"status": "succeeded"})
                if self.store is not None:
                    self._resolve_preparation_alerts(subject=subject)
                return result
            finally:
                if task.done():
                    self._runtime_checks.pop(key, None)

    @staticmethod
    def runtime_options(runtime, settings):
        options = settings.runtime_template()
        return replace(
            runtime,
            adb_path=options.adb_path,
            connection_preset=options.connection_preset,
            touch_mode=options.touch_mode,
        )

    async def latest_runtime(self, request: RunRequest):
        assert self.store is not None
        settings = settings_from_bytes(
            self.store.configuration_content(request.config_revision), self.config
        )
        preparation = self._preparations.get(request.attempt_id)
        if preparation is None:
            preparation = self.select_preparation(settings)
        environment = await preparation.get(self.prepare_dependencies)
        return self.runtime_options(environment.runtime, settings)

    def remember_activity(self, snapshot) -> None:
        self.activity = snapshot
        if self.store is not None:
            now = time.time()
            self.store.observe_activities(snapshot, now=now)
            self.store.expire_activity_state(now=now)
            self.wakeup.set()

    async def activity_snapshot(self, settings):
        from maa_planner.daemon.tools.hot_update import prepare_hot_update

        # Queries do not install binaries or change execution resources.
        async with self._runtime_update_lock:
            try:
                hot = await asyncio.to_thread(prepare_hot_update, settings.cache_directory)
            except Exception as error:
                if self.store is not None:
                    self._observe_preparation_error(error, subject="activity-snapshot")
                raise
            else:
                if self.store is not None:
                    self._resolve_preparation_alerts(subject="activity-snapshot")
            self.remember_activity(hot.activity)
            return hot.activity

    def _observe_preparation_error(self, error: Exception, *, subject="shared-environment") -> None:
        from maa_planner.daemon.tools.hot_update import ActivitySourceError

        assert self.store is not None
        category = "activity_source" if isinstance(error, ActivitySourceError) else "preparation"
        self.store.observe_alert(
            "admin",
            category,
            subject,
            {"reason": str(error) or type(error).__name__},
            now=time.time(),
        )

    def _resolve_preparation_alerts(self, *, subject="shared-environment") -> None:
        assert self.store is not None
        for category in ("preparation", "activity_source"):
            self.store.observe_alert("admin", category, subject, None, now=time.time())

    async def _execute(self, request: RunRequest, pending: PendingExecution) -> None:
        async def admit() -> bool:
            if self.stopping.is_set():
                return False
            future = asyncio.get_running_loop().create_future()
            await self._changes.put(("admit", (request, future)))
            self.wakeup.set()
            return await future

        async def phase(value: str) -> None:
            await self._changes.put(("phase", (request.attempt_id, value)))
            self.wakeup.set()

        started = False
        try:
            identifier = self._immediate.get(request.attempt_id)

            def cancelled():
                operation = self.operations.store.operation(identifier) if identifier else None
                return (
                    self.stopping.is_set()
                    or pending.cancel_requested
                    or bool(operation and operation["cancel_requested"])
                )

            if cancelled():
                result = RunResult(request.attempt_id, "cancelled", "执行已取消，未启动")
            else:
                began = time.monotonic()
                runtime = await self.latest_runtime(request)
                # Automatic dependency maintenance does not consume the implicit start window.
                if identifier in self._default_deadlines:
                    self._deadlines[identifier] += time.monotonic() - began
                if cancelled():
                    raise CancelledRun("依赖检查期间取消，未启动")
                if identifier is not None and self._deadlines[identifier] <= time.time():
                    raise ValueError("依赖检查期间请求已过期，未启动")
                assert request.core is not None
                request = replace(request, core=replace(request.core, runtime=runtime))
                assert self.store is not None
                self.store.prepared_request(request)
                worker = self.worker_factory(request, self.root)
                if isinstance(worker, WorkerClient):

                    async def login_report(report):
                        await self._changes.put(("login_success", (request, report)))
                        self.wakeup.set()

                    worker.login_callback = login_report

                    async def clearance_report(report):
                        await self._changes.put(("activity_copilot", (request, report)))
                        self.wakeup.set()

                    worker.clearance_callback = clearance_report

                    async def shop_report(report):
                        await self._changes.put(("activity_shop", (request, report)))
                        self.wakeup.set()

                    worker.shop_callback = shop_report

                    async def shop_permission(query):
                        future = asyncio.get_running_loop().create_future()
                        await self._changes.put(("shop_permission", (request, query, future)))
                        self.wakeup.set()
                        return await future

                    worker.shop_permission_callback = shop_permission

                    async def orundum_start(query):
                        future = asyncio.get_running_loop().create_future()
                        await self._changes.put(("orundum_start", (request, query, future)))
                        self.wakeup.set()
                        return await future

                    async def orundum_applied(report):
                        await self._changes.put(("orundum_applied", (request, report)))
                        self.wakeup.set()

                    worker.orundum_start_callback = orundum_start
                    worker.orundum_callback = orundum_applied
                task = asyncio.current_task()
                assert task is not None
                self.running[request.attempt_id] = (worker, task)
                started = True
                result = await worker.run(admit, phase)
        except CancelledRun as error:
            result = RunResult(request.attempt_id, "cancelled", str(error))
        except Exception as error:
            result = RunResult(
                request.attempt_id, "recovery_required" if started else "failed", str(error)
            )
        await self._changes.put(("done", result))
        self.wakeup.set()

    async def _events(self) -> None:
        assert self.store is not None
        while not self._changes.empty():
            kind, payload = self._changes.get_nowait()
            if kind == "login_success":
                request, report = payload
                self.game_maintenance.observe_login(
                    request.binding.profile.name, report, attempt_id=request.attempt_id
                )
            elif kind == "scheduled_ready":
                self.daily.ready(payload)
            elif kind == "activity_copilot":
                request, report = payload
                self.daily.clearance_report(request, report)
            elif kind == "activity_shop":
                request, report = payload
                self.daily.shop_report(request, report)
            elif kind == "orundum_start":
                request, query, future = payload
                try:
                    allowed = self.daily.orundum.start(request, query)
                except Exception as error:
                    if not future.done():
                        future.set_exception(error)
                else:
                    if not future.done():
                        future.set_result(allowed)
            elif kind == "orundum_applied":
                request, report = payload
                self.daily.orundum.applied(request, report)
            elif kind == "shop_permission":
                request, query, future = payload
                try:
                    status = self.daily.shop_permission(request, query)
                except Exception as error:
                    if not future.done():
                        future.set_exception(error)
                else:
                    if not future.done():
                        future.set_result(status)
            elif kind == "copilot_ready":
                request_id, request, error = payload
                self._copilot_tasks.pop(request_id, None)
                preparation = self._copilot_preparations.pop(request_id, None)
                cancel = self._preparation_cancel.pop(request_id, None)
                if cancel is not None and cancel.is_set():
                    error = "准备已取消，未启动"
                if time.time() >= self._deadlines.get(request_id, float("inf")):
                    error = "请求已过期，未启动"
                if error is None and (self.stopping.is_set()):
                    error = "copilot 准备期间 daemon 停止，未启动"
                if error is not None:
                    self.release_resources("copilot:" + request_id)
                    self.store.fail_copilot(request_id, error)
                    self._deadlines.pop(request_id, None)
                    self._default_deadlines.discard(request_id)
                    if self.store.operation(request_id) is not None:
                        self.operations.refresh(request_id)
                else:
                    self._immediate[request.attempt_id] = request_id
                    self.release_resources("copilot:" + request_id)
                    self._launch(request, preparation=preparation)
            elif kind == "apk_report":
                self.game_updates = payload
                self.game_update_error = None
            elif kind == "apk_error":
                self.game_updates = None
                self.game_update_error = payload
            elif kind == "done":
                assert isinstance(payload, RunResult)
                logins = [
                    item["login_success"]
                    for item in payload.business_results
                    if item.get("status") == "completed" and "login_success" in item
                ]
                request = self.store.prepared_execution(payload.attempt_id) if logins else None
                self.store.finish(payload)
                if request is not None:
                    for receipt in logins:
                        self.game_maintenance.observe_login(
                            request.binding.profile.name, receipt, attempt_id=payload.attempt_id
                        )
                self.daily.finished(payload)
                self.running.pop(payload.attempt_id, None)
                self._preparations.pop(payload.attempt_id, None)
                self.release_resources("attempt:" + payload.attempt_id)
                self.store.event(
                    "execution",
                    {
                        "operation_id": self._immediate.get(payload.attempt_id),
                        "attempt_id": payload.attempt_id,
                        "phase": payload.status,
                    },
                )
                request_id = self._immediate.pop(payload.attempt_id, None)
                if request_id is not None:
                    self._deadlines.pop(request_id, None)
                    self._default_deadlines.discard(request_id)
                    if self.store.operation(request_id) is not None:
                        self.operations.refresh(request_id)
            elif kind == "phase":
                attempt_id, phase = payload
                self.store.phase(attempt_id, phase)
                if phase == "running":
                    self.daily.running(attempt_id)
                operation_id = self._immediate.get(attempt_id)
                if operation_id is not None and self.store.operation(operation_id) is not None:
                    self.operations.refresh(operation_id)
                self.store.event(
                    "execution",
                    {
                        "operation_id": self._immediate.get(attempt_id),
                        "attempt_id": attempt_id,
                        "phase": phase,
                    },
                )
            elif kind == "admit":
                request, future = payload
                allowed = False
                if (
                    not self.stopping.is_set()
                    and (
                        self.configuration is not None
                        or self.daily.control.is_temporary(request.job_id)
                    )
                    and (
                        request.attempt_id in self._immediate
                        or self.daily.control.is_temporary(request.job_id)
                        or (
                            self.config_error is None
                            and self.configuration is not None
                            and request.config_revision == self.configuration.revision
                        )
                    )
                    and time.time()
                    < self._deadlines.get(self._immediate.get(request.attempt_id, ""), float("inf"))
                    and self.daily.admit(request)
                ):
                    try:
                        allowed = (
                            request.attempt_id in self._immediate
                            or self.daily.control.is_temporary(request.job_id)
                            or self.policy.admit(request, self.context())
                        )
                    except Exception:
                        log.exception("准入策略失败，拒绝本次启动")
                if allowed:
                    self.store.phase(request.attempt_id, "admitted")
                if not future.done():
                    future.set_result(allowed)

    async def _reconcile(self) -> float | None:
        assert self.store is not None
        if self._config_dirty:
            self._config_dirty = False
            await self._configuration()
        await self._events()
        self.operations.refresh_batches()
        delay = self.reconcile_interval
        if (maintenance_at := await self.game_maintenance.tick(dispatch=False)) is not None:
            seconds = max(0.05, maintenance_at - time.time())
            delay = seconds if delay is None else min(delay, seconds)
        if (deadline := self.store.expire_activity_state(now=time.time())) is not None:
            seconds = max(0.05, deadline - time.time())
            delay = seconds if delay is None else min(delay, seconds)
        self.daily.dispatch()
        if (wake_at := self.daily.wake_at()) is not None:
            seconds = max(0.05, wake_at - time.time())
            delay = seconds if delay is None else min(delay, seconds)
        if (
            self.draining
            and not self.running
            and not self._copilot_tasks
            and not self.daily.preparing
            and not self.operations.tasks
            and not self.operations.submissions
        ):
            if self.restart_requested and self.config_error is not None:
                self.draining = False
                self.restart_requested = False
                self.operations.store.event("restart_rejected", {"error": self.config_error})
            else:
                self.request_stop()
        if (
            self.configuration is None
            or self.stopping.is_set()
            or self.draining
            or self.maintenance is not None
            or self.config_error is not None
        ):
            return delay
        try:
            decision = self.policy.evaluate(self.context())
        except Exception:
            log.exception("策略评估失败，本轮不准入新任务")
            return delay
        if decision.wake_at is not None:
            seconds = max(0.05, decision.wake_at - time.time())
            delay = seconds if delay is None else min(delay, seconds)
        self.daily.enqueue(decision.occurrences)
        await self.game_maintenance.tick()
        self.daily.dispatch()
        if self.maintenance is not None or self.maintenance_pending:
            return delay
        if (wake_at := self.daily.wake_at()) is not None:
            seconds = max(0.05, wake_at - time.time())
            delay = seconds if delay is None else min(delay, seconds)
        preparation = None
        for request in decision.requests:
            if request.config_revision != self.configuration.revision:
                continue
            if self._unit_busy(
                emulator_resource(request.binding.mumu_root, request.binding.instance_index).key
            ):
                continue
            try:
                if not self.store.enqueue(request):
                    continue
            except ValueError:
                log.exception("策略提交了冲突的 attempt ID")
                continue
            if preparation is None:
                settings = settings_from_bytes(self.configuration.content, self.config)
                preparation = self.select_preparation(settings)
            self._launch(request, enqueued=True, preparation=preparation)
        if (cleanup_at := self.cache_maintenance.tick()) is not None:
            seconds = max(0.05, cleanup_at - time.time())
            delay = seconds if delay is None else min(delay, seconds)
        return delay

    def _launch(self, request: RunRequest, *, enqueued: bool = False, preparation=None) -> None:
        assert self.store is not None
        owner = "attempt:" + request.attempt_id
        unit = emulator_resource(request.binding.mumu_root, request.binding.instance_index).key
        if not self.claim_unit(owner, unit, accepted=True):
            raise APIError("busy", "实例已占用")
        try:
            if preparation is None:
                settings = settings_from_bytes(
                    self.store.configuration_content(request.config_revision), self.config
                )
                preparation = self.select_preparation(settings)
            self.retain_preparation(owner, preparation)
            if not enqueued:
                self.store.enqueue(request)
            self.store.start(request.attempt_id)
            self._preparations[request.attempt_id] = preparation
            pending = PendingExecution(request)
            task = asyncio.create_task(self._execute(request, pending))
            self.running[request.attempt_id] = (pending, task)
        except BaseException:
            self._preparations.pop(request.attempt_id, None)
            self.release_resources(owner)
            raise

    async def _shutdown(self) -> None:
        assert self.store is not None
        self.stopping.set()
        self._apk_cancel.set()
        for event in self._preparation_cancel.values():
            event.set()
        await self.cache_maintenance.close()
        await self.daily.close()
        try:
            for worker, _ in list(self.running.values()):
                try:
                    await asyncio.wait_for(worker.cancel(), 2)
                except Exception:
                    log.exception("发送取消失败；关闭通道后 worker 应自行清理")
            deadline = time.monotonic() + 40
            while self.running and time.monotonic() < deadline:
                self.wakeup.clear()
                await self._events()
                for identifier, (_, task) in list(self.running.items()):
                    if task.done():
                        # A completed task normally queued a result. Drain it first.
                        await self._events()
                        if identifier in self.running:
                            self.store.finish(
                                RunResult(identifier, "recovery_required", "执行协程被中断")
                            )
                            self.running.pop(identifier)
                if self.running:
                    try:
                        await asyncio.wait_for(self.wakeup.wait(), 0.2)
                    except TimeoutError:
                        pass
            if self.running:
                log.error("worker 清理超时，保留恢复记录")
        finally:
            if self._copilot_tasks:
                await asyncio.gather(*list(self._copilot_tasks.values()), return_exceptions=True)
                await self._events()
            await self.operations.close()
            if self._runtime_checks:
                await asyncio.gather(*list(self._runtime_checks.values()), return_exceptions=True)
            await self._events()
            if self._apk_checks:
                await asyncio.gather(*list(self._apk_checks.values()), return_exceptions=True)
            remaining = list(self.running.items())
            for _, (_, task) in remaining:
                task.cancel()
            if remaining:
                await asyncio.gather(*(task for _, (_, task) in remaining), return_exceptions=True)
                for identifier, _ in remaining:
                    self.store.finish(
                        RunResult(identifier, "recovery_required", "关闭期间未确认执行结果")
                    )
                self.running.clear()

    async def run(self) -> None:
        # Single instance is user-wide even if another config/socket/root is supplied.
        with lock_file(state_directory().parent / "daemon.lock"):
            self.store = StateStore(
                self.root / "state.sqlite3", on_event=self.operations.state_event
            )
            watcher = ConfigWatch(self.config, self.configuration_changed)
            try:
                self.store.recover_attempts()
                self.store.recover_copilot()
                self.store.recover_operations()
                self.store.recover_schedules()
                self.daily.orundum.recover()
                watcher.start()
                self._config_dirty = False
                await self._configuration()
                await self.server.start()
                while not self.stopping.is_set():
                    self.wakeup.clear()
                    delay = await self._reconcile()
                    try:
                        await asyncio.wait_for(self.wakeup.wait(), delay)
                    except TimeoutError:
                        pass
            finally:
                self.stopping.set()
                await watcher.close()
                try:
                    await self._shutdown()
                finally:
                    try:
                        await self.server.close()
                    finally:
                        self.store.close()

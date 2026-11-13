"""Daemon adapters for configuration, execution admission and control queries."""

import time
from dataclasses import asdict

from maa_planner.daemon.config import settings_from_bytes
from maa_planner.daemon.operations import json_value
from maa_planner.schema import ConfigurationUpdate, StageConfirmation


class ConfigurationService:
    def __init__(self, *, snapshot, error, path, replace):
        self._snapshot, self._error, self.path, self._replace = snapshot, error, path, replace

    @property
    def snapshot(self):
        return self._snapshot()

    def settings(self, *, allow_stale=False):
        snapshot = self.snapshot
        if snapshot is None or (self._error() is not None and not allow_stale):
            raise ValueError("没有有效的已生效配置")
        return settings_from_bytes(snapshot.content, self.path)

    def get(self):
        snapshot = self.snapshot
        return {
            "revision": snapshot.revision if snapshot else None,
            "toml": snapshot.content.decode("utf-8-sig") if snapshot else None,
            "effective": json_value(asdict(self.settings(allow_stale=True))) if snapshot else None,
            "error": self._error(),
        }

    async def replace(self, params):
        update = ConfigurationUpdate.from_dict(params)
        await self._replace(update.toml, update.expected_revision)
        return self.get()


class ExecutionAccess:
    """Expose admission and submission, never mutable daemon queue containers."""

    def __init__(self, coordinator):
        self._coordinator = coordinator

    @property
    def stopping(self):
        return self._coordinator.stopping.is_set()

    @property
    def draining(self):
        return self._coordinator.draining

    @property
    def maintenance(self):
        return self._coordinator.maintenance

    def available(self):
        self._coordinator.available()

    def reserve(self, identifier, *, accepted=False, accepted_configuration=False, exclusive=False):
        self._coordinator.reserve_resources(
            "operation:" + identifier,
            accepted=accepted,
            accepted_configuration=accepted_configuration,
            exclusive=exclusive,
        )

    def release(self, identifier):
        self._coordinator.release_resources("operation:" + identifier)

    def select_preparation(self, settings):
        return self._coordinator.select_preparation(settings)

    def retain_preparation(self, identifier, preparation):
        self._coordinator.retain_preparation("operation:" + identifier, preparation)

    async def submit_run(self, *args, **kwargs):
        return await self._coordinator.submit_run(*args, **kwargs)

    async def submit_copilot(self, *args, **kwargs):
        return await self._coordinator.submit_copilot(*args, **kwargs)

    async def cancel_execution(self, identifier):
        await self._coordinator.cancel_execution(identifier)


class ControlService:
    def __init__(self, *, store, configuration, status, drain, daily=None):
        self.daily = daily
        self._store, self.configuration = store, configuration
        self.status, self.drain = status, drain

    async def handle(self, method, params):
        store = self._store()
        if method == "daily.get":
            assert self.daily is not None
            return self.daily().control.get(params["profile"], params["occurrence_id"])
        if method == "daily.status":
            assert self.daily is not None
            return self.daily().control.status(params["profile"])
        if method == "daily.next":
            assert self.daily is not None
            return self.daily().control.next(params["profile"])
        if method == "daemon.status":
            return self.status()
        if method in {"daemon.stop", "daemon.restart"}:
            if type(params.get("force", False)) is not bool:
                raise ValueError("force 必须为布尔值")
            if method == "daemon.restart":
                self.configuration.settings()
            self.drain(restart=method == "daemon.restart", force=params.get("force", False))
            return self.status()
        if method == "infrast.orundum.get":
            if not any(p.name == params["profile"] for p in self.configuration.settings().profiles):
                raise ValueError("未知 profile")
            assert self.daily is not None
            return self.daily().orundum.get(params["profile"])
        if method == "config.get":
            return self.configuration.get()
        if method == "config.replace":
            return await self.configuration.replace(params)
        if method == "event.list":
            after, limit = params.get("after", 0), params.get("limit", 100)
            if (
                type(after) is not int
                or after < 0
                or type(limit) is not int
                or not 1 <= limit <= 100
            ):
                raise ValueError("event.list 游标或条数无效")
            return store.events(after, limit)
        if method == "alert.list":
            if type(params.get("active_only", True)) is not bool:
                raise ValueError("active_only 必须为布尔值")
            return store.alerts(params.get("profile"), active_only=params.get("active_only", True))
        if method in {"stage.confirm", "stage.unconfirm", "stage.progress.get"}:
            value = params
            confirmation = StageConfirmation.from_dict(
                {**value, "stage_id": "query"} if method == "stage.progress.get" else value
            )
            if not any(
                p.name == confirmation.profile for p in self.configuration.settings().profiles
            ):
                raise ValueError("未知 profile")
            if method == "stage.confirm":
                return store.confirm_stage(confirmation, now=time.time())
            if method == "stage.unconfirm":
                return store.unconfirm_stage(confirmation, now=time.time())
            return store.stage_progress(confirmation.profile, confirmation.activity_id)
        raise ValueError("未知方法")

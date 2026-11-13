"""Daemon preparation units shared explicitly by batch members."""

import asyncio
import time
from dataclasses import dataclass, field, replace

from maa_planner.daemon.config import Settings
from maa_planner.daemon.constants import (
    RESOURCE_ARCHIVE_PREFIX,
    RESOURCE_ARCHIVE_URL,
    RESOURCE_VERSION_URL,
)
from maa_planner.daemon.execution.runtime import CoreRuntime
from maa_planner.daemon.planning.activity import ActivitySnapshot
from maa_planner.daemon.tools.hot_update import prepare_hot_update
from maa_planner.daemon.tools.resources import check_resource, update_resource
from maa_planner.daemon.tools.runtime_install import ensure_latest_runtime


@dataclass(frozen=True)
class PreparedEnvironment:
    runtime: CoreRuntime
    activity: ActivitySnapshot
    observations: dict = field(default_factory=dict)


def prepare_environment(
    settings: Settings, *, pinned: PreparedEnvironment | None = None
) -> PreparedEnvironment:
    observations = {}
    stage = "core"
    try:
        existing = set(settings.runtime_directory.glob("core-*")) if pinned is None else set()
        runtime = ensure_latest_runtime(settings) if pinned is None else pinned.runtime
        observations[stage] = {
            "status": "succeeded",
            "version": runtime.expected_core_version,
            "installed": pinned is None and runtime.data_dir.parent not in existing,
            "reused": pinned is not None,
            "observed_at": time.time(),
        }
        stage = "resources"
        status = (
            check_resource(runtime.resources, version_url=RESOURCE_VERSION_URL)
            if pinned is None
            else None
        )
        if status is not None and status.available and settings.shared_resource_dir is not None:
            from maa_planner.daemon.adapters.processes import ensure_stopped

            ensure_stopped(settings.profiles)
        if status is not None and status.available:
            update_resource(
                runtime.resources,
                version_url=RESOURCE_VERSION_URL,
                archive_url=RESOURCE_ARCHIVE_URL,
                archive_prefix=RESOURCE_ARCHIVE_PREFIX,
                cache_directory=settings.cache_directory,
                observed=status,
            )
        observations[stage] = {
            **(pinned.observations.get(stage, {}) if pinned else {}),
            "status": "succeeded",
            "updated": False,
            "reused": pinned is not None,
            "observed_at": time.time(),
        }
        if status is not None:
            observations[stage].update(
                {
                    "status": "succeeded",
                    "previous_version": status.current.isoformat(),
                    "version": status.latest.isoformat()
                    if status.available
                    else status.current.isoformat(),
                    "updated": status.available,
                    "observed_at": time.time(),
                }
            )
        stage = "hot_update"
        hot = prepare_hot_update(settings.cache_directory)
        observations[stage] = {
            "status": "succeeded",
            "fingerprint": hot.directory.name,
            "source": hot.activity.source_url,
            "fetched_at": hot.activity.fetched_at.isoformat(),
            "observed_at": time.time(),
        }
        runtime = replace(runtime, navigation=hot.directory)
        stage = "validation"
        runtime.validate()
        return PreparedEnvironment(runtime, hot.activity, observations)
    except Exception as error:
        setattr(error, "preparation_stage", stage)
        setattr(error, "preparation_observations", observations)
        raise


class Preparation:
    """Exactly one preparation result, retained for this unit's lifetime, without a TTL."""

    def __init__(self, settings: Settings, *, observer=None, shared=None, batch_key=None):
        self.settings = settings
        self.observer = observer
        self.shared = shared
        self.batch_key = batch_key
        self.task: asyncio.Task | None = None
        self.catalog_task: asyncio.Task | None = None
        self.copilot_sets_task: asyncio.Task | None = None
        self.annihilation_task: asyncio.Task | None = None
        self.navigation_tasks: dict[str, asyncio.Task] = {}

    def compatible(self, settings: Settings) -> bool:
        return (
            self.settings.runtime_platform,
            self.settings.runtime_directory,
            self.settings.shared_resource_dir,
        ) == (settings.runtime_platform, settings.runtime_directory, settings.shared_resource_dir)

    async def get(self, loader) -> PreparedEnvironment:
        if self.task is None:

            async def prepare():
                if self.shared is None:
                    return await loader(self.settings)
                environment = await self.shared.get(loader)
                self.shared = None
                return await loader(self.settings, pinned=environment)

            self.task = asyncio.create_task(prepare())
        return await asyncio.shield(self.task)

    async def stage_catalog(self) -> dict:
        from maa_planner.daemon.tools.stage_catalog import load_stage_catalog

        if self.catalog_task is None:
            self.catalog_task = asyncio.create_task(
                asyncio.to_thread(load_stage_catalog, self.settings.cache_directory)
            )
        try:
            result = await asyncio.shield(self.catalog_task)
        except Exception as error:
            if self.observer:
                self.observer("catalog", {"status": "failed", "error": str(error)})
            raise
        if self.observer:
            self.observer(
                "catalog",
                {
                    "status": "succeeded",
                    "commit": result["commit"],
                    "source": result.get("source"),
                },
            )
        return result

    async def activity_navigation(self, catalog, activity_id):
        import threading

        from maa_planner.daemon.tools.activity_sprite_navigation import prepare_navigation_assets

        if activity_id not in self.navigation_tasks:
            self.navigation_tasks[activity_id] = asyncio.create_task(
                asyncio.to_thread(
                    prepare_navigation_assets,
                    self.settings,
                    catalog,
                    activity_id,
                    threading.Event(),
                )
            )
        return await asyncio.shield(self.navigation_tasks[activity_id])

    async def copilot_sets(self) -> tuple[dict, ...]:
        import threading

        from maa_planner.daemon.planning.auto_copilot import query_sets

        if self.copilot_sets_task is None:
            self.copilot_sets_task = asyncio.create_task(
                asyncio.to_thread(query_sets, self.settings, threading.Event())
            )
        return await asyncio.shield(self.copilot_sets_task)

    async def annihilation_candidates(self, cancel, *, now):
        if cancel.is_set():
            raise InterruptedError("剿灭准备取消")
        from maa_planner.daemon.planning.annihilation_rescue import current_candidates
        from maa_planner.daemon.tools.stage_catalog import load_campaign_table

        if self.annihilation_task is None:

            async def load():
                catalog = await self.stage_catalog()
                campaign = await asyncio.to_thread(
                    load_campaign_table, self.settings.cache_directory, catalog["commit"]
                )
                candidates = await asyncio.to_thread(current_candidates, catalog, campaign, now=now)
                return catalog["commit"], candidates

            self.annihilation_task = asyncio.create_task(load())
        result = await asyncio.shield(self.annihilation_task)
        if cancel.is_set():
            raise InterruptedError("剿灭准备取消")
        return result

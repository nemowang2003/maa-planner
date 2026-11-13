"""Observation and planning use cases, independently callable from API transport."""

import asyncio
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

from maa_planner.daemon.config import Settings
from maa_planner.daemon.constants import (
    RESOURCE_ARCHIVE_PREFIX,
    RESOURCE_ARCHIVE_URL,
    RESOURCE_VERSION_URL,
)


class ObservationService:
    def __init__(
        self, *, store, activity_snapshot, check_game_updates, observe_preparation, recovery_root
    ):
        self._store = store
        self.activity_snapshot = activity_snapshot
        self.check_game_updates = check_game_updates
        self.observe_preparation = observe_preparation
        self.recovery_root = recovery_root

    @property
    def store(self):
        return self._store()

    async def execute(
        self,
        action: str,
        settings: Settings,
        cancel: threading.Event,
        arguments: dict | None = None,
    ):
        if action == "game-check":
            return asdict(await self.check_game_updates(settings))
        if action == "assets":
            from maa_planner.daemon.tools.activity_assets import prepare_assets

            return await asyncio.to_thread(prepare_assets, settings, arguments or {}, cancel)
        if action == "news":
            from maa_planner.daemon.tools.news import collect_news

            return await asyncio.to_thread(
                collect_news, settings.cache_directory, arguments or {}, cancel
            )
        if action == "activities":
            from maa_planner.daemon.planning.skland import summarize_activities

            activity = await self.activity_snapshot(settings)
            return summarize_activities(activity) | await self.region_windows(settings, activity)
        if action == "plan":
            from maa_planner.daemon.daily_preparation import (
                collect_daily_evidence,
                has_ordinary_fight,
                prepare_daily_inputs,
            )
            from maa_planner.daemon.planning.auto_copilot import query_sets
            from maa_planner.daemon.planning.daily import build_daily_plan
            from maa_planner.daemon.preparation import Preparation
            from maa_planner.daemon.tools.stage_catalog import load_stage_catalog

            profile = settings.profiles[0]
            schedule = next(
                (s for s in settings.schedules if s.id == (arguments or {}).get("schedule_id")),
                None,
            )
            if schedule is None or profile.name not in schedule.profiles:
                raise ValueError("日程不存在或不包含目标账号")
            now = time.time()
            evidence = await collect_daily_evidence(settings, profile, schedule, now=now)
            if evidence.skland_error is not None and evidence.requires_skland:
                raise evidence.skland_error
            ordinary_fight_today = has_ordinary_fight(schedule, now=now)
            has_shop_state = self.store.has_shop_state(profile.name)
            activity = None
            if (
                any(t.task_type == "Fight" for t in evidence.experts)
                or ordinary_fight_today
                or has_shop_state
            ):
                activity = await self.activity_snapshot(settings)
            catalog = None
            public = Preparation(settings, observer=self.observe_preparation)

            async def catalog_loader():
                nonlocal catalog
                if catalog is None:
                    catalog = await asyncio.to_thread(load_stage_catalog, settings.cache_directory)
                return catalog

            async def sets_loader():
                return await asyncio.to_thread(query_sets, settings, cancel)

            async def navigation_loader(catalog, activity_id):
                from maa_planner.daemon.tools.activity_sprite_navigation import (
                    prepare_navigation_assets,
                )

                return await asyncio.to_thread(
                    prepare_navigation_assets, settings, catalog, activity_id, threading.Event()
                )

            async def rescue_candidates():
                if public.catalog_task is None:
                    public.catalog_task = asyncio.create_task(catalog_loader())
                return await public.annihilation_candidates(cancel, now=now)

            inputs = await prepare_daily_inputs(
                settings,
                profile,
                schedule,
                evidence,
                now=now,
                activity=activity,
                store=self.store,
                load_catalog=catalog_loader,
                load_sets=sets_loader,
                load_navigation=navigation_loader,
                load_annihilation_candidates=rescue_candidates,
                cancel=cancel,
                prepare_shops=ordinary_fight_today or has_shop_state,
            )
            row = self.store.db.execute(
                "SELECT payload FROM orundum_intents WHERE profile=?", (profile.name,)
            ).fetchone()
            intent = json.loads(row[0]) if row else None
            from maa_planner.daemon.orundum import check_observation

            check_observation(
                self.store,
                profile.name,
                evidence,
                manual_entry=intent["intent_id"]
                if intent and intent["status"] == "pending"
                else None,
                clear=False,
            )
            plan = await asyncio.to_thread(
                build_daily_plan,
                schedule,
                **inputs.plan_arguments(),
                manual_entry=intent["intent_id"]
                if intent and intent["status"] == "pending"
                else None,
            )
            activity = inputs.activity
            configured_processing = inputs.configured_processing
            processing = inputs.processing
            rescue_error = (
                str(error) if (error := inputs.issues.get("annihilation_prepare")) else None
            )
            clearance_error = (
                str(error) if (error := inputs.issues.get("activity_copilot_prepare")) else None
            )
            shop_error = (
                str(error) if (error := inputs.issues.get("activity_shop_prepare")) else None
            )
            return {
                "schedule_id": schedule.id,
                "orundum_intent": intent,
                "profile": profile.name,
                "planned_at": now,
                "enabled": schedule.enabled,
                "tasks": json.loads(plan.tasks.document)["tasks"] if plan.tasks else [],
                "resource_tasks": json.loads(plan.tasks.document).get("resource_tasks", {})
                if plan.tasks
                else {},
                "attachments": json.loads(plan.tasks.attachments) if plan.tasks else {},
                "resource_images": json.loads(plan.tasks.document).get("resource_images", {})
                if plan.tasks
                else {},
                "decisions": [asdict(d) for d in plan.decisions],
                "annihilation": asdict(plan.annihilation) if plan.annihilation else None,
                "farming": [asdict(d) for d in plan.farming],
                "annihilation_copilot": plan.annihilation_rescue,
                "annihilation_copilot_error": rescue_error,
                "infrast_processing": json.loads(plan.tasks.document).get("infrast_processing", [])
                if plan.tasks
                else [],
                "infrast_processing_error": processing.get("error") if processing else None,
                "infrast_processing_warnings": [
                    {"reason": "unsupported_operator", "operator": "九色鹿"}
                ]
                if any("九色鹿" in t.processing_operators for t in configured_processing)
                else [],
                "activity_copilot": plan.clearance,
                "activity_copilot_error": clearance_error,
                "activity_shops": list(plan.shops),
                "activity_shop_error": shop_error,
                **(
                    await self.region_windows(settings, activity, catalog=catalog)
                    if activity
                    else {}
                ),
            }
        if action == "player-context":
            from maa_planner.daemon.planning.skland import collect_player_context, summarize_context

            context = await collect_player_context(
                settings, settings.profiles[0], await self.activity_snapshot(settings)
            )
            return summarize_context(context)
        return await asyncio.to_thread(execute_sync, action, settings, cancel, self.recovery_root)

    async def region_windows(self, settings, activity, *, catalog=None):
        from maa_planner.daemon.planning.regions import activity_regions
        from maa_planner.daemon.tools.stage_catalog import load_stage_catalog

        if not activity.data.side_stories:
            return {"activity_regions": [], "activity_regions_error": None}
        try:
            catalog = catalog or await asyncio.to_thread(
                load_stage_catalog, settings.cache_directory
            )
            self.observe_preparation(
                "catalog",
                {
                    "status": "succeeded",
                    "commit": catalog["commit"],
                    "source": catalog.get("source"),
                },
            )
            regions = activity_regions(activity, catalog, now=time.time())
        except Exception as error:
            self.observe_preparation("catalog", {"status": "failed", "error": str(error)})
            return {"activity_regions": None, "activity_regions_error": str(error)}
        return {"activity_regions": regions, "activity_regions_error": None}


def execute_sync(action: str, settings: Settings, cancel: threading.Event, recovery_root: Path):
    from maa_planner.daemon.adapters.mumu import MuMu, match_profiles
    from maa_planner.daemon.adapters.processes import ensure_stopped
    from maa_planner.daemon.tools.locks import acquire, data_resource

    runtime = settings.runtime_template()
    if action == "emulators":
        if settings.mumu_path is None:
            raise ValueError("请配置 [mumu].path")
        return [
            {"profile": p.name, **asdict(i)}
            for p, i in match_profiles(
                settings.profiles, MuMu(settings.mumu_path).instances()
            ).items()
        ]
    if action == "game-update":
        from maa_planner.daemon.tools.game_update import update_game

        return asdict(update_game(settings))
    if action == "runtime-prepare":
        from maa_planner.daemon.preparation import prepare_environment

        return prepare_environment(settings).runtime.to_dict()
    if action == "runtime-check":
        from maa_planner.daemon.execution.runtime import probe_runtime
        from maa_planner.daemon.tools.runtime_install import ensure_latest_runtime

        runtime = ensure_latest_runtime(settings)
        return {"versions": probe_runtime(runtime)}
    if action in {"resource-check", "resource-update"}:
        from maa_planner.daemon.tools.resources import check_resource, update_resource

        if settings.shared_resource_dir is None:
            from maa_planner.daemon.tools.runtime_install import ensure_latest_runtime

            runtime = ensure_latest_runtime(settings)
        resource = settings.shared_resource_dir or runtime.resources
        ensure_stopped(settings.profiles)
        if action == "resource-update":
            return asdict(
                update_resource(
                    resource,
                    version_url=RESOURCE_VERSION_URL,
                    archive_url=RESOURCE_ARCHIVE_URL,
                    archive_prefix=RESOURCE_ARCHIVE_PREFIX,
                    cache_directory=settings.cache_directory,
                )
            )
        with acquire([data_resource(resource, shared=True)]):
            return asdict(check_resource(resource, version_url=RESOURCE_VERSION_URL))
    if action == "recover":
        from maa_planner.daemon.execution.context import UnitBinding
        from maa_planner.daemon.execution.runner import restore_core

        if settings.mumu_path is None:
            raise ValueError("请配置 [mumu].path")
        profile = settings.profiles[0]
        instance = match_profiles((profile,), MuMu(settings.mumu_path).instances())[profile]
        restore_core(
            UnitBinding(profile, settings.mumu_path, instance.index, instance.name),
            recovery_root=recovery_root,
        )
        return {"profile": profile.name, "recovered": True}
    raise ValueError("未实现的 action")

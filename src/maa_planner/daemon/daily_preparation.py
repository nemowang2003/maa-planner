"""Account evidence and optional inputs shared by preview and daily execution.

No scheduling state or alerts are written here. Callers own fatal-error policy,
activity loading and the lifetime of the public dependency loaders.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ActivitySnapshot
from maa_planner.daemon.planning.annihilation import (
    WeeklyProgress,
    decide_annihilation,
    is_annihilation_stage,
    observe_weekly_progress,
)
from maa_planner.daemon.planning.daily import InfrastObservation, observe_depot, observe_infrast
from maa_planner.daemon.planning.processing import needs_processing
from maa_planner.daemon.planning.schedule import DailyTask

if TYPE_CHECKING:
    from skland_api import CharacterInfo


@dataclass
class DailyEvidence:
    experts: tuple[DailyTask, ...]
    info: CharacterInfo | None = None
    observation: InfrastObservation | None = None
    depot: dict[str, int] | None = None
    weekly_progress: WeeklyProgress | None = None
    skland_error: Exception | None = None
    weekly_error: Exception | None = None

    @property
    def requires_skland(self):
        return any(t.task_type == "Infrast" or t.farming_expert for t in self.experts)


async def collect_daily_evidence(settings, profile, schedule, *, now, collect_info=None):
    if collect_info is None:
        from maa_planner.daemon.planning.skland import collect_character_info

        collect_info = collect_character_info
    weekday = datetime.fromtimestamp(now, BUSINESS_TIMEZONE).isoweekday()
    evidence = DailyEvidence(
        tuple(
            t
            for t in schedule.tasks
            if t.enabled and weekday in t.weekdays and t.strategy == "expert"
        )
    )
    if not evidence.experts:
        return evidence
    try:
        evidence.info, _ = await collect_info(settings, profile)
        observed_now = time.time()
        if any(t.task_type == "Infrast" for t in evidence.experts):
            evidence.observation = observe_infrast(
                evidence.info.cultivate, evidence.info.player_info, now=observed_now
            )
        if any(t.farming_expert for t in evidence.experts):
            evidence.depot = observe_depot(evidence.info.cultivate)
    except Exception as error:
        evidence.skland_error = error
    if evidence.info is not None and any(t.annihilation_expert for t in evidence.experts):
        try:
            evidence.weekly_progress = observe_weekly_progress(
                evidence.info.player_info, now=time.time()
            )
        except Exception as error:
            evidence.weekly_error = error
    return evidence


def has_ordinary_fight(schedule, *, now):
    weekday = datetime.fromtimestamp(now, BUSINESS_TIMEZONE).isoweekday()
    return any(
        t.enabled
        and weekday in t.weekdays
        and t.task_type == "Fight"
        and not t.annihilation_expert
        and not is_annihilation_stage(json.loads(t.params_json).get("stage"))
        for t in schedule.tasks
    )


@dataclass
class DailyInputs:
    evidence: DailyEvidence
    activity: ActivitySnapshot | None
    now: float
    configured_processing: tuple[DailyTask, ...]
    pending: frozenset[str] | None = None
    clearance: tuple[dict, dict, dict] | None = None
    shops: tuple[dict, ...] = ()
    annihilation_rescue: tuple[dict, dict, dict] | None = None
    processing: dict | None = None
    # A present key with None means that preparation completed successfully.
    issues: dict[str, Exception | None] = field(default_factory=dict)

    def plan_arguments(self):
        return {
            "now": self.now,
            "observation": self.evidence.observation,
            "activity": self.activity,
            "weekly_progress": self.evidence.weekly_progress,
            "depot": self.evidence.depot,
            "clearance": self.clearance,
            "shops": self.shops,
            "skip_farming": self.issues.get("activity_copilot_prepare") is not None,
            "annihilation_rescue": self.annihilation_rescue,
            "processing": self.processing,
            "pending": self.pending,
        }


async def prepare_daily_inputs(
    settings,
    profile,
    schedule,
    evidence,
    *,
    now,
    activity,
    store,
    load_catalog,
    load_sets,
    load_navigation,
    load_annihilation_candidates,
    cancel,
    pending=None,
    prepare_shops=True,
):
    from maa_planner.daemon.planning.activity_history import with_activity_history
    from maa_planner.daemon.planning.activity_shop import prepare_shop_context
    from maa_planner.daemon.planning.annihilation_rescue import prepare_rescue
    from maa_planner.daemon.planning.auto_copilot import prepare_automatic
    from maa_planner.daemon.planning.processing import prepare_processing

    # Weekly progress and original-event metadata are independent evidence.
    # Missing progress must not discard a valid three-week rerun exception.
    if (
        activity is not None
        and evidence.info is not None
        and any(t.annihilation_expert for t in evidence.experts)
    ):
        activity = with_activity_history(activity, evidence.info.player_info)
    weekday = datetime.fromtimestamp(now, BUSINESS_TIMEZONE).isoweekday()
    configured_processing = tuple(
        t
        for i, t in enumerate(schedule.tasks)
        if needs_processing(t, weekday) and (pending is None or f"task:{i}" in pending)
    )
    result = DailyInputs(evidence, activity, now, configured_processing, pending)
    ordinary_fight = has_ordinary_fight(schedule, now=now)

    async def attempt(stage, prepare):
        try:
            value = await prepare()
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception as error:
            result.issues[stage] = error
            return None
        result.issues[stage] = None
        return value

    if (
        ordinary_fight
        and activity is not None
        and activity.data.active_side_stories(datetime.fromtimestamp(now, BUSINESS_TIMEZONE))
    ):
        result.clearance = await attempt(
            "activity_copilot_prepare",
            lambda: prepare_automatic(
                settings,
                profile,
                evidence.info,
                activity,
                store,
                load_catalog,
                load_sets,
                cancel,
                now=now,
            ),
        )
    if activity is not None and prepare_shops:
        shops = await attempt(
            "activity_shop_prepare",
            lambda: prepare_shop_context(
                schedule,
                activity,
                now=now,
                store=store,
                profile=profile.name,
                load_catalog=load_catalog,
                load_navigation=load_navigation,
                roi_cache_directory=settings.cache_directory,
                depot=evidence.depot
                if evidence.depot is not None
                else evidence.observation.depot
                if evidence.observation is not None
                else None,
            ),
        )
        result.shops = shops or ()
    if (
        activity is not None
        and any(t.annihilation_expert for t in evidence.experts)
        and decide_annihilation(now=now, activity=activity, progress=evidence.weekly_progress).run
    ):
        result.annihilation_rescue = await attempt(
            "annihilation_prepare",
            lambda: prepare_rescue(
                settings, profile, evidence.info, load_annihilation_candidates, cancel, now=now
            ),
        )
    if any(any(o != "九色鹿" for o in t.processing_operators) for t in configured_processing):
        result.processing = await attempt(
            "infrast_processing_prepare",
            lambda: prepare_processing(settings, profile, evidence.info, load_catalog),
        )
        error = result.issues["infrast_processing_prepare"]
        if error is not None:
            result.processing = {"error": str(error)}
    return result

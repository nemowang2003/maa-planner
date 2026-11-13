"""Weekly annihilation decisions; no network, clocks, or game side effects."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ActivitySnapshot
from maa_planner.daemon.planning.activity_history import four_am_end


def game_day(now: float) -> float:
    local = datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
    date = (local - timedelta(hours=4)).date()
    return datetime(date.year, date.month, date.day, 4, tzinfo=BUSINESS_TIMEZONE).timestamp()


def week_bounds(now: float) -> tuple[float, float]:
    day = datetime.fromtimestamp(game_day(now), BUSINESS_TIMEZONE)
    start = day - timedelta(days=day.weekday())
    return start.timestamp(), (start + timedelta(days=7)).timestamp()


def is_annihilation_stage(stage: object) -> bool:
    return isinstance(stage, str) and (stage == "Annihilation" or stage.endswith("@Annihilation"))


@dataclass(frozen=True, slots=True)
class WeeklyProgress:
    current: int
    total: int
    observed_at: float

    @property
    def complete(self) -> bool:
        return self.current == self.total


def observe_weekly_progress(player: dict, *, now: float) -> WeeklyProgress | None:
    """A previous-cycle snapshot is unknown for this cycle, not an aged-out depot."""
    if not isinstance(player, dict):
        return None
    status, campaign = player.get("status"), player.get("campaign")
    if (
        not isinstance(status, dict)
        or not isinstance(campaign, dict)
        or not isinstance(campaign.get("reward"), dict)
    ):
        return None
    timestamp = status.get("storeTs")
    reward = campaign["reward"]
    current, total = reward.get("current"), reward.get("total")
    start, _ = week_bounds(now)
    if (
        type(timestamp) not in (int, float)
        or not math.isfinite(timestamp)
        or not start <= timestamp <= now
        or type(current) is not int
        or type(total) is not int
        or not 0 <= current <= total
        or total <= 0
    ):
        return None
    return WeeklyProgress(current, total, float(timestamp))


@dataclass(frozen=True, slots=True)
class AnnihilationDecision:
    run: bool
    week_start: float
    week_end: float
    reason: str
    deferred_until: float | None = None
    completed: bool = False
    reopen_original_ids: tuple[str, ...] = ()
    history_unknown: bool = False


def decide_annihilation(
    *,
    now: float,
    activity: ActivitySnapshot,
    progress: WeeklyProgress | None = None,
) -> AnnihilationDecision:
    start, end = week_bounds(now)
    if progress is not None and start <= progress.observed_at <= now and progress.complete:
        return AnnihilationDecision(False, start, end, "本周剿灭额度已完成", completed=True)
    current = datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
    reopens = [
        s
        for s in activity.data.side_stories
        if s.key.casefold() == "ssreopen"
        and s.starts_at <= current < (four_am_end(s.ends_at) or s.ends_at)
        and (four_am_end(s.ends_at) or s.ends_at).timestamp() < end
    ]
    qualified = [s for s in reopens if s.original is not None and s.original.game_days == 21]
    if qualified:
        return AnnihilationDecision(
            False,
            start,
            end,
            "原活动为三周的复刻将在本周结束，等待活动结束后再剿灭",
            deferred_until=max(
                (four_am_end(s.ends_at) or s.ends_at).timestamp() for s in qualified
            ),
            reopen_original_ids=tuple(
                s.original.activity_id for s in qualified if s.original is not None
            ),
        )
    unknown = any(s.original is None for s in reopens)
    reason = "周重置后尽早完成剿灭" if progress else "本周进度未知，进行有次数上限的剿灭尝试"
    if unknown:
        reason += "；缺少原活动历史，无法确认三周复刻例外，先保障周额度"
    return AnnihilationDecision(
        True,
        start,
        end,
        reason,
        history_unknown=unknown,
    )

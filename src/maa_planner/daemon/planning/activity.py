"""Immutable public MAA activity data; business timestamps use Asia/Shanghai."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum

from maa_planner.daemon.constants import BUSINESS_TIMEZONE


def shanghai_now() -> datetime:
    return datetime.now(BUSINESS_TIMEZONE)


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Expected a timezone-aware datetime")
    return value.astimezone(BUSINESS_TIMEZONE)


class ActivityStatus(StrEnum):
    UPCOMING = "upcoming"
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityWindow:
    starts_at: datetime
    ends_at: datetime
    timezone_offset: int

    def status(self, now: datetime | None = None) -> ActivityStatus:
        """Use a half-open interval: starts_at <= now < ends_at."""
        current = require_aware(now if now is not None else shanghai_now())
        if current < self.starts_at:
            return ActivityStatus.UPCOMING
        if current >= self.ends_at:
            return ActivityStatus.ENDED
        return ActivityStatus.ACTIVE


@dataclass(frozen=True, slots=True, kw_only=True)
class RecommendedStage:
    display: str
    value: str
    drop: str


EFFICIENCY_PREFIX = "搓玉效率"


def efficiency_value(drop: str) -> Decimal | None:
    """Parse a non-negative efficiency recommendation, preserving raw Drop text."""
    if not drop.startswith(EFFICIENCY_PREFIX):
        return None
    match = re.fullmatch(re.escape(EFFICIENCY_PREFIX) + r"\s*([0-9]+(?:\.[0-9]+)?)", drop)
    if match is None or Decimal(match[1]) <= 0:
        return None
    return Decimal(match[1])


def validate_active_recommendations(data: ActivityData, now: datetime) -> None:
    """Reject unusable recommendations while the public snapshot is still fresh."""
    current = require_aware(now)
    for story in data.active_side_stories(current):
        if any(not stage.value or not stage.drop for stage in story.stages):
            raise ActivityParseError(f"{story.key}: empty stage recommendation")
        materials = [
            stage for stage in story.stages if stage.drop.isascii() and stage.drop.isdecimal()
        ]
        if not materials:
            raise ActivityParseError(f"{story.key}: 缺少普通材料推荐")
        if not any(stage.drop.startswith(EFFICIENCY_PREFIX) for stage in story.stages):
            raise ActivityParseError(f"{story.key}: 缺少搓玉效率推荐")
        for stage in story.stages:
            if stage.drop.startswith(EFFICIENCY_PREFIX) and efficiency_value(stage.drop) is None:
                raise ActivityParseError(f"{story.key}: 搓玉效率推荐无效")


@dataclass(frozen=True, slots=True, kw_only=True)
class OriginalSideStory:
    activity_id: str
    replica_id: str
    name: str
    starts_at: datetime
    ends_at: datetime
    game_days: int
    source: str = "skland.activityInfoMap+activity"


@dataclass(frozen=True, slots=True, kw_only=True)
class SideStory(ActivityWindow):
    key: str
    name: str
    tip: str
    stages: tuple[RecommendedStage, ...]
    minimum_required: str | None
    original: OriginalSideStory | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceCollection(ActivityWindow):
    tip: str
    is_resource_collection: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class MiniGame(ActivityWindow):
    display: str
    value: str
    tip: str | None
    minimum_required: str | None
    display_key: str | None
    tip_key: str | None


type Activity = SideStory | ResourceCollection | MiniGame


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityData:
    side_stories: tuple[SideStory, ...]
    resource_collection: ResourceCollection | None
    mini_games: tuple[MiniGame, ...]

    def active_side_stories(self, now: datetime | None = None) -> tuple[SideStory, ...]:
        """Combat windows only; shop access and garbage time do not extend them."""
        current = require_aware(now if now is not None else shanghai_now())
        return tuple(s for s in self.side_stories if s.status(current) == ActivityStatus.ACTIVE)

    def by_status(
        self, status: ActivityStatus, now: datetime | None = None
    ) -> tuple[Activity, ...]:
        """Query all three activity kinds without discarding their concrete types."""
        current = require_aware(now if now is not None else shanghai_now())
        activities: tuple[Activity, ...] = self.side_stories
        if self.resource_collection is not None:
            activities += (self.resource_collection,)
        activities += self.mini_games
        return tuple(activity for activity in activities if activity.status(current) == status)

    def active(self, now: datetime | None = None) -> tuple[Activity, ...]:
        return self.by_status(ActivityStatus.ACTIVE, now)

    def upcoming(self, now: datetime | None = None) -> tuple[Activity, ...]:
        return self.by_status(ActivityStatus.UPCOMING, now)

    def ended(self, now: datetime | None = None) -> tuple[Activity, ...]:
        return self.by_status(ActivityStatus.ENDED, now)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivitySnapshot:
    data: ActivityData
    source_url: str
    fetched_at: datetime
    expires_at: datetime
    checked_at: datetime
    stale: bool = False
    refresh_error: str | None = None

    def is_stale(self, now: datetime | None = None) -> bool:
        """Recheck freshness after holding a snapshot beyond its original lookup."""
        current = require_aware(now if now is not None else shanghai_now())
        return current >= self.expires_at


class ActivityParseError(ValueError):
    """MAA returned an invalid or unsupported activity document."""


def _object(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        raise ActivityParseError(f"{path}: expected an object")
    return value


def _array(value: object, path: str) -> list:
    if not isinstance(value, list):
        raise ActivityParseError(f"{path}: expected an array")
    return value


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ActivityParseError(f"{key}: expected a string")
    return value


def _optional_text(data: dict, key: str) -> str | None:
    return None if key not in data else _text(data, key)


def _window(data: dict) -> tuple[datetime, datetime, int]:
    offset = data.get("TimeZone")
    if type(offset) is not int or not -23 <= offset <= 23:
        raise ActivityParseError("TimeZone: expected an integer UTC offset between -23 and 23")
    try:
        # Despite their names, MAA interprets these strings in TimeZone, then
        # subtracts that offset. See MaaWpfGui/Services/StageManager.cs ParseDateTime.
        zone = timezone(timedelta(hours=offset))
        start = datetime.strptime(_text(data, "UtcStartTime"), "%Y/%m/%d %H:%M:%S")
        end = datetime.strptime(_text(data, "UtcExpireTime"), "%Y/%m/%d %H:%M:%S")
        starts_at = start.replace(tzinfo=zone).astimezone(BUSINESS_TIMEZONE)
        ends_at = end.replace(tzinfo=zone).astimezone(BUSINESS_TIMEZONE)
    except ValueError as error:
        raise ActivityParseError(f"Invalid activity time: {error}") from error
    if starts_at >= ends_at:
        raise ActivityParseError("Activity start must precede its end")
    return starts_at, ends_at, offset


def parse_activity_data(payload: object) -> ActivityData:
    """Parse Official only, keeping source order, keys, and unmodified Drop text.

    Required sections must exist, even when empty; malformed data is never
    silently converted to an empty schedule. Unknown additional fields are ignored.
    resourceCollection may be null when no such window is advertised.
    """
    root = _object(payload, "root")
    official = _object(root.get("Official"), "Official")
    stories = _object(official.get("sideStoryStage"), "Official.sideStoryStage")
    side_stories = []
    for key, raw in stories.items():
        if not isinstance(key, str):
            raise ActivityParseError("sideStoryStage: expected string activity keys")
        entry = _object(raw, f"sideStoryStage.{key}")
        activity = _object(entry.get("Activity"), f"{key}.Activity")
        starts_at, ends_at, offset = _window(activity)
        stages = []
        for raw_stage in _array(entry.get("Stages"), f"{key}.Stages"):
            stage = _object(raw_stage, f"{key}.Stages[]")
            stages.append(
                RecommendedStage(
                    display=_text(stage, "Display"),
                    value=_text(stage, "Value"),
                    drop=_text(stage, "Drop"),
                )
            )
        side_stories.append(
            SideStory(
                key=key,
                name=_text(activity, "StageName"),
                tip=_text(activity, "Tip"),
                starts_at=starts_at,
                ends_at=ends_at,
                timezone_offset=offset,
                stages=tuple(stages),
                minimum_required=_optional_text(entry, "MinimumRequired"),
            )
        )

    if "resourceCollection" not in official:
        raise ActivityParseError("Official.resourceCollection: missing section")
    resource_collection = None
    if official["resourceCollection"] is not None:
        resource = _object(official["resourceCollection"], "resourceCollection")
        starts_at, ends_at, offset = _window(resource)
        flag = resource.get("IsResourceCollection")
        if not isinstance(flag, bool):
            raise ActivityParseError("IsResourceCollection: expected a boolean")
        resource_collection = ResourceCollection(
            tip=_text(resource, "Tip"),
            starts_at=starts_at,
            ends_at=ends_at,
            timezone_offset=offset,
            is_resource_collection=flag,
        )

    mini_games = []
    for raw in _array(official.get("miniGame"), "Official.miniGame"):
        game = _object(raw, "miniGame[]")
        starts_at, ends_at, offset = _window(game)
        mini_games.append(
            MiniGame(
                display=_text(game, "Display"),
                value=_text(game, "Value"),
                tip=_optional_text(game, "Tip"),
                starts_at=starts_at,
                ends_at=ends_at,
                timezone_offset=offset,
                minimum_required=_optional_text(game, "MinimumRequired"),
                display_key=_optional_text(game, "DisplayKey"),
                tip_key=_optional_text(game, "TipKey"),
            )
        )
    return ActivityData(
        side_stories=tuple(side_stories),
        resource_collection=resource_collection,
        mini_games=tuple(mini_games),
    )

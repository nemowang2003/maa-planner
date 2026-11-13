"""Join MAA's current rerun to Skland's original activity metadata.

No second current-activity request, name-prefix heuristic, or guessed ID suffix.
The shared public snapshot is never mutated by account-specific enrichment.
"""

import math
from dataclasses import replace
from datetime import datetime, time, timedelta

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ActivitySnapshot, OriginalSideStory


def four_am_end(end: datetime) -> datetime | None:
    if end.timetz().replace(tzinfo=None) == time(3, 59, 59):
        return end + timedelta(seconds=1)
    if end.timetz().replace(tzinfo=None) == time(4):
        return end
    return None


def original_story(player: dict, name: str) -> OriginalSideStory | None:
    if not isinstance(player, dict):
        return None
    info, records = player.get("activityInfoMap"), player.get("activity")
    if not isinstance(info, dict) or not isinstance(records, list):
        return None
    candidates = [
        (key, value)
        for key, value in info.items()
        if isinstance(value, dict)
        and value.get("name") == name
        and value.get("type") == "SIDESTORY"
        and value.get("isReplicate") is False
    ]
    if len(candidates) != 1:
        return None
    identifier, entry = candidates[0]
    if not isinstance(identifier, str) or entry.get("id") != identifier:
        return None
    replicas = {
        record.get("actReplicaId")
        for record in records
        if isinstance(record, dict)
        and record.get("actId") == identifier
        and isinstance(record.get("actReplicaId"), str)
        and record["actReplicaId"]
        and record["actReplicaId"] != identifier
    }
    if len(replicas) != 1:
        return None
    values = [entry.get("startTime"), entry.get("endTime")]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
        return None
    try:
        start, end = (datetime.fromtimestamp(v, BUSINESS_TIMEZONE) for v in values)
    except (ValueError, OverflowError, OSError):
        return None
    if end <= start:
        return None
    # Skland uses an inclusive 03:59:59 expiry. Count playable game dates,
    # not elapsed 24h units: a noon launch otherwise makes 3 weeks only 20⅔ days.
    exclusive_end = four_am_end(end)
    if exclusive_end is None:
        return None
    first_day = start.replace(hour=4, minute=0, second=0, microsecond=0)
    if start < first_day:
        first_day -= timedelta(days=1)
    days = (exclusive_end - first_day).days
    if days <= 0:
        return None
    return OriginalSideStory(
        activity_id=identifier,
        replica_id=next(iter(replicas)),
        name=name,
        starts_at=start,
        ends_at=end,
        game_days=days,
    )


def with_activity_history(snapshot: ActivitySnapshot, player: dict) -> ActivitySnapshot:
    stories = []
    for story in snapshot.data.side_stories:
        if story.key.casefold() != "ssreopen":
            stories.append(story)
            continue
        original = original_story(player, story.name)
        if (
            original is not None
            and (four_am_end(original.ends_at) or original.ends_at) > story.starts_at
        ):
            original = None
        stories.append(replace(story, original=original))
    return replace(snapshot, data=replace(snapshot.data, side_stories=tuple(stories)))

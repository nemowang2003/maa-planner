from datetime import datetime, timedelta

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.activity import ActivityData, ActivitySnapshot


def empty_activity():
    now = datetime.now(BUSINESS_TIMEZONE)
    return ActivitySnapshot(
        data=ActivityData(side_stories=(), mini_games=(), resource_collection=None),
        source_url="https://test/activity",
        fetched_at=now,
        checked_at=now,
        expires_at=now + timedelta(minutes=5),
    )

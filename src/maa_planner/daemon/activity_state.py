"""Remember combat deadlines across restarts; retain business evidence separately."""

import json
import math
import sqlite3

from maa_planner.daemon.planning.activity import ActivitySnapshot


def observe_windows(db: sqlite3.Connection, snapshot: ActivitySnapshot, *, now: float) -> None:
    if snapshot.stale or snapshot.refresh_error is not None:
        return
    checked = snapshot.checked_at.timestamp()
    with db:
        for story in snapshot.data.side_stories:
            start, end = story.starts_at.timestamp(), story.ends_at.timestamp()
            if end <= now:
                continue  # Historical upstream entries are not current state.
            previous = db.execute(
                "SELECT checked_at, ended_at FROM activity_windows WHERE name=? AND starts_at=?",
                (story.name, start),
            ).fetchone()
            if previous is not None and previous[0] >= checked:
                continue
            # Already-ended entries on first observation are history, not new transitions.
            ended = (previous[1] if previous else now) if end <= now else None
            db.execute(
                "INSERT INTO activity_windows VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(name,starts_at) DO UPDATE SET ends_at=excluded.ends_at, "
                "activity_key=excluded.activity_key,checked_at=excluded.checked_at,"
                "ended_at=excluded.ended_at",
                (story.name, start, end, story.key, checked, ended),
            )


def expire_state(db: sqlite3.Connection, *, now: float, event) -> float | None:
    """Missing upstream entries do not end an event; its remembered deadline does."""
    if not math.isfinite(now):
        raise ValueError("活动状态清理时间无效")
    with db:
        for name, start, end, key in db.execute(
            "SELECT name, starts_at, ends_at, activity_key FROM activity_windows "
            "WHERE ended_at IS NULL AND ends_at<=?",
            (now,),
        ).fetchall():
            event(
                "activity_ended",
                {
                    "key": key,
                    "name": name,
                    "starts_at": start,
                    "ends_at": end,
                    "observed_at": now,
                },
            )
        deadlines = [
            row[0]
            for row in db.execute(
                "SELECT ends_at FROM activity_windows WHERE ended_at IS NULL AND ends_at>?", (now,)
            )
        ]
        # The user defines the combat deadline as the end of all activity strategy.
        # Missing expiry in an old record is unknown, not permission to delete it.
        for profile, activity_id, fingerprint, payload in db.execute(
            "SELECT profile, activity_id, fingerprint, payload FROM activity_shops"
        ).fetchall():
            state = json.loads(payload)
            window = db.execute(
                "SELECT ends_at FROM activity_windows WHERE name=? AND starts_at=?",
                (state.get("activity_name"), state.get("activity_starts_at")),
            ).fetchone()
            expiry = window[0] if window is not None else state.get("state_expires_at")
            if type(expiry) not in (int, float) or not math.isfinite(expiry):
                continue
            if expiry > now:
                deadlines.append(expiry)
                continue
            db.execute(
                "DELETE FROM activity_shops WHERE profile=? AND activity_id=? AND fingerprint=?",
                (profile, activity_id, fingerprint),
            )
            event(
                "activity_state_cleaned",
                {
                    "profile": profile,
                    "activity_id": activity_id,
                    "fingerprint": fingerprint,
                    "state_expires_at": expiry,
                    "observed_at": now,
                },
            )
        for activity_id, start, payload in db.execute(
            "SELECT activity_id, starts_at, payload FROM activity_shop_navigation"
        ).fetchall():
            state = json.loads(payload)
            window = db.execute(
                "SELECT ends_at FROM activity_windows WHERE name=? AND starts_at=?",
                (state["activity_name"], start),
            ).fetchone()
            expiry = window[0] if window is not None else state["state_expires_at"]
            if expiry > now:
                deadlines.append(expiry)
                continue
            db.execute(
                "DELETE FROM activity_shop_navigation WHERE activity_id=? AND starts_at=?",
                (activity_id, start),
            )
        db.execute("DELETE FROM activity_windows WHERE ends_at<=?", (now,))
    return min(deadlines, default=None)

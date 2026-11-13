"""Expose explicit regional windows without inferring unlocks or completion."""

import math
from datetime import datetime

from maa_planner.daemon.constants import BUSINESS_TIMEZONE


def activity_regions(activity, catalog, *, now):
    output = []
    table, zones = catalog["activity_table"], catalog["zone_table"]
    for story in activity.data.side_stories:
        matches = [
            v
            for v in table["basicInfo"].values()
            if v.get("name") == story.name
            and v["startTime"] < story.ends_at.timestamp()
            and v["endTime"] >= story.starts_at.timestamp()
        ]
        entry = {
            "key": story.key,
            "activity_id": None,
            "regions": [],
            "regions_known": False,
            "catalog_commit": catalog["commit"],
        }
        if len(matches) == 1:
            entry["activity_id"] = matches[0]["id"]
            for zone_id, activity_id in table["zoneToActivity"].items():
                if activity_id != entry["activity_id"]:
                    continue
                window = zones.get("zoneValidInfo", {}).get(zone_id, {})
                start, end = window.get("startTs"), window.get("endTs")
                valid = (
                    all(
                        type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (start, end)
                    )
                    and end > start
                )
                region = {
                    "zone_id": zone_id,
                    "name": zones.get("zones", {}).get(zone_id, {}).get("zoneNameSecond"),
                    "starts_at": None,
                    "ends_at": None,
                    "status": "unknown",
                }
                if valid:
                    region.update(
                        starts_at=datetime.fromtimestamp(start, BUSINESS_TIMEZONE),
                        ends_at=datetime.fromtimestamp(end + 1, BUSINESS_TIMEZONE),
                        status="upcoming"
                        if now < start
                        else "ended"
                        if now >= end + 1
                        else "active",
                    )
                entry["regions"].append(region)
            entry["regions_known"] = bool(entry["regions"]) and all(
                r["status"] != "unknown" for r in entry["regions"]
            )
        output.append(entry)
    return output

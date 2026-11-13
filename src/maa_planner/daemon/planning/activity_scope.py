"""Complete activity targets and region windows from one frozen game catalog."""

import math

from maa_planner.daemon.planning.stories import trailing_stories


def activity_scope(catalog: dict, activity_id: str, *, now: float | None = None) -> dict:
    stages = catalog["stage_table"]["stages"]
    zones = catalog["zone_table"]
    zone_ids = sorted(
        k for k, v in catalog["activity_table"]["zoneToActivity"].items() if v == activity_id
    )
    tails = set(trailing_stories(catalog, activity_id))
    complete, starts, ends, required = bool(zone_ids), [], [], []
    for zone_id in zone_ids:
        validity = zones.get("zoneValidInfo", {}).get(zone_id, {})
        start, end = validity.get("startTs"), validity.get("endTs")
        if (
            any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (start, end))
            or end <= start
        ):
            complete = False
        else:
            starts.append(start)
            # Game tables use inclusive last-second expiry.
            ends.append(end + 1)
        normal = [
            (k, v)
            for k, v in stages.items()
            if v.get("zoneId") == zone_id and v.get("difficulty") == "NORMAL"
        ]
        if not normal:
            complete = False
        for identifier, stage in normal:
            # Intermediate plots are handled while navigating to later battles.
            if stage.get("isStoryOnly") is True and identifier not in tails:
                continue
            window = catalog["stage_table"].get("stageValidInfo", {}).get(identifier)
            opened = now is None or (
                type(start) in (int, float)
                and type(end) in (int, float)
                and start <= now <= end
                and (
                    window is None
                    or (type(window.get("startTs")) in (int, float) and window["startTs"] <= now)
                )
            )
            if opened:
                required.append([identifier, False])
            if "hardStagedId" not in stage:
                complete = False
            raid = stage.get("hardStagedId")
            if raid is not None:
                target = stages.get(raid)
                if (
                    not isinstance(target, dict)
                    or target.get("zoneId") != zone_id
                    or target.get("difficulty") != "FOUR_STAR"
                ):
                    complete = False
                if opened:
                    required.append([identifier, True])
            window = catalog["stage_table"].get("stageValidInfo", {}).get(identifier)
            if window is not None:
                if type(window.get("startTs")) not in (int, float) or not math.isfinite(
                    window["startTs"]
                ):
                    complete = False
                else:
                    starts.append(window["startTs"])
    required.sort()
    return {
        "zones": zone_ids,
        "required": required,
        "catalog_complete": complete,
        "all_regions_from": max(starts) if starts else None,
        "all_regions_until": min(ends) if ends else None,
    }

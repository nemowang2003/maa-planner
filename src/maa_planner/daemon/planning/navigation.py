"""Game-data-derived navigation labels and page inputs."""

NAVIGATION_REVISION = "2026-11-12.4"


def activity_labels(catalog: dict, activity_id: str) -> list[str]:
    """Entry labels do not depend on optional reward metadata."""
    table = catalog["activity_table"]
    labels = [table["basicInfo"][activity_id]["name"]]
    for zone, owner in table["zoneToActivity"].items():
        if owner == activity_id:
            label = catalog["zone_table"]["zones"].get(zone, {}).get("zoneNameSecond")
            if isinstance(label, str) and label.strip('“”" '):
                labels.append(label.strip('“”" '))
    return list(dict.fromkeys(labels))


def activity_page_data(catalog: dict, activity_id: str) -> dict:
    """Freeze mission anchors; missing optional data remains an explicit error."""
    import re

    table = catalog["activity_table"]
    missions, texts = [], []
    data = table.get("missionData")
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        if item.get("missionGroup") != activity_id:
            continue
        text = item.get("description")
        if not isinstance(text, str) or not text.strip():
            continue
        # Game UI renders rich-text tags, not their serialized markup.
        text = re.sub(r"<[^>]*>", "", text).strip()
        if not text:
            continue
        missions.append(item["id"])
        texts.append(text)
    return {
        "activity_labels": activity_labels(catalog, activity_id),
        "mission_texts": list(dict.fromkeys(texts)),
        "mission_page_source": {
            "table": "activity_table.missionData",
            "catalog_commit": catalog["commit"],
            "activity_id": activity_id,
            "mission_ids": missions,
        },
    }

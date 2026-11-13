"""Current recognition ROI artifacts; absence is not a business completion fact."""

import hashlib
import json
import re
from pathlib import Path

from maa_planner.daemon.tools.locks import acquire, data_resource
from maa_planner.daemon.tools.storage import write_json

VIEWPORT = [1280, 720]
REVISION = 1
CLAIM_TEXT = ("一键领取", "键领取")


def valid_roi(value):
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(type(v) is int for v in value)
        and min(value[:2]) >= 0
        and min(value[2:]) > 0
        and value[0] + value[2] <= VIEWPORT[0]
        and value[1] + value[3] <= VIEWPORT[1]
    )


def reward_roi_key(plan):
    identity = {
        "revision": REVISION,
        "viewport": VIEWPORT,
        "activity_id": plan["activity_id"],
        "available_from": plan["available_from"],
        "available_until": plan["available_until"],
        "navigation_revision": plan["navigation_revision"],
        "source": plan["navigation_source"],
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def roi_from_match(result):
    if not isinstance(result, dict) or result.get("text") not in CLAIM_TEXT:
        return None
    rect = result.get("rect")
    if not valid_roi(rect):
        return None
    x, y, w, h = rect
    px, py = max(8, w // 4), max(8, h // 2)
    left, top = max(0, x - px), max(0, y - py)
    right, bottom = min(VIEWPORT[0], x + w + px), min(VIEWPORT[1], y + h + py)
    return [left, top, right - left, bottom - top]


def valid_key(key):
    return isinstance(key, str) and re.fullmatch("[a-f0-9]{64}", key) is not None


def load_claim_roi(directory: Path, key):
    if not valid_key(key):
        raise ValueError("领奖 ROI 缓存身份无效")
    path = directory / "activity-roi" / (key + ".json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("key") != key
        or value.get("revision") != REVISION
        or value.get("viewport") != VIEWPORT
        or not valid_roi(value.get("roi"))
    ):
        return None
    return value["roi"]


def save_claim_roi(directory: Path, report):
    key, roi = report.get("claim_roi_key"), report.get("claim_roi")
    if not valid_key(key) or not valid_roi(roi) or not report.get("page_verified"):
        return False
    root = directory / "activity-roi"
    root.mkdir(parents=True, exist_ok=True)
    with acquire([data_resource(root)]):
        write_json(
            root / (key + ".json"),
            {
                "key": key,
                "roi": roi,
                "viewport": VIEWPORT,
                "revision": REVISION,
                "activity_id": report["activity_id"],
            },
        )
    return True

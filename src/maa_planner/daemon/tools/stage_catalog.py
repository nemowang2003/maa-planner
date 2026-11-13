"""Supplement MAA's activity snapshot with explicit game-stage relationships."""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

from maa_planner.daemon.constants import GITHUB_API_URL, GITHUB_RAW_URL, STAGE_DATA_REPOSITORY
from maa_planner.daemon.tools.cache_cleanup import touch_cache
from maa_planner.daemon.tools.locks import acquire, data_resource
from maa_planner.daemon.tools.storage import write_json

MAX_TABLE = 32 * 1024 * 1024


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "maa-planner"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(MAX_TABLE + 1)
    if len(body) > MAX_TABLE:
        raise ValueError("关卡表超过大小限制")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("关卡表返回格式错误")
    return value


def load_stage_catalog(cache: Path) -> dict:
    # All tables come from one immutable commit; never combine racing master
    # responses. Current activity eligibility still comes exclusively from MAA.
    commit = _get(f"{GITHUB_API_URL}/repos/{STAGE_DATA_REPOSITORY}/commits/master").get("sha")
    if not isinstance(commit, str) or not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("游戏数据版本无效")
    root = cache / "stage-catalog"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (commit + ".json")
    with acquire([data_resource(root)]):
        if path.is_file():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(document, dict)
                    and document.get("commit") == commit
                    and all(
                        isinstance(document.get(name), dict)
                        for name in ("stage_table", "zone_table", "activity_table")
                    )
                ):
                    touch_cache(path)
                    return document
            except (OSError, ValueError):
                pass
        base = f"{GITHUB_RAW_URL}/{STAGE_DATA_REPOSITORY}/{commit}/cn/gamedata/excel"
        document = {"commit": commit, "source": base}
        for name in ("stage_table", "zone_table", "activity_table"):
            document[name] = _get(f"{base}/{name}.json")
        write_json(path, document)
        return document


def load_campaign_table(cache: Path, commit: str) -> dict:
    if not re.fullmatch("[a-f0-9]{40}", commit):
        raise ValueError("剿灭目录版本无效")
    root = cache / "campaign-catalog"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (commit + ".json")
    with acquire([data_resource(root)]):
        if path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("commit") == commit and isinstance(
                document.get("campaign_table"), dict
            ):
                touch_cache(path)
                return document["campaign_table"]
            raise ValueError("剿灭目录缓存版本不匹配")
        table = _get(
            f"{GITHUB_RAW_URL}/{STAGE_DATA_REPOSITORY}/{commit}/cn/gamedata/excel/campaign_table.json"
        )
        write_json(path, {"commit": commit, "campaign_table": table})
        return table


def load_processing_catalog(cache: Path, commit: str) -> dict:
    """Recipes and item rarity share the exact stage-data commit of this batch."""
    if not re.fullmatch("[a-f0-9]{40}", commit):
        raise ValueError("加工目录版本无效")
    root = cache / "processing-catalog"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (commit + ".json")
    with acquire([data_resource(root)]):
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("commit") == commit and all(
                isinstance(document.get(k), dict) for k in ("building_data", "item_table")
            ):
                return document
            raise ValueError("加工目录缓存版本不匹配")
        base = f"{GITHUB_RAW_URL}/{STAGE_DATA_REPOSITORY}/{commit}/cn/gamedata/excel"
        document = {
            "commit": commit,
            **{k: _get(f"{base}/{k}.json") for k in ("building_data", "item_table")},
        }
        write_json(path, document)
        return document

"""Discover UI navigation candidates from serialized prefabs, without game access.

Candidates preserve provenance and uncertainty. They are not runtime approvals.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import TypedDict


class DumpAsset(TypedDict):
    source: str
    path_id: int
    container: str
    type: str
    name: str
    text: str
    sha256: str


def _pointer(text, field):
    match = re.search(
        r"^\tPPtr<[^\n]+> "
        + re.escape(field)
        + r"\n\t\tint m_FileID = (\d+)\n\t\tSInt64 m_PathID = (-?\d+)",
        text,
        re.M,
    )
    return (int(match[1]), int(match[2])) if match else None


def _vector(text, field, dimensions=2):
    match = re.search(
        r"^\t(?:Vector[23]f|Quaternionf) "
        + re.escape(field)
        + r"\n((?:\t\tfloat [xyzw] = [^\n]+\n?)+)",
        text,
        re.M,
    )
    if not match:
        raise ValueError(f"布局缺少 {field}")
    values = [float(v) for v in re.findall(r"= ([^\n]+)", match[1])]
    if len(values) != dimensions or not all(math.isfinite(v) for v in values):
        raise ValueError(f"布局 {field} 无效")
    return values


class PrefabDump:
    """Read the fixed AssetStudio dump format; keep references file-local."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.assets: dict[tuple[str, int], DumpAsset] = {}
        self.transforms = {}
        for entry in ET.parse(self.directory / "assets.xml").getroot().findall("Asset"):
            if entry.findtext("Type") not in {
                "MonoBehaviour",
                "RectTransform",
                "Sprite",
            }:
                continue
            source = entry.findtext("Source")
            identifier = entry.findtext("PathID")
            name, container = entry.findtext("Name"), entry.findtext("Container") or ""
            if not source or not identifier or not name:
                raise ValueError("资源索引字段缺失")
            key = (source, int(identifier))
            if key in self.assets:
                raise ValueError("同一资源文件内对象 ID 重复")
            relative = PurePosixPath(container)
            if relative.is_absolute() or ".." in relative.parts or "/" in name or "\\" in name:
                raise ValueError("资源索引路径无效")
            folder = relative.with_suffix("") if container else PurePosixPath()
            path = self.directory / folder / (name + ".txt")
            if not path.resolve().is_relative_to(self.directory):
                raise ValueError("资源对象离开导出目录")
            text = path.read_text(encoding="utf-8-sig")
            self.assets[key] = {
                "source": Path(source).name,
                "path_id": int(identifier),
                "container": container,
                "type": entry.findtext("Type") or "",
                "name": name,
                "text": text,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            if entry.findtext("Type") == "RectTransform":
                go = self.local(key, _pointer(text, "m_GameObject"))
                if go is None:
                    raise ValueError("RectTransform 缺少 GameObject 引用")
                if go in self.transforms:
                    raise ValueError("GameObject 包含重复 RectTransform")
                self.transforms[go] = key

    @staticmethod
    def local(key, pointer):
        if pointer is None or pointer[1] == 0:
            return None
        if pointer[0] != 0:
            raise ValueError("跨文件引用缺少依赖映射，不能猜测")
        return key[0], pointer[1]

    def rect(self, go, viewport=(1280, 720), visiting=None):
        visiting = set() if visiting is None else visiting
        transform = self.transforms.get(go)
        if transform is None or transform in visiting:
            raise ValueError("布局引用缺失或循环")
        visiting.add(transform)
        text = self.assets[transform]["text"]
        if _vector(text, "m_LocalRotation", 4) != [0, 0, 0, 1]:
            raise ValueError("旋转布局需要验证，不能套用轴对齐坐标")
        scale = _vector(text, "m_LocalScale", 3)
        if any(v != 1 for v in scale):
            raise ValueError("非单位缩放布局需要验证")
        parent = self.local(transform, _pointer(text, "m_Father"))
        if parent is None:
            # A prefab root is mounted by the game's UI controller. The root
            # canvas is an explicit hypothesis, never inferred from zero size.
            return 0.0, 0.0, float(viewport[0]), float(viewport[1])
        parent_go = self.local(parent, _pointer(self.assets[parent]["text"], "m_GameObject"))
        px, py, pw, ph = self.rect(parent_go, viewport, visiting)
        lo, hi = _vector(text, "m_AnchorMin"), _vector(text, "m_AnchorMax")
        delta, position, pivot = (
            _vector(text, name) for name in ("m_SizeDelta", "m_AnchoredPosition", "m_Pivot")
        )
        if any(not 0 <= a <= b <= 1 for a, b in zip(lo, hi)):
            raise ValueError("锚点范围无效")
        width, height = pw * (hi[0] - lo[0]) + delta[0], ph * (hi[1] - lo[1]) + delta[1]
        if min(width, height) <= 0:
            raise ValueError("布局矩形为空")
        x = px + pw * (lo[0] + (hi[0] - lo[0]) * pivot[0]) + position[0] - width * pivot[0]
        y = py + ph * (lo[1] + (hi[1] - lo[1]) * pivot[1]) + position[1] - height * pivot[1]
        return x, y, width, height

    def evidence(self, key):
        return {
            k: self.assets[key][k] for k in ("source", "path_id", "container", "type", "sha256")
        }


def discover_navigation(directory: Path, catalog: dict, activity_id: str) -> dict:
    basic = catalog["activity_table"]["basicInfo"][activity_id]
    ui_type = basic.get("type", "")
    archetype = re.fullmatch(r"TYPE_(ACT[0-9]+(?:SIDE|SRE|D[0-9]+))", ui_type)
    ui_ids = {activity_id, archetype[1].lower()} if archetype else {activity_id}
    index = PrefabDump(directory)
    buttons, anchors, rejected = [], [], []
    # These are shared serialized UI semantics, not activity IDs or titles.
    methods = {
        "EventOnShopClicked": "shop",
        "EventOnShopBtnClicked": "shop",
        "EventOnClaimAllClick": "claim_all",
    }
    for key, asset in index.assets.items():
        if asset["type"] != "MonoBehaviour" or not re.search(
            r"(?:/|\[uc\])(?:" + "|".join(re.escape(v) for v in sorted(ui_ids)) + r")(?:/|_)",
            asset["container"],
        ):
            continue
        text = asset["text"]
        methods_found = set(re.findall(r'string m_MethodName = "([^\"]+)"', text))
        roles = {role for name, role in methods.items() if name in methods_found}
        if 'string _bindParam = "tempalte_mission"' in text:
            roles.add("missions")
        for role in sorted(roles):
            candidate = {"role": role, "object": index.evidence(key)}
            try:
                go = index.local(key, _pointer(text, "m_GameObject"))
                x, y, w, h = index.rect(go)
                candidate["rect"] = [round(x, 3), round(720 - y - h, 3), round(w, 3), round(h, 3)]
                candidate["layout_assumption"] = "prefab_root_mounted_on_1280x720_canvas"
                candidate["placement"] = (
                    "scene_root_hypothesis"
                    if asset["container"].endswith("_activity_stage.prefab")
                    else "unresolved_subprefab_mount"
                )
                if min(x, y) < 0 or x + w > 1280 or y + h > 720:
                    raise ValueError("点击候选超出画面")
                buttons.append(candidate)
            except (ValueError, KeyError) as error:
                rejected.append({**candidate, "reason": str(error)})
        # A title image field identifies a page-specific anchor without OCR or
        # knowing the localized title. Do not turn that reference into a match
        # threshold or screen ROI without rendering/replay evidence.
        if "/mission/" in asset["container"] or "/shop/" in asset["container"]:
            pointer = _pointer(text, "_titleImg")
            if pointer and pointer[1]:
                try:
                    sprite = index.local(key, pointer)
                    if sprite not in index.assets or index.assets[sprite]["type"] != "Sprite":
                        raise ValueError("页面标题不是已解析的 Sprite")
                    anchors.append(
                        {
                            "role": "missions" if "/mission/" in asset["container"] else "shop",
                            "field": "_titleImg",
                            "object": index.evidence(key),
                            "sprite": {
                                **index.evidence(sprite),
                                "name": index.assets[sprite]["name"],
                            },
                        }
                    )
                except (ValueError, KeyError) as error:
                    rejected.append({"object": index.evidence(key), "reason": str(error)})
    missions = [
        v
        for v in catalog["activity_table"].get("missionData", [])
        if v.get("missionGroup") == activity_id
    ]
    return {
        "activity_id": activity_id,
        "activity_name": basic["name"],
        "catalog_commit": catalog["commit"],
        "ui_ids": sorted(ui_ids),
        "dump_index_sha256": hashlib.sha256((directory / "assets.xml").read_bytes()).hexdigest(),
        "status": "candidate",
        "runtime_ready": False,
        "buttons": buttons,
        "page_anchor_sprites": anchors,
        "rejected": rejected,
        "mission_style": catalog["activity_table"]
        .get("activityTemplateMissionStyles", {})
        .get(activity_id),
        "mission_text_candidates": [
            {"mission_id": v["id"], "text": v["description"]} for v in missions
        ],
        "remaining_validation": [
            "prefab mounting and animation",
            "template scale and visible area",
            "page discrimination against other pages",
        ],
    }

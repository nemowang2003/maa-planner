"""Prepare masked private navigation from fixed exported sprite paths; no prefab."""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image, ImageStat

from maa_planner.daemon.adapters.windows import filesystem_path
from maa_planner.daemon.execution.png import encode_png

REVISION = "masked-sprites-1"
SCALES = (1.0, 0.75, 2 / 3, 0.5)


def navigation_ui_id(catalog, activity_id):
    kind = catalog["activity_table"]["basicInfo"][activity_id].get("type", "")
    match = re.fullmatch(r"TYPE_(ACT[0-9]+(?:SIDE|SRE|D[0-9]+))", kind)
    result = match[1].lower() if match else activity_id
    if not re.fullmatch(r"[a-z0-9_]+", result):
        raise ValueError("活动 UI 身份无效")
    return result


def build_navigation_assets(snapshot: Path, ui_id: str):
    snapshot = filesystem_path(snapshot)
    if not re.fullmatch(r"[a-z0-9_]+", ui_id):
        raise ValueError("活动 UI 身份无效")
    raw_manifest = (snapshot / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    assets = {v["path"]: v for v in manifest["assets"]}
    root = f"images/dyn/activity/[uc]{ui_id}/prefabs/"
    images = {}
    sources = {}

    def load(relative):
        path = snapshot / relative
        if not path.resolve().is_relative_to(snapshot.resolve()) or relative not in assets:
            raise ValueError("活动导航缺少素材: " + relative)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != assets[relative]["sha256"]:
            raise ValueError("活动导航素材校验失败: " + relative)
        with Image.open(io.BytesIO(data)) as image:
            if (
                image.format != "PNG"
                or max(image.size) > 4096
                or image.width * image.height > 4194304
            ):
                raise ValueError("活动导航素材尺寸或格式无效")
            result = image.convert("RGBA")
        sources[relative] = digest
        return result

    def encode(image):
        output = io.BytesIO()
        image.save(output, format="PNG")
        data = output.getvalue()
        name = "Planner@Sprite_" + hashlib.sha256(data).hexdigest() + ".png"
        images[name] = encode_png(data)
        return name

    entries = {}
    for role, filename in (("shop", "shop"), ("missions", "mission")):
        variants = []
        for suffix in ("", "_02"):
            relative = root + f"{ui_id}_activity_stage/bg_{filename}{suffix}.png"
            if relative not in assets:
                continue
            original = load(relative)
            for scale in SCALES:
                size = tuple(round(v * scale) for v in original.size)
                if min(size) < 8 or size[0] > 1280 or size[1] > 720:
                    continue
                image = original.resize(size, Image.Resampling.LANCZOS)
                # Core reads RGB and creates its mask by grayscale range.
                pixels = [
                    (r, g, b) if a == 255 else (0, 0, 0)
                    for r, g, b, a in image.get_flattened_data()
                ]
                points = [
                    (i % size[0], i // size[0]) for i, pixel in enumerate(pixels) if max(pixel) >= 8
                ]
                if len(points) < 16:
                    continue
                x, y = min(
                    points, key=lambda p: ((p[0] - size[0] / 2) ** 2 + (p[1] - size[1] / 2) ** 2, p)
                )
                masked = Image.new("RGB", size)
                masked.putdata(pixels)
                if max(ImageStat.Stat(masked).var) < 1:
                    continue
                variants.append(
                    {
                        "template": encode(masked),
                        "rectMove": [x, y, 1, 1],
                        "source": relative,
                        "size": list(size),
                    }
                )
        if not variants:
            raise ValueError(f"活动导航缺少可识别的 {role} 入口素材")
        entries[role] = variants

    banner_path = root + f"shop/title_shop_{ui_id}/img_bkg.png"
    banner = load(banner_path)
    candidates = []
    for y in range(0, banner.height - 64 + 1, 32):
        for x in range(0, banner.width - 128 + 1, 32):
            rect = (x, y, x + 128, y + 64)
            patch = banner.crop(rect)
            if patch.getchannel("A").getextrema() != (255, 255):
                continue
            variance = sum(ImageStat.Stat(patch.convert("RGB")).var)
            if variance >= 500:
                candidates.append((variance, rect))
    selected = []
    for _, rect in sorted(candidates, reverse=True):
        if any(
            max(0, min(rect[2], old[2]) - max(rect[0], old[0]))
            * max(0, min(rect[3], old[3]) - max(rect[1], old[1]))
            > 128 * 64 * 0.25
            for old in selected
        ):
            continue
        selected.append(rect)
        if len(selected) == 3:
            break
    if not selected:
        raise ValueError("商店横幅缺少可识别的不透明细节")
    ready = []
    for rect in selected:
        patch = banner.crop(rect).convert("RGB")
        for scale in SCALES:
            ready.append(
                encode(
                    patch.resize(
                        tuple(round(v * scale) for v in patch.size), Image.Resampling.LANCZOS
                    )
                )
            )
    return {
        "entries": entries,
        "shop_ready": list(dict.fromkeys(ready)),
        "images": images,
        "source": {
            "revision": REVISION,
            "ui_id": ui_id,
            "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "sprites": sources,
            "shop_patches": [list(v) for v in selected],
        },
    }


def prepare_navigation_assets(settings, catalog, activity_id, cancel):
    from maa_planner.daemon.tools.activity_assets import prepare_assets

    ui_id = navigation_ui_id(catalog, activity_id)
    prepared = prepare_assets(
        settings,
        {
            "activity_id": activity_id,
            "bundles": [f"activity/[uc]{ui_id}.ab"],
            "resolve_dependencies": True,
            "max_bundle_bytes": 64 * 1024**2,
            "max_output_bytes": 128 * 1024**2,
            "export_timeout": 120,
        },
        cancel,
    )
    if cancel.is_set():
        raise InterruptedError("活动导航资源准备取消")
    result = build_navigation_assets(Path(prepared["snapshot"]), ui_id)
    result["source"].update(
        activity_id=activity_id, snapshot=prepared["identity"], res_version=prepared["res_version"]
    )
    return result

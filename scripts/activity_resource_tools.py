# /// script
# requires-python = ">=3.14"
# dependencies = ["pillow==12.2.0", "numpy==2.5.4", "opencv-python-headless==5.0.0.93"]
# ///
"""Deterministic, offline activity navigation resource tools; no game access."""

import argparse
import base64
import hashlib
import io
import json
import math
import re
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

TOOL_VERSION = 1


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bounds(value, limit):
    if not isinstance(value, list) or len(value) != 4 or any(type(n) is not int for n in value):
        raise ValueError("rectangle must contain four integers")
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > limit[0] or y + h > limit[1]:
        raise ValueError("rectangle outside image")
    return x, y, x + w, y + h


def contained(inner, outer):
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


class Workspace:
    def __init__(self, path):
        self.root = path.resolve().parent
        self.manifest = json.loads(path.read_text(encoding="utf-8"))
        for key in ("activity_id", "res_version", "bundle_hash", "core_version"):
            if not isinstance(self.manifest.get(key), str) or not self.manifest[key]:
                raise ValueError(f"missing version evidence: {key}")
        self.assets = self.index("assets")
        self.frames = self.index("frames")

    def index(self, name):
        entries = self.manifest.get(name)
        if not isinstance(entries, list):
            raise ValueError(f"missing {name} manifest")
        output = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not entry["id"]
                or entry["id"] in output
            ):
                raise ValueError("duplicate or invalid asset/frame ID")
            if not re.fullmatch("[a-f0-9]{64}", entry.get("sha256", "")):
                raise ValueError("missing immutable image hash")
            output[entry["id"]] = entry
        return output

    def image(self, entry):
        path = self.root / entry["path"]
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("image path escapes workspace")
        with path.open("rb") as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024 or digest(data) != entry["sha256"]:
            raise ValueError("image missing, oversized or changed since manifest")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or (
                max(image.size) > 4096 or image.width * image.height > 4194304
            ):
                raise ValueError("bounded PNG required")
            return image.convert("RGBA")

    def assets_list(self):
        return [{**entry, "size": list(self.image(entry).size)} for entry in self.assets.values()]

    def inspect(self, identifier, region=None):
        entry = self.assets.get(identifier) or self.frames.get(identifier)
        if entry is None:
            raise ValueError("unknown image ID")
        image = self.image(entry)
        region = region or [0, 0, *image.size]
        return image.crop(bounds(region, image.size)), {
            "id": identifier,
            "source_sha256": entry["sha256"],
            "source_size": image.size,
            "crop": region,
            "preview_scale": 1,
        }

    def materialize(self, candidate):
        if set(candidate) != {
            "asset_id",
            "crop",
            "size",
            "background",
            "roi",
            "click",
            "threshold",
            "model_version",
            "prompt_version",
        }:
            raise ValueError(
                "candidate must explicitly specify every processing and matching parameter"
            )
        for key in ("model_version", "prompt_version"):
            if not isinstance(candidate[key], str) or not candidate[key]:
                raise ValueError("missing candidate provenance")
        threshold = candidate["threshold"]
        if (
            type(threshold) not in (int, float)
            or not math.isfinite(threshold)
            or not 0 < threshold <= 1
        ):
            raise ValueError("explicit finite threshold in (0,1] required")
        image = self.image(self.assets[candidate["asset_id"]])
        image = image.crop(bounds(candidate["crop"], image.size))
        size = candidate["size"]
        if (
            not isinstance(size, list)
            or len(size) != 2
            or any(type(n) is not int or not 1 <= n <= 1280 for n in size)
        ):
            raise ValueError("invalid output size")
        image = image.resize(tuple(size), Image.Resampling.LANCZOS)
        background = candidate["background"]
        if background is None:
            if image.getchannel("A").getextrema() != (255, 255):
                raise ValueError("transparent template needs an explicit compositing background")
        else:
            if (
                not isinstance(background, list)
                or len(background) != 3
                or any(type(n) is not int or not 0 <= n <= 255 for n in background)
            ):
                raise ValueError("invalid RGB background")
            image = Image.alpha_composite(
                Image.new("RGBA", image.size, tuple(background) + (255,)), image
            )
        image = image.convert("RGB")
        if np.asarray(image).reshape(-1, 3).std(axis=0).max() == 0:
            raise ValueError("constant templates cannot be replayed reliably")
        roi = bounds(candidate["roi"], (1280, 720))
        bounds(candidate["click"], (1280, 720))
        if size[0] > roi[2] - roi[0] or size[1] > roi[3] - roi[1]:
            raise ValueError("template exceeds ROI")
        output = io.BytesIO()
        image.save(output, format="PNG")
        png = output.getvalue()
        if len(png) > 2 * 1024 * 1024:
            raise ValueError("template exceeds private PNG attachment budget")
        template = "Planner@" + digest(png)[:32] + ".png"
        node_name = "Planner@Entry" + digest(json.dumps(candidate, sort_keys=True).encode())[:32]
        return png, {
            node_name: {
                "algorithm": "MatchTemplate",
                "template": template,
                "templThreshold": threshold,
                "roi": candidate["roi"],
                "action": "ClickRect",
                "specificRect": candidate["click"],
                "next": [],
            }
        }

    def replay(self, candidate):
        png, nodes = self.materialize(candidate)
        template = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
        roi = bounds(candidate["roi"], (1280, 720))
        reports, coverage = [], set()
        strata = self.manifest.get("required_strata")
        if (
            not isinstance(strata, list)
            or not strata
            or any(
                not isinstance(s, list)
                or len(s) != 2
                or any(not isinstance(v, str) or not v for v in s)
                for s in strata
            )
        ):
            raise ValueError("explicit background/state coverage required")
        for frame in self.frames.values():
            image = self.image(frame)
            if image.size != (1280, 720):
                raise ValueError("replay screenshots must use Core 1280x720 coordinates")
            if (
                frame.get("split") not in {"development", "validation"}
                or any(
                    not isinstance(frame.get(k), str) or not frame[k]
                    for k in ("background", "state")
                )
                or "target" not in frame
            ):
                raise ValueError("missing split, labels or positive/negative target")
            label = frame["target"] is not None
            target = bounds(frame["target"], image.size) if label else None
            coverage.add((frame["background"], frame["state"], frame["split"], label))
            start = time.monotonic()
            response = cv2.matchTemplate(
                np.asarray(image.convert("RGB"))[roi[1] : roi[3], roi[0] : roi[2]],
                template,
                cv2.TM_CCOEFF_NORMED,
            )
            if not np.isfinite(response).all():
                raise ValueError("non-finite replay response")
            _, score, _, location = cv2.minMaxLoc(response)
            match = [roi[0] + location[0], roi[1] + location[1], *candidate["size"]]
            hit = score >= candidate["threshold"]
            localized = bool(
                target
                and contained(bounds(match, image.size), target)
                and contained(bounds(candidate["click"], image.size), target)
            )
            passed = hit and localized if label else not hit
            reports.append(
                {
                    "frame_id": frame["id"],
                    "sha256": frame["sha256"],
                    "pixel_sha256": digest(image.tobytes()),
                    "split": frame["split"],
                    "background": frame["background"],
                    "state": frame["state"],
                    "positive": label,
                    "score": score,
                    "match": match,
                    "passed": passed,
                    "elapsed_ms": (time.monotonic() - start) * 1000,
                }
            )
        required = {
            (bg, state, split, label)
            for bg, state in strata
            for split in ("development", "validation")
            for label in (True, False)
        }
        missing = sorted(required - coverage)
        # Validation images must be independent files/pixels, not renamed copies
        # of the development set. Parameter tuning still belongs to the caller.
        split_hashes = {
            s: {f["pixel_sha256"] for f in reports if f["split"] == s}
            for s in ("development", "validation")
        }
        reused = sorted(split_hashes["development"] & split_hashes["validation"])
        valid = bool(reports) and all(r["passed"] for r in reports) and not missing and not reused
        return {
            "status": "offline_validated" if valid else "needs_review",
            "missing_coverage": missing,
            "reused_validation_hashes": reused,
            "results": reports,
            "template_sha256": digest(png),
            "resource_tasks": nodes,
        }

    def submit(self, candidate, destination):
        # Recompute evidence; caller-provided reports never grant publication.
        report = self.replay(candidate)
        if report["status"] != "offline_validated":
            raise ValueError(
                "candidate requires review; replay failures or missing independent samples"
            )
        png, nodes = self.materialize(candidate)
        template = next(iter(nodes.values()))["template"]
        document = {
            "tasks": [{"type": "Custom", "params": {"task_names": list(nodes)}}],
            "resource_tasks": nodes,
            "resource_images": {template: "@file:template.png"},
            "attachments": {"template.png": base64.b64encode(png).decode("ascii")},
        }
        evidence = {
            "tool_version": TOOL_VERSION,
            "opencv_version": cv2.__version__,
            "pillow_version": Image.__version__,
            "candidate": candidate,
            "manifest": self.manifest,
            "replay": report,
            "plan_sha256": digest(json.dumps(document, sort_keys=True).encode()),
        }
        # Content addressing ignores measured elapsed times, but includes all
        # source versions/hashes and the exact processing parameters.
        identity = digest(
            json.dumps(
                {"candidate": candidate, "manifest": self.manifest, "tool": TOOL_VERSION},
                sort_keys=True,
            ).encode()
        )
        directory = destination / identity
        directory.mkdir(parents=True, exist_ok=False)
        try:
            (directory / "template.png").write_bytes(png)
            (directory / "plan.json").write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (directory / "manifest.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            # Completion marker last: incomplete staging is never a snapshot.
            (directory / "READY").write_text(identity + "\n")
        except BaseException:
            # Preserve partial output as failure evidence; never mark it READY.
            (directory / "READY").unlink(missing_ok=True)
            raise
        return {"snapshot": str(directory), "status": "offline_validated", "identity": identity}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list_assets")
    inspect = commands.add_parser("inspect_image")
    inspect.add_argument("image_id")
    inspect.add_argument("--crop", type=int, nargs=4)
    inspect.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare_frames")
    compare.add_argument("image_ids", nargs="+")
    compare.add_argument("--output", type=Path, required=True)
    for name in (
        "propose_candidate",
        "materialize_candidate",
        "replay_candidate",
        "inspect_failures",
        "submit_artifact",
    ):
        command = commands.add_parser(name)
        command.add_argument("--candidate", type=Path, required=True)
        if name in {"materialize_candidate", "submit_artifact"}:
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    workspace = Workspace(args.workspace)
    if args.command == "list_assets":
        result = workspace.assets_list()
    elif args.command == "inspect_image":
        image, result = workspace.inspect(args.image_id, args.crop)
        with args.output.open("xb") as stream:
            image.save(stream, format="PNG")
    elif args.command == "compare_frames":
        images = [workspace.inspect(identifier)[0] for identifier in args.image_ids]
        if not images or len(images) > 8:
            raise ValueError("compare 1..8 images")
        canvas = Image.new("RGBA", (sum(i.width for i in images), max(i.height for i in images)))
        x = 0
        for image in images:
            canvas.paste(image, (x, 0))
            x += image.width
        with args.output.open("xb") as stream:
            canvas.save(stream, format="PNG")
        result = {"image_ids": args.image_ids, "scale": 1, "output": str(args.output)}
    else:
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        if args.command in {"propose_candidate", "materialize_candidate"}:
            png, nodes = workspace.materialize(candidate)
            if args.command == "materialize_candidate":
                with args.output.open("xb") as stream:
                    stream.write(png)
            result = {"candidate": candidate, "resource_tasks": nodes, "sha256": digest(png)}
        elif args.command == "submit_artifact":
            result = workspace.submit(candidate, args.output)
        else:
            result = workspace.replay(candidate)
            if args.command == "inspect_failures":
                result["results"] = [r for r in result["results"] if not r["passed"]]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

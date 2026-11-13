import json
from pathlib import Path

import numpy as np  # ty: ignore[unresolved-import] -- optional authoring environment
import pytest
from PIL import Image

from maa_planner.daemon.execution.tasks import CoreTasks
from scripts.activity_resource_tools import Workspace, digest


def workspace(tmp_path):
    rng = np.random.default_rng(77)
    template = Image.fromarray(rng.integers(0, 256, (12, 24, 3), dtype=np.uint8))
    template.save(tmp_path / "asset.png")
    asset = {
        "id": "entry",
        "path": "asset.png",
        "sha256": digest((tmp_path / "asset.png").read_bytes()),
    }
    frames = []
    for split in ("development", "validation"):
        for positive in (True, False):
            image = Image.fromarray(rng.integers(0, 256, (720, 1280, 3), dtype=np.uint8))
            if positive:
                image.paste(template, (100, 200))
            name = f"{split}-{positive}.png"
            # Keep source fixtures within the attachment/image byte budget.
            image = image.quantize(colors=16).convert("RGB") if not positive else image
            if positive:
                image = Image.new(
                    "RGB", (1280, 720), (33 if split == "development" else 99, 10, 21)
                )
                image.paste(template, (100, 200))
            image.save(tmp_path / name)
            frames.append(
                {
                    "id": name,
                    "path": name,
                    "sha256": digest((tmp_path / name).read_bytes()),
                    "split": split,
                    "background": "test",
                    "state": "available",
                    "target": [90, 190, 60, 40] if positive else None,
                }
            )
    manifest = {
        "activity_id": "act_test",
        "res_version": "test",
        "bundle_hash": "a" * 32,
        "core_version": "6.21.4",
        "assets": [asset],
        "frames": frames,
        "required_strata": [["test", "available"]],
    }
    path = tmp_path / "workspace.json"
    path.write_text(json.dumps(manifest))
    candidate = {
        "asset_id": "entry",
        "crop": [0, 0, 24, 12],
        "size": [24, 12],
        "background": None,
        "roi": [0, 0, 1280, 720],
        "click": [100, 200, 24, 12],
        "threshold": 0.99,
        "model_version": "human",
        "prompt_version": "manual-v1",
    }
    return Workspace(path), candidate


def test_replay_publishes_immutable_private_plan_and_never_claims_game_validation(tmp_path):
    work, candidate = workspace(tmp_path)
    report = work.replay(candidate)
    assert report["status"] == "offline_validated"
    assert all(r["passed"] for r in report["results"])
    published = work.submit(candidate, tmp_path / "snapshots")
    folder = Path(published["snapshot"])
    assert published["status"] == "offline_validated"
    assert (folder / "READY").exists()
    plan = CoreTasks.read(folder / "plan.json")
    materialized = plan.materialize(tmp_path)
    assert (
        Path(next(iter(materialized["resource_images"].values()))).read_bytes()
        == (folder / "template.png").read_bytes()
    )
    with pytest.raises(FileExistsError):
        work.submit(candidate, tmp_path / "snapshots")


def test_missing_holdout_and_false_positive_cannot_be_published(tmp_path):
    work, candidate = workspace(tmp_path)
    frame = next(
        f for f in work.frames.values() if f["split"] == "validation" and f["target"] is None
    )
    frame["target"] = [90, 190, 60, 40]
    report = work.replay(candidate)
    assert report["status"] == "needs_review"
    assert any(not r["passed"] for r in report["results"])
    assert report["missing_coverage"]
    with pytest.raises(ValueError, match="requires review"):
        work.submit(candidate, tmp_path / "snapshots")
    assert not (tmp_path / "snapshots").exists()


def test_source_changes_path_escape_and_unlabelled_clicks_rejected(tmp_path):
    work, candidate = workspace(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        work.materialize({**candidate, "crop": [23, 0, 24, 12]})
    report = work.replay({**candidate, "click": [700, 500, 10, 10]})
    assert report["status"] == "needs_review"
    work.assets["entry"]["path"] = "../outside.png"
    with pytest.raises(ValueError, match="escapes"):
        work.materialize(candidate)
    work.assets["entry"]["path"] = "asset.png"
    (tmp_path / "asset.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        work.materialize(candidate)

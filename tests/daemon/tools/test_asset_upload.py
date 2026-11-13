import hashlib
import json

import pytest

from maa_planner.daemon.tools.asset_upload import prepare_upload
from maa_planner.daemon.tools.storage import write_json
from tests.helpers.png import PNG


@pytest.fixture
def inputs(tmp_path):
    snapshot = tmp_path / "exported"
    snapshot.mkdir()
    assets = []
    for identifier in ("shop", "missions", "ordinary", "shared_dependency"):
        (snapshot / (identifier + ".png")).write_bytes(PNG)
        assets.append(
            {
                "id": identifier,
                "path": identifier + ".png",
                "size": len(PNG),
                "sha256": hashlib.sha256(PNG).hexdigest(),
            }
        )
    source = {"activity_id": "event", "res_version": "one", "export_key": "a" * 64}
    write_json(snapshot / "manifest.json", {"source": source, "assets": assets})
    write_json(
        snapshot / "READY",
        {"identity": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()},
    )
    core = tmp_path / "core"
    core.mkdir()
    write_json(
        core / "tasks.json",
        {"TL-OpenOcr": {"algorithm": "OcrDetect"}, "ClickStageName": {"algorithm": "OcrDetect"}},
    )
    return snapshot, core, tmp_path / "upload"


def prepare(inputs, requirements, *, budget=10000):
    snapshot, core, output = inputs
    return prepare_upload(
        snapshot, [core], requirements, core_version="6.22.0", max_bytes=budget, output=output
    )


def test_package_only_has_explicit_missing_task_assets_and_deduplicates_bytes(inputs):
    snapshot, _, output = inputs
    (snapshot / "ordinary.png").write_bytes(b"unneeded corrupted file")
    result = prepare(
        inputs,
        [
            {"task": "TL-OpenOcr", "assets": ["ordinary"]},
            {"task": "TLShop", "assets": ["shop"]},
            {"task": "TLMissions", "assets": ["missions", "shop"]},
        ],
    )
    assert result["skipped_tasks"] == ["TL-OpenOcr"]
    assert result["missing_tasks"] == ["TLShop", "TLMissions"]
    assert result["asset_count"] == 2 and result["file_count"] == 1
    assert result["image_bytes"] == len(PNG)
    assert result["bytes"] == sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
    workspace = json.loads((output / "workspace.json").read_text())
    assert {asset["id"] for asset in workspace["assets"]} == {"shop", "missions"}
    assert "ordinary" not in (output / "workspace.json").read_text()
    assert not (output / "manifest.json").exists()
    assert len(list(output.rglob("*.png"))) == 1
    assert (output / "READY").exists()


def test_existing_and_implicitly_derived_tasks_create_no_upload(inputs):
    result = prepare(inputs, [{"task": "Prefix@ClickStageName", "assets": ["not-needed"]}])
    assert result["status"] == "not_needed"
    assert not inputs[2].exists()


@pytest.mark.parametrize(
    "failure",
    [
        "unknown_asset",
        "wildcard",
        "budget",
        "hash",
        "traversal",
        "ready",
        "empty_core",
        "missing_base",
    ],
)
def test_uncertain_or_invalid_selection_never_publishes_a_package(inputs, failure):
    snapshot, core, output = inputs
    requirements = [{"task": "TLShop", "assets": ["shop"]}]
    budget = 10000
    manifest = json.loads((snapshot / "manifest.json").read_text())
    if failure == "unknown_asset":
        requirements[0]["assets"] = ["not-found"]
    elif failure == "wildcard":
        requirements[0]["assets"] = ["*"]
    elif failure == "budget":
        budget = 1
    elif failure == "hash":
        (snapshot / "shop.png").write_bytes(b"changed")
    elif failure == "traversal":
        manifest["assets"][0]["path"] = "../shop.png"
        write_json(snapshot / "manifest.json", manifest)
    elif failure == "ready":
        write_json(snapshot / "READY", {"identity": "unknown"})
    elif failure == "empty_core":
        write_json(core / "tasks.json", {})
    elif failure == "missing_base":
        write_json(core / "tasks.json", {"TLShop": {"baseTask": "Unknown"}})
    with pytest.raises((ValueError, OSError)):
        prepare(inputs, requirements, budget=budget)
    assert not output.exists()


def test_overlay_core_task_is_reused_and_existing_package_is_never_overwritten(inputs, tmp_path):
    snapshot, core, output = inputs
    overlay = tmp_path / "hot"
    overlay.mkdir()
    write_json(overlay / "tasks.json", {"TLShop": {"algorithm": "OcrDetect"}})
    result = prepare_upload(
        snapshot,
        [core, overlay],
        [{"task": "TLShop", "assets": ["shop"]}],
        core_version="6.22.0",
        max_bytes=1000,
        output=output,
    )
    assert result["status"] == "not_needed"
    output.mkdir()
    (output / "keep").write_text("existing")
    with pytest.raises(FileExistsError):
        prepare(inputs, [{"task": "TLShop", "assets": ["shop"]}])
    assert (output / "keep").read_text() == "existing"


def test_nested_core_tables_are_checked_instead_of_assuming_main_file_is_complete(inputs):
    _, core, _ = inputs
    (core / "tasks.json").unlink()
    (core / "tasks" / "Stages").mkdir(parents=True)
    write_json(core / "tasks" / "tasks.json", {"ClickStageName": {"algorithm": "OcrDetect"}})
    write_json(core / "tasks" / "Stages" / "TL.json", {"TLShop": {"algorithm": "OcrDetect"}})
    result = prepare(inputs, [{"task": "TLShop", "assets": ["shop"]}])
    assert result["status"] == "not_needed"


def test_partial_table_path_and_metadata_exceeding_packet_budget_fail(inputs):
    snapshot, core, output = inputs
    with pytest.raises(ValueError, match="完整 Core"):
        prepare_upload(
            snapshot,
            [core / "tasks.json"],
            [{"task": "TLShop", "assets": ["shop"]}],
            core_version="6.22.0",
            max_bytes=10000,
            output=output,
        )
    with pytest.raises(ValueError, match="清单"):
        prepare(inputs, [{"task": "TLShop", "assets": ["shop"]}], budget=len(PNG))
    assert not output.exists()


def test_implicit_template_derivative_does_not_hide_missing_private_png(inputs):
    _, core, _ = inputs
    write_json(core / "tasks.json", {"Return": {"action": "ClickSelf"}})
    result = prepare(inputs, [{"task": "New@Return", "assets": ["shop"]}])
    assert result["missing_tasks"] == ["New@Return"]
    assert result["asset_count"] == 1

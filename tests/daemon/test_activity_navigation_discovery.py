import json
import shutil
from pathlib import Path

import pytest

from maa_planner.daemon.tools.activity_navigation import discover_navigation

FIXTURE = Path(__file__).with_name("fixtures") / "activity-navigation"


def test_real_prefab_discovers_entry_actions_and_page_anchors_without_localized_registry():
    catalog = json.loads((FIXTURE / "catalog.json").read_text())
    result = discover_navigation(FIXTURE, catalog, "act55side")
    assert result["runtime_ready"] is False  # extraction is not visual acceptance
    assert result["status"] == "candidate"
    assert {v["role"] for v in result["buttons"]} >= {"shop", "missions", "claim_all"}
    # Click coordinates independently recorded by Core during 2026-11-12's
    # successful shop and mission entry, not calculated by this implementation.
    for role, point in [("shop", (961, 147)), ("missions", (1153, 271))]:
        assert any(
            x <= point[0] <= x + w and y <= point[1] <= y + h
            for v in result["buttons"]
            if v["role"] == role
            for x, y, w, h in [v["rect"]]
        )
    anchors = {v["role"]: v["sprite"] for v in result["page_anchor_sprites"]}
    assert anchors["missions"]["name"] == "img_title"
    assert anchors["shop"]["name"] == "img_bkg"
    assert len(result["mission_text_candidates"]) == 31
    assert result["mission_style"]["isMissionListCommonType"] is True
    assert result["mission_style"]["isClaimAllBtnCommonType"] is False
    assert all(v["object"]["sha256"] for v in result["buttons"])
    assert any("缩放" in r["reason"] for r in result["rejected"])


def test_activity_identity_comes_from_inputs_not_tl_specific_code(tmp_path):
    # Renamed fixture tests absence of ID/title hardcoding; it is deliberately
    # not evidence that a second activity's distinct UI has been validated.
    target = tmp_path / "dump"
    shutil.copytree(FIXTURE, target)
    for p in list(target.rglob("*")):
        if p.is_file():
            p.write_text(p.read_text().replace("act55side", "different_event"))
    source = target / "dyn/activity/[uc]act55side"
    source.rename(source.with_name("[uc]different_event"))
    # Container paths also embed the activity ID in prefab basenames.
    for p in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if "act55side" in p.name:
            p.rename(p.with_name(p.name.replace("act55side", "different_event")))
    catalog = json.loads((target / "catalog.json").read_text())
    result = discover_navigation(target, catalog, "different_event")
    assert len(result["buttons"]) == 6
    assert len(result["page_anchor_sprites"]) == 2


def test_dump_rejects_paths_outside_export(tmp_path):
    (tmp_path / "assets.xml").write_text(
        "<Assets><Asset><Type>MonoBehaviour</Type><Name>escape</Name><Source>CAB</Source><PathID>1</PathID><Container>../outside.prefab</Container></Asset></Assets>"
    )
    catalog = json.loads((FIXTURE / "catalog.json").read_text())
    with pytest.raises(ValueError, match="路径"):
        discover_navigation(tmp_path, catalog, "act55side")


def test_cross_file_pointer_never_resolves_by_coincidental_path_id():
    from maa_planner.daemon.tools.activity_navigation import PrefabDump

    with pytest.raises(ValueError, match="跨文件"):
        PrefabDump.local(("CAB-main", 1), (2, 1))


def test_nested_prefab_candidate_does_not_claim_a_screen_position():
    catalog = json.loads((FIXTURE / "catalog.json").read_text())
    result = discover_navigation(FIXTURE, catalog, "act55side")
    claims = [v for v in result["buttons"] if v["role"] == "claim_all"]
    assert claims and all(v["placement"] == "unresolved_subprefab_mount" for v in claims)
    assert result["runtime_ready"] is False


def test_rerun_can_reuse_original_ui_without_reusing_original_activity_progress_identity():
    catalog = json.loads((FIXTURE / "catalog.json").read_text())
    basic = catalog["activity_table"]["basicInfo"].pop("act55side")
    catalog["activity_table"]["basicInfo"]["act55sre"] = {
        **basic,
        "id": "act55sre",
        "isReplicate": True,
    }
    result = discover_navigation(FIXTURE, catalog, "act55sre")
    assert result["activity_id"] == "act55sre"
    assert result["ui_ids"] == ["act55side", "act55sre"]
    assert {v["role"] for v in result["buttons"]} >= {"shop", "missions"}

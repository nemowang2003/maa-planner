import hashlib
import json

import pytest
from PIL import Image, ImageDraw

from maa_planner.daemon.tools.activity_sprite_navigation import build_navigation_assets


def snapshot(tmp_path, ui_id="event"):
    paths = []
    root = f"images/dyn/activity/[uc]{ui_id}/prefabs/"
    for role in ("shop", "mission"):
        for suffix in ("", "_02"):
            path = root + f"{ui_id}_activity_stage/bg_{role}{suffix}.png"
            image = Image.new("RGBA", (120, 60))
            draw = ImageDraw.Draw(image)
            # Thin disconnected glyphs have no large opaque rectangle. Both
            # variants must still produce templates and a nontransparent click.
            for x in range(20, 100, 8):
                draw.line((x, 22, x, 38), fill=(255, 180, 80, 255), width=2)
                draw.line((x, 22, x + 5, 22), fill="white", width=2)
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target)
            paths.append(path)
    path = root + f"shop/title_shop_{ui_id}/img_bkg.png"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    image = Image.new("RGBA", (256, 128), "black")
    draw = ImageDraw.Draw(image)
    for x in range(0, 256, 8):
        draw.rectangle((x, 0, x + 3, 127), fill="white")
    image.save(target)
    paths.append(path)
    manifest = {
        "source": {"activity_id": "event", "res_version": "v1"},
        "assets": [
            {"path": p, "sha256": hashlib.sha256((tmp_path / p).read_bytes()).hexdigest()}
            for p in paths
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def test_source_variants_generate_masked_nodes_with_opaque_relative_clicks(tmp_path):
    source = snapshot(tmp_path)
    result = build_navigation_assets(source, "event")
    for role in ("shop", "missions"):
        entries = result["entries"][role]
        assert {v["source"].split("/")[-1] for v in entries} == {
            f"bg_{'mission' if role == 'missions' else role}.png",
            f"bg_{'mission' if role == 'missions' else role}_02.png",
        }
        for item in entries:
            image = Image.open(source / item["source"]).convert("RGBA")
            image = image.resize(tuple(item["size"]), Image.Resampling.LANCZOS)
            x, y, w, h = item["rectMove"]
            assert w == h == 1
            assert image.getchannel("A").getpixel((x, y)) == 255
            assert 20 / 120 <= x / image.width <= 100 / 120
            assert item["template"] in result["images"]
    assert result["shop_ready"]
    assert all(v in result["images"] for v in result["shop_ready"])
    # Changing the source after acquisition must not silently change the plan.
    target = source / result["entries"]["shop"][0]["source"]
    target.write_bytes(b"edited")
    with pytest.raises(ValueError, match="校验"):
        build_navigation_assets(source, "event")


def test_generated_resources_reach_daily_tasks_and_freeze_into_private_images(tmp_path):
    from maa_planner.daemon.planning.activity_shop import prepare_shops
    from maa_planner.daemon.planning.daily import build_daily_plan
    from tests.helpers.activity_shop import NOW, activity, configured, shop_catalog

    assets = build_navigation_assets(snapshot(tmp_path / "source"), "event")
    shops = prepare_shops(
        shop_catalog(), activity(), now=NOW, navigation={"event": assets}, blacklist=()
    )
    result = build_daily_plan(
        configured(tmp_path), now=NOW, activity=activity(), depot={}, shops=shops
    )
    assert result.tasks is not None
    document = json.loads(result.tasks.document)
    nodes = document["resource_tasks"]
    entries = [v for k, v in nodes.items() if k.startswith("Planner@Rewards0@Entry")]
    assert len(entries) == len(assets["entries"]["missions"])
    assert all(v["next"] == ["Planner@Rewards0@MissionReady"] for v in entries)
    assert nodes["Planner@Shop0@ShopReady"]["template"] == assets["shop_ready"]
    (tmp_path / "run").mkdir()
    frozen = result.tasks.materialize(tmp_path / "run")
    assert set(frozen["resource_images"]) == set(assets["images"])
    # No execution-time source reads after admission.
    for source in assets["source"]["sprites"]:
        (tmp_path / "source" / source).unlink()
    for path in frozen["resource_images"].values():
        assert Image.open(path).format == "PNG"


def test_rerun_prepares_original_ui_bundle_but_retains_current_activity_identity(
    tmp_path, monkeypatch
):
    import threading
    from types import SimpleNamespace

    from maa_planner.daemon.tools.activity_sprite_navigation import prepare_navigation_assets

    source = snapshot(tmp_path / "source", ui_id="act44side")
    calls = []

    def acquire(settings, arguments, cancel):
        calls.append(arguments)
        return {"snapshot": str(source), "identity": "a" * 64, "res_version": "v1"}

    monkeypatch.setattr("maa_planner.daemon.tools.activity_assets.prepare_assets", acquire)
    catalog = {"activity_table": {"basicInfo": {"act44sre": {"type": "TYPE_ACT44SIDE"}}}}
    result = prepare_navigation_assets(SimpleNamespace(), catalog, "act44sre", threading.Event())
    assert calls[0]["activity_id"] == "act44sre"
    assert calls[0]["bundles"] == ["activity/[uc]act44side.ab"]
    assert calls[0]["resolve_dependencies"] is True
    assert result["source"]["activity_id"] == "act44sre"
    assert result["source"]["ui_id"] == "act44side"
    assert result["source"]["snapshot"] == "a" * 64

import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from maa_planner.daemon.commands import Submission
from maa_planner.daemon.tools import activity_assets as assets
from tests.helpers.activity_assets import ARGUMENTS, scenario, zip_bytes
from tests.helpers.png import PNG


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_versioned_private_export_reuses_unchanged_bundle_and_decoder_across_resource_versions(
    tmp_path, monkeypatch, platform
):
    settings, get, export, calls, exports, version = scenario(tmp_path, monkeypatch, platform)

    def prepare():
        return assets.prepare_assets(
            settings, ARGUMENTS, threading.Event(), get=get, export=export, platform=platform
        )

    first = prepare()
    assert first["asset_count"] == 1 and first["status"] == "exported"
    destination = Path(first["snapshot"])
    assert (destination / "images/dir/button.png").read_bytes() == PNG
    manifest = (destination / "manifest.json").read_bytes()
    second = prepare()
    assert second["reused"] and second["identity"] == first["identity"]
    version[0] = "two"
    third = prepare()
    assert third["identity"] != first["identity"] and not third["reused"]
    assert len(exports) == 1
    assert len([u for u in calls if u.endswith(".dat")]) == 1
    assert (destination / "manifest.json").read_bytes() == manifest


def test_decode_failure_keeps_job_and_does_not_publish_partial_snapshot(tmp_path, monkeypatch):
    settings, get, _, _, _, _ = scenario(tmp_path, monkeypatch, "windows")

    def fail(*args, **kwargs):
        raise ValueError("decoder failure")

    with pytest.raises(ValueError, match="decoder failure"):
        assets.prepare_assets(
            settings, ARGUMENTS, threading.Event(), get=get, export=fail, platform="windows"
        )
    root = tmp_path / "activity-assets"
    assert list((root / "jobs").glob("*/source.json"))
    assert not list(root.glob("snapshots/*/READY"))


def test_corrupt_cached_decoder_cannot_be_authorized_by_modifying_metadata(tmp_path, monkeypatch):
    settings, get, export, _, _, version = scenario(tmp_path, monkeypatch, "windows")
    assets.prepare_assets(
        settings, ARGUMENTS, threading.Event(), get=get, export=export, platform="windows"
    )
    tool = next((tmp_path / "activity-assets/exporters").rglob("ArknightsStudioCLI.dll"))
    tool.write_bytes(b"modified executable")
    # Force a different input/export identity, not merely a different resource
    # version (which should legitimately reuse the existing exported art).
    with pytest.raises(ValueError, match="缓存被修改"):
        assets.prepare_exporter(tmp_path / "activity-assets", "windows", get, threading.Event())


def test_explicit_budgets_and_paths_checked_before_acceptance():
    for change in (
        {"bundles": ["../outside.ab"]},
        {"max_output_bytes": True},
        {"export_timeout": 0},
    ):
        with pytest.raises(ValueError):
            Submission.from_dict(
                {"request_id": "assets", "action": "assets", "arguments": {**ARGUMENTS, **change}}
            )
    assert (
        Submission.from_dict(
            {"request_id": "assets", "action": "assets", "arguments": ARGUMENTS}
        ).target
        is None
    )


def test_apk_base_bundle_uses_matching_cached_client_without_installing_or_downloading(
    tmp_path, monkeypatch
):
    from maa_planner.daemon.constants import GAME_PACKAGE
    from maa_planner.daemon.tools.apk import ApkInfo
    from tests.helpers.apk import binary_manifest

    settings, get, export, calls, _, _ = scenario(tmp_path, monkeypatch, "windows")
    settings.game_cache.mkdir()
    apk_bytes = zip_bytes(
        [
            ("AndroidManifest.xml", binary_manifest(ApkInfo(GAME_PACKAGE, 1, "2.7.91"))),
            ("assets/AB/Android/activity/base.ab", b"UnityFS fake test data"),
        ]
    )
    (settings.game_cache / "latest.apk").write_bytes(apk_bytes)
    result = assets.prepare_assets(
        settings,
        {**ARGUMENTS, "bundles": ["activity/base.ab"]},
        threading.Event(),
        get=get,
        export=export,
        platform="windows",
    )
    manifest = json.loads((Path(result["snapshot"]) / "manifest.json").read_text())
    assert manifest["source"]["inputs"][0]["source"] == "apk_cache"
    assert (settings.game_cache / "latest.apk").read_bytes() == apk_bytes
    assert not any(url.endswith(".dat") for url in calls)


def test_cancel_before_preparation_performs_no_network_or_publication(tmp_path):
    cancel = threading.Event()
    cancel.set()

    def get(*args):
        pytest.fail("cancelled scan must not request sources")

    with pytest.raises(InterruptedError):
        assets.prepare_assets(SimpleNamespace(cache_directory=tmp_path), ARGUMENTS, cancel, get=get)
    assert not (tmp_path / "activity-assets").exists()


def test_failed_snapshot_copy_is_isolated_and_retry_reuses_valid_export(tmp_path, monkeypatch):
    settings, get, export, _, exports, _ = scenario(tmp_path, monkeypatch, "windows")
    real_copy = assets.shutil.copyfile
    fail = [True]

    def copy(source, target, *args, **kwargs):
        if fail[0] and "/snapshot/" in Path(target).as_posix():
            raise OSError("disk write interrupted")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(assets.shutil, "copyfile", copy)
    with pytest.raises(OSError, match="interrupted"):
        assets.prepare_assets(
            settings, ARGUMENTS, threading.Event(), get=get, export=export, platform="windows"
        )
    assert not list((tmp_path / "activity-assets").glob("snapshots/*/READY"))
    fail[0] = False
    result = assets.prepare_assets(
        settings, ARGUMENTS, threading.Event(), get=get, export=export, platform="windows"
    )
    assert (Path(result["snapshot"]) / "READY").exists()
    assert len(exports) == 1


def test_official_index_adds_transitive_anonymous_dependencies(tmp_path, monkeypatch):
    from tests.helpers.asset_index import index_bytes

    settings, base_get, export, _, _, _ = scenario(tmp_path, monkeypatch, "windows")
    index = index_bytes([("activity/test.ab", [1]), ("anon/shared.bin", [])])
    index_zip = zip_bytes([("index.idx", index)])
    dependency = b"shared texture resource"
    dependency_zip = zip_bytes([("anon/shared.bin", dependency)])

    def get(url, limit, cancel):
        if url.endswith("hot_update_list.json"):
            response = json.loads(base_get(url, limit, cancel))
            response["abInfos"].extend(
                [
                    {
                        "name": "index.idx",
                        "abSize": len(index),
                        "totalSize": len(index_zip),
                        "md5": hashlib.md5(index).hexdigest(),
                    },
                    {
                        "name": "anon/shared.bin",
                        "abSize": len(dependency),
                        "totalSize": len(dependency_zip),
                        "md5": hashlib.md5(dependency).hexdigest(),
                    },
                ]
            )
            return json.dumps(response).encode()
        if url.endswith("index.dat"):
            return index_zip
        if url.endswith("anon_shared.dat"):
            return dependency_zip
        return base_get(url, limit, cancel)

    def export_all(dll, source, destination, log, timeout, cancel, **kwargs):
        assert (source / "anon/shared.bin").read_bytes() == dependency
        # The existing fixture checks the root AB and writes one PNG.
        export(dll, source, destination, log, timeout, cancel, **kwargs)

    result = assets.prepare_assets(
        settings,
        {**ARGUMENTS, "resolve_dependencies": True},
        threading.Event(),
        get=get,
        export=export_all,
        platform="windows",
    )
    source = json.loads((Path(result["snapshot"]) / "manifest.json").read_text())["source"]
    assert source["dependency_scope"] == "index_closure"
    assert source["requested_bundles"] == ["activity/test.ab"]
    assert [i["name"] for i in source["inputs"]] == ["activity/test.ab", "anon/shared.bin"]


def test_missing_apk_cache_uses_validated_partial_remote_members_and_reuses_body_cache(
    tmp_path, monkeypatch
):
    from maa_planner.daemon.constants import GAME_PACKAGE
    from maa_planner.daemon.tools.apk import ApkInfo
    from tests.helpers.apk import binary_manifest

    settings, get, export, _, _, _ = scenario(tmp_path, monkeypatch, "windows")
    content = zip_bytes(
        [
            ("AndroidManifest.xml", binary_manifest(ApkInfo(GAME_PACKAGE, 1, "2.7.91"))),
            ("assets/AB/Android/activity/base.ab", b"UnityFS fake test data"),
        ]
    )
    opened, reads = [], []

    class Remote(io.BytesIO):
        def __init__(self, url, **kwargs):
            super().__init__(content)
            opened.append(kwargs)
            self.validator = "same-apk"
            self.transferred = 0

    original = zipfile.ZipFile

    class Archive(original):
        def read(self, member, *args, **kwargs):
            if getattr(member, "filename", member) == "assets/AB/Android/activity/base.ab":
                reads.append(member)
            return super().read(member, *args, **kwargs)

    monkeypatch.setattr("maa_planner.daemon.tools.remote_apk.RangeFile", Remote)
    monkeypatch.setattr(
        "maa_planner.daemon.tools.game_update.resolve_latest",
        lambda *args, **kwargs: "https://ak.hycdn.cn/game.apk",
    )
    monkeypatch.setattr(assets.zipfile, "ZipFile", Archive)
    arguments = {**ARGUMENTS, "bundles": ["activity/base.ab"]}
    for _ in range(2):
        result = assets.prepare_assets(
            settings, arguments, threading.Event(), get=get, export=export, platform="windows"
        )
    assert len(reads) == 1
    assert all(c["large_reads"] for c in opened)
    assert not (settings.game_cache / "latest.apk").exists()
    source = json.loads((Path(result["snapshot"]) / "manifest.json").read_text())["source"]
    assert source["inputs"][0]["source"] == "apk_range"


def test_anonymous_short_revision_is_not_treated_as_md5_or_shared_across_manifests(
    tmp_path, monkeypatch
):
    from tests.helpers.asset_index import index_bytes

    settings, base_get, export, _, exports, version = scenario(tmp_path, monkeypatch, "windows")
    name = "anon/" + "a" * 32 + ".bin"
    data = b"anonymous UnityFS bundle"
    packed = zip_bytes([(name, data)])
    index = index_bytes([("activity/test.ab", [1]), (name, [])])
    index_zip = zip_bytes([("index.idx", index)])
    payload_calls = []

    def get(url, limit, cancel):
        if url.endswith("hot_update_list.json"):
            response = json.loads(base_get(url, limit, cancel))
            response["abInfos"].extend(
                [
                    {
                        "name": "index.idx",
                        "abSize": len(index),
                        "totalSize": len(index_zip),
                        "md5": hashlib.md5(index).hexdigest(),
                    },
                    {
                        "name": name,
                        "abSize": len(data),
                        "totalSize": len(packed),
                        "md5": "abcd",
                        "hash": "abcd",
                        "cat": 1,
                        "meta": 1,
                    },
                ]
            )
            return json.dumps(response).encode()
        if url.endswith("index.dat"):
            return index_zip
        if url.endswith("anon_" + "a" * 32 + ".dat"):
            payload_calls.append(url)
            return packed
        return base_get(url, limit, cancel)

    def prepare():
        return assets.prepare_assets(
            settings,
            {**ARGUMENTS, "resolve_dependencies": True},
            threading.Event(),
            get=get,
            export=export,
            platform="windows",
        )

    first = prepare()
    source = json.loads((Path(first["snapshot"]) / "manifest.json").read_text())["source"]
    assert source["inputs"][1]["checksum_kind"] == "opaque_revision_zip_crc"
    prepare()
    assert len(payload_calls) == 1
    version[0] = "two"
    prepare()
    assert len(payload_calls) == 2  # Same short token cannot certify another manifest.
    assert len(exports) == 1  # Actual content still matches the immutable export cache.

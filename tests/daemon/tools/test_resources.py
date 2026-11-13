from __future__ import annotations

import json
import warnings
import zipfile

import pytest

from maa_planner.daemon.tools import resources as updates


def test_resource_merge_preserves_existing_files(tmp_path, monkeypatch) -> None:
    resource = tmp_path / "resource"
    resource.mkdir()
    (resource / "version.json").write_text('{"last_updated":"2026-09-01 00:00:00.000"}')
    (resource / "keep.txt").write_text("keep")
    archive = tmp_path / "resource.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("MaaResource-main/resource/new.txt", "new")
        output.writestr(
            "MaaResource-main/resource/version.json",
            '{"last_updated":"2026-09-02 00:00:00.000"}',
        )
    monkeypatch.setattr(
        updates,
        "_get_json",
        lambda url: {"last_updated": "2026-09-02 00:00:00.000"},
    )
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", lambda request, timeout: archive.open("rb")
    )

    observed = updates.check_resource(resource, version_url="https://example.test/version.json")
    monkeypatch.setattr(
        updates,
        "_get_json",
        lambda url: pytest.fail("the batch already checked the resource version"),
    )
    assert updates.update_resource(
        resource,
        version_url="https://example.test/version.json",
        archive_url="https://example.test/resource.zip",
        archive_prefix="MaaResource-main/resource",
        observed=observed,
        cache_directory=tmp_path / "cache",
    ).available
    assert (resource / "keep.txt").read_text() == "keep"
    assert (resource / "new.txt").read_text() == "new"
    assert (
        json.loads((resource / "version.json").read_text())["last_updated"]
        == "2026-09-02 00:00:00.000"
    )


def test_resource_merge_rejects_windows_path_escape(tmp_path, monkeypatch) -> None:
    resource = tmp_path / "resource"
    resource.mkdir()
    (resource / "version.json").write_text('{"last_updated":"2026-09-01 00:00:00.000"}')
    archive = tmp_path / "resource.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "MaaResource-main/resource/version.json",
            '{"last_updated":"2026-09-02 00:00:00.000"}',
        )
        output.writestr("MaaResource-main/resource/..\\config\\gui.new.json", "bad")
    monkeypatch.setattr(
        updates,
        "_get_json",
        lambda url: {"last_updated": "2026-09-02 00:00:00.000"},
    )
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", lambda request, timeout: archive.open("rb")
    )

    with pytest.raises(ValueError, match="Unsafe archive path"):
        updates.update_resource(
            resource,
            version_url="https://example.test/version.json",
            archive_url="https://example.test/resource.zip",
            archive_prefix="MaaResource-main/resource",
            cache_directory=tmp_path / "cache",
        )
    assert not (tmp_path / "config").exists()
    assert (
        json.loads((resource / "version.json").read_text())["last_updated"]
        == "2026-09-01 00:00:00.000"
    )


def test_resource_merge_rolls_back_when_a_later_file_cannot_be_replaced(
    tmp_path, monkeypatch
) -> None:
    resource = tmp_path / "resource"
    resource.mkdir()
    (resource / "version.json").write_text('{"last_updated":"2026-09-01 00:00:00.000"}')
    (resource / "keep.txt").write_text("old")
    archive = tmp_path / "resource.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("MaaResource-main/resource/keep.txt", "new")
        output.writestr("MaaResource-main/resource/nested/new.txt", "new")
        output.writestr(
            "MaaResource-main/resource/version.json",
            '{"last_updated":"2026-09-02 00:00:00.000"}',
        )
    monkeypatch.setattr(
        updates,
        "_get_json",
        lambda url: {"last_updated": "2026-09-02 00:00:00.000"},
    )
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", lambda request, timeout: archive.open("rb")
    )
    replace = updates.os.replace
    failed = False

    def fail_version(source, destination):
        nonlocal failed
        if not failed and destination.name == "version.json":
            failed = True
            raise OSError("disk full")
        replace(source, destination)

    monkeypatch.setattr(updates.os, "replace", fail_version)

    with pytest.raises(OSError, match="disk full"):
        updates.update_resource(
            resource,
            version_url="https://example.test/version.json",
            archive_url="https://example.test/resource.zip",
            archive_prefix="MaaResource-main/resource",
            cache_directory=tmp_path / "cache",
        )

    assert (resource / "keep.txt").read_text() == "old"
    assert not (resource / "nested").exists()
    assert (
        json.loads((resource / "version.json").read_text())["last_updated"]
        == "2026-09-01 00:00:00.000"
    )


def test_resource_merge_rejects_duplicate_archive_paths(tmp_path, monkeypatch) -> None:
    resource = tmp_path / "resource"
    resource.mkdir()
    (resource / "version.json").write_text('{"last_updated":"2026-09-01 00:00:00.000"}')
    archive = tmp_path / "resource.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "MaaResource-main/resource/version.json",
            '{"last_updated":"2026-09-02 00:00:00.000"}',
        )
        output.writestr("MaaResource-main/resource/file.txt", "first")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            output.writestr("MaaResource-main/resource/file.txt", "second")
    monkeypatch.setattr(
        updates,
        "_get_json",
        lambda url: {"last_updated": "2026-09-02 00:00:00.000"},
    )
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", lambda request, timeout: archive.open("rb")
    )

    with pytest.raises(ValueError, match="Duplicate archive path"):
        updates.update_resource(
            resource,
            version_url="https://example.test/version.json",
            archive_url="https://example.test/resource.zip",
            archive_prefix="MaaResource-main/resource",
            cache_directory=tmp_path / "cache",
        )

    assert not (resource / "file.txt").exists()


def test_prepare_environment_does_not_fall_back_after_rolled_back_resource_update(
    tmp_path, monkeypatch
) -> None:
    from maa_planner.daemon import preparation
    from maa_planner.daemon.config import Settings
    from maa_planner.daemon.execution.runtime import CoreRuntime

    resource = tmp_path / "resource"
    resource.mkdir()
    version = '{"last_updated":"2026-09-01 00:00:00.000"}'
    (resource / "version.json").write_text(version)
    runtime = CoreRuntime(
        tmp_path / "core",
        tmp_path / "data",
        tmp_path / "navigation",
        tmp_path / "work",
        resource_dir=resource,
    )
    settings = Settings(
        tmp_path / "config.toml", (), core_runtime=runtime, cache_directory=tmp_path / "cache"
    )
    observed = updates.ResourceUpdate(
        updates._resource_time(json.loads(version)),
        updates._resource_time({"last_updated": "2026-09-02 00:00:00.000"}),
        True,
    )
    monkeypatch.setattr(preparation, "ensure_latest_runtime", lambda settings: runtime)
    monkeypatch.setattr(preparation, "check_resource", lambda *args, **kwargs: observed)

    def fail_update(*args, **kwargs):
        raise OSError("resource write failed")

    monkeypatch.setattr(preparation, "update_resource", fail_update)
    monkeypatch.setattr(preparation, "prepare_hot_update", lambda cache: pytest.fail("unreachable"))

    with pytest.raises(OSError, match="resource write failed"):
        preparation.prepare_environment(settings)

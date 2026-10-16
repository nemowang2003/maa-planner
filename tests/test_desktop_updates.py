from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from maa_desktop_planner import updates


def test_software_version_order(monkeypatch) -> None:
    monkeypatch.setattr(
        updates,
        "_get_json",
        lambda url: {"stable": {"version": "v6.18.0"}, "alpha": {"version": "v6.19.0-alpha.2"}},
    )
    assert updates.check_software(Path("unused"), "stable", "v6.17.9").available
    assert not updates.check_software(Path("unused"), "stable", "v6.18.0").available
    assert updates.check_software(Path("unused"), "alpha", "v6.19.0-alpha.1").available
    assert not updates.check_software(Path("unused"), "alpha", "v6.19.0").available


def test_plan_software_selects_full_package(monkeypatch) -> None:
    monkeypatch.setattr(
        updates,
        "_get_json",
        lambda url: {
            "version": "v6.18.0",
            "details": {
                "assets": [
                    {
                        "name": "MAAComponent-OTA-v6.17.0_v6.18.0-win-x64.zip",
                        "browser_download_url": "ota",
                    },
                    {"name": "MAA-v6.18.0-win-arm64.zip", "browser_download_url": "arm"},
                    {
                        "name": "MAA-v6.18.0-win-x64.zip",
                        "browser_download_url": "https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases/download/v6.18.0/MAA-v6.18.0-win-x64.zip",
                    },
                ]
            },
        },
    )
    package = updates.plan_software_package("v6.18.0", "stable", "x64")
    assert package.name == "MAA-v6.18.0-win-x64.zip"
    assert "MaaAssistantArknights/releases" in package.url


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

    assert updates.update_resource(resource).available
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
        updates.update_resource(resource)
    assert not (tmp_path / "config").exists()
    assert (
        json.loads((resource / "version.json").read_text())["last_updated"]
        == "2026-09-01 00:00:00.000"
    )

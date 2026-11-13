import io
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

from maa_planner.daemon.tools import runtime_install
from maa_planner.daemon.tools.runtime_install import _extract


@pytest.mark.parametrize(
    "filename", ["../config/gui.json", "/outside", "C:/outside", r"..\outside"]
)
def test_archive_cannot_overwrite_outside_staging(tmp_path, filename):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(filename, b"bad")
    with pytest.raises(ValueError, match="不安全路径"):
        _extract(archive, tmp_path / "stage")


def test_archive_symlink_is_rejected(tmp_path):
    archive = tmp_path / "runtime.zip"
    entry = zipfile.ZipInfo("resource")
    entry.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(entry, b"../../share")
    with pytest.raises(ValueError, match="符号链接"):
        _extract(archive, tmp_path / "stage")


def test_tar_cannot_write_outside_staging(tmp_path):
    path = tmp_path / "core.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        entry = tarfile.TarInfo("../outside")
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="不安全路径"):
        _extract(path, tmp_path / "stage")
    assert not (tmp_path / "outside").exists()


def zip_archive(destination, files):
    with zipfile.ZipFile(destination, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def tar_archive(destination, files):
    with tarfile.open(destination, "w:gz") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))


@pytest.mark.parametrize(
    "platform_name,os_name,interpreter,asset_name,core_name,write_archive",
    [
        ("windows", "nt", "python.exe", "MAA-v6.20.1-win-x64.zip", "MaaCore.dll", zip_archive),
        (
            "linux",
            "posix",
            "python",
            "MAA-v6.20.1-linux-x86_64.tar.gz",
            "libMaaCore.so",
            tar_archive,
        ),
    ],
    ids=["windows-package", "linux-package"],
)
def test_prepare_downloads_only_selected_core(
    tmp_path, monkeypatch, platform_name, os_name, interpreter, asset_name, core_name, write_archive
):
    executable = tmp_path / interpreter
    executable.write_bytes(b"host interpreter")
    monkeypatch.setattr(runtime_install, "os", SimpleNamespace(name=os_name))
    monkeypatch.setattr(runtime_install, "sys", SimpleNamespace(executable=str(executable)))
    monkeypatch.setattr(runtime_install.platform, "machine", lambda: "x86_64")
    core_asset = {"name": asset_name}
    downloaded = []

    def download(asset, destination):
        downloaded.append(asset["name"])
        write_archive(destination, {core_name: b"core", "resource/version.json": b"{}"})

    monkeypatch.setattr(runtime_install, "_download", download)
    monkeypatch.setattr(runtime_install, "probe_runtime", lambda runtime: "versions OK")
    releases = runtime_install.LatestReleases("6.20.1", {"assets": [core_asset]})
    runtime = runtime_install.prepare_runtime(
        tmp_path / "runtime",
        platform_name=platform_name,
        releases=releases,
        cache_directory=tmp_path / "cache",
    )
    assert runtime.executable == executable
    assert executable.read_bytes() == b"host interpreter"
    assert downloaded == [asset_name]
    assert runtime.windows == (platform_name == "windows")
    runtime.validate(check_hot_update=False)
    assert (runtime.data_dir / "lib" / core_name).read_bytes() == b"core"
    assert not (runtime.data_dir / "lib/resource").exists()

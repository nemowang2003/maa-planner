import subprocess
import threading
import zipfile
from contextlib import contextmanager

import pytest

from maa_planner.daemon.adapters.mumu import Instance
from maa_planner.daemon.config import Profile, Settings
from maa_planner.daemon.tools import game_update
from maa_planner.daemon.tools.apk import ApkInfo, read_apk
from tests.helpers.apk import binary_manifest

TARGET = ApkInfo("com.hypergryph.arknights", 200, "2.7.81")
OLD = ApkInfo("com.hypergryph.arknights", 200, "2.7.80")
URL = "https://ak.hycdn.cn/test/arknights-hg-2781.apk"


def test_binary_manifest_reads_package_and_versions(tmp_path):
    path = tmp_path / "app.apk"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", binary_manifest(TARGET))
    assert read_apk(path) == TARGET


def test_same_version_code_still_rejects_older_version_name():
    with pytest.raises(RuntimeError, match="降级"):
        game_update.refuse_downgrade(TARGET, OLD)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(game_update, "require_windows_host", lambda: None)
    profiles = tuple(Profile(n, tmp_path / n) for n in ("minepig", "nemo", "wyf", "zy"))
    settings = Settings(
        tmp_path / "config.toml",
        profiles,
        mumu_path=tmp_path / "MuMu",
        cache_directory=tmp_path / "cache",
    )
    observed = {p.name: TARGET for p in profiles}
    running = {p.name: True for p in profiles}
    installs, downloads, remote_reads, source_reads, boots, connects = [], [], [], [], [], []
    failed = set()
    unreadable = set()

    class FakeMuMu:
        def __init__(self, root):
            pass

        def instances(self):
            return [
                Instance(i, p.name, running[p.name], p.name if running[p.name] else None)
                for i, p in enumerate(profiles)
            ]

        def connect(self, address):
            connects.append(address)

        @contextmanager
        def running(self, instance):
            if not running[instance.name]:
                boots.append(instance.name)
            yield Instance(instance.index, instance.name, True, instance.name)

        def adb(self, address, *args, **kw):
            if args[0] == "install":
                assert args[1:] == ("-r", r"C:\cache\latest.apk")
                assert (settings.game_cache / "latest.apk").is_file()
                installs.append(address)
                if address in failed:
                    raise subprocess.CalledProcessError(1, "adb install")
                observed[address] = TARGET
                return "Success\n"
            if args[:2] == ("shell", "dumpsys"):
                if address in unreadable:
                    return "Unable to find package"
                value = observed[address]
                return f"Package [{value.package}]:\n versionCode={value.version_code} minSdk=24\n versionName={value.version_name}\n"
            return ""

    def fetch(latest_url, *, allowed_host):
        source_reads.append(URL)
        return URL

    def metadata(url, *, allowed_host):
        remote_reads.append(url)
        return TARGET

    def download(url, path, *, allowed_host):
        downloads.append(url)
        path.write_bytes(b"apk")

    def unexpected_process_check(*args):
        pytest.fail("APK 更新不应探测 MAA 进程")

    monkeypatch.setattr(game_update, "MuMu", FakeMuMu)
    monkeypatch.setattr(game_update, "resolve_latest", fetch)
    monkeypatch.setattr(game_update, "read_remote_apk", metadata)
    monkeypatch.setattr(game_update, "read_apk", lambda path: TARGET)
    monkeypatch.setattr(game_update, "download", download)
    monkeypatch.setattr(game_update, "windows_path", lambda path: r"C:\cache\latest.apk")
    monkeypatch.setattr(
        "maa_planner.daemon.adapters.processes.psutil.process_iter", unexpected_process_check
    )
    return locals()


def test_fresh_start_without_any_records_or_apk_does_not_reinstall(setup):
    env = setup
    report = game_update.update_game(env["settings"])
    assert {u.status for u in report.units} == {"up_to_date"}
    assert env["downloads"] == env["installs"] == []
    assert not (env["settings"].game_cache / "latest.apk").exists()
    assert not env["settings"].game_cache.exists()


def test_legacy_state_file_is_never_read_or_rewritten(setup):
    env = setup
    folder = env["settings"].game_cache
    folder.mkdir(parents=True)
    legacy = folder / "state.json"
    legacy.write_text("obsolete invalid JSON")
    env["observed"]["nemo"] = OLD
    report = game_update.update_game(env["settings"])
    assert env["installs"] == ["nemo"]
    assert env["downloads"] == [URL]
    assert report.units[1].status == "updated"
    assert legacy.read_text() == "obsolete invalid JSON"
    assert {p.name for p in folder.iterdir()} == {"state.json"}


def test_different_url_same_metadata_is_current_without_download(setup, monkeypatch):
    env = setup
    game_update.update_game(env["settings"])
    other = "https://ak.hycdn.cn/new-location/identical-version.apk"
    monkeypatch.setattr(game_update, "resolve_latest", lambda latest_url, **kw: other)
    report = game_update.update_game(env["settings"])
    assert report.latest.url == other
    assert {u.status for u in report.units} == {"up_to_date"}
    assert env["installs"] == env["downloads"] == []


def test_retries_observe_real_versions_and_download_once_per_update(setup):
    env = setup
    for name in env["observed"]:
        env["observed"][name] = OLD
    env["failed"].add("wyf")
    with pytest.raises(RuntimeError, match="wyf"):
        game_update.update_game(env["settings"])
    assert env["installs"] == ["minepig", "nemo", "wyf", "zy"]
    env["failed"].clear()
    report = game_update.update_game(env["settings"])
    assert env["installs"] == ["minepig", "nemo", "wyf", "zy", "wyf"]
    assert env["downloads"] == [URL, URL]
    assert env["source_reads"] == env["remote_reads"] == [URL, URL]
    assert [u.status for u in report.units] == ["up_to_date", "up_to_date", "updated", "up_to_date"]


def test_check_only_reads_running_instances_and_never_boots_or_downloads(setup):
    env = setup
    env["running"]["wyf"] = False
    env["observed"]["nemo"] = OLD
    report = game_update.check_game_updates(env["settings"])
    assert [u.status for u in report.units] == [
        "up_to_date",
        "update_required",
        "unavailable",
        "up_to_date",
    ]
    assert env["boots"] == env["downloads"] == env["installs"] == []


def test_outdated_units_install_without_maa_process_check(setup):
    env = setup
    env["observed"]["nemo"] = OLD
    env["observed"]["wyf"] = OLD
    env["running"]["nemo"] = False
    report = game_update.update_game(env["settings"])
    assert env["installs"] == ["nemo", "wyf"]
    assert env["boots"] == ["nemo"]
    assert report.units[1].status == report.units[2].status == "updated"
    assert env["downloads"] == [URL]


def test_already_current_client_is_not_reinstalled(setup):
    env = setup
    report = game_update.update_game(env["settings"])
    assert report.units[1].status == "up_to_date"
    assert env["downloads"] == env["installs"] == []


def test_unreadable_metadata_is_unknown_not_update_required(setup):
    env = setup
    env["unreadable"].add("nemo")
    report = game_update.check_game_updates(env["settings"])
    assert report.units[1].status == "unavailable"
    assert env["downloads"] == env["installs"] == []


def test_failed_remote_metadata_does_not_trigger_full_download(setup, monkeypatch):
    env = setup

    def fail(url, *, allowed_host):
        raise ValueError("Range unsupported")

    monkeypatch.setattr(game_update, "read_remote_apk", fail)
    with pytest.raises(ValueError, match="Range"):
        game_update.update_game(env["settings"])
    assert env["downloads"] == env["installs"] == []


def test_older_remote_release_never_downgrades_local(setup):
    env = setup
    env["observed"]["nemo"] = ApkInfo("com.hypergryph.arknights", 200, "2.7.82")
    report = game_update.update_game(env["settings"])
    assert report.units[1].status == "ahead"
    assert env["downloads"] == env["installs"] == []


def test_full_download_metadata_must_match_remote_probe(setup, monkeypatch):
    env = setup
    for name in env["observed"]:
        env["observed"][name] = OLD
    monkeypatch.setattr(game_update, "read_apk", lambda path: OLD)
    with pytest.raises(RuntimeError, match="metadata"):
        game_update.update_game(env["settings"])
    assert env["downloads"] == [URL]
    assert env["installs"] == []
    assert not (env["settings"].game_cache / "latest.apk").exists()


def test_check_cancellation_does_not_query_instances(setup):
    env = setup
    cancelled = threading.Event()
    cancelled.set()
    report = game_update.check_game_updates(env["settings"], cancel=cancelled)
    assert {u.status for u in report.units} == {"unavailable"}
    assert env["connects"] == []


def test_existing_apk_is_reused_by_manifest_after_url_changes(setup, monkeypatch):
    env = setup
    folder = env["settings"].game_cache
    folder.mkdir(parents=True)
    (folder / "latest.apk").write_bytes(b"cached apk")
    env["observed"]["nemo"] = OLD
    monkeypatch.setattr(
        game_update, "resolve_latest", lambda latest_url, **kw: "https://ak.hycdn.cn/new/app.apk"
    )
    game_update.update_game(env["settings"])
    assert env["downloads"] == []
    assert env["installs"] == ["nemo"]
    assert not list(folder.iterdir())


@pytest.mark.parametrize("fail", [False, True])
def test_update_removes_apk_after_all_accounts_even_on_install_failure(setup, fail):
    env = setup
    for name in env["observed"]:
        env["observed"][name] = OLD
    if fail:
        env["failed"].add("nemo")
        with pytest.raises(RuntimeError, match="nemo"):
            game_update.update_game(env["settings"])
    else:
        game_update.update_game(env["settings"])
    assert env["installs"] == ["minepig", "nemo", "wyf", "zy"]
    assert env["downloads"] == [URL]
    assert not (env["settings"].game_cache / "latest.apk").exists()


def test_busy_artifact_lock_never_deletes_another_updates_package(setup):
    from maa_planner.daemon.tools.locks import BusyError, acquire

    folder = setup["settings"].game_cache
    folder.mkdir(parents=True)
    apk = folder / "latest.apk"
    apk.write_bytes(b"in use")
    with acquire([game_update.cache_resource(folder)]):
        with pytest.raises(BusyError):
            game_update.update_game(setup["settings"])
    assert apk.read_bytes() == b"in use"


def test_automatic_updates_defer_closed_emulators_and_mark_install_intent_first(setup):
    env = setup
    env["observed"]["nemo"] = OLD
    env["observed"]["wyf"] = OLD
    env["running"]["nemo"] = False
    pending = []

    def intent(profile, metadata):
        assert profile.name not in env["installs"]
        pending.append((profile.name, metadata))

    report = game_update.update_game(
        env["settings"], allow_emulator_start=False, on_install=intent, raise_on_failure=False
    )
    assert env["boots"] == []
    assert env["installs"] == ["wyf"]
    assert report.units[1].status == "deferred"
    assert pending == [("wyf", TARGET)]

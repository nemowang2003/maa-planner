import os
from pathlib import Path

from maa_planner.daemon.config import Settings, default_config_file


def test_default_paths_and_runtime_platform_follow_native_host(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    if os.name == "nt":
        root, platform = tmp_path / "local", "windows"
    else:
        root, platform = tmp_path / ".config", "linux"
    assert default_config_file() == root / "maa-planner/config.toml"
    assert Settings(tmp_path / "config.toml", ()).runtime_platform == platform


def test_native_defaults_share_one_application_root(tmp_path, monkeypatch):
    from maa_planner.daemon.tools.locks import state_directory
    from maa_planner.endpoint import default_endpoint

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    settings = Settings(tmp_path / "config.toml", ())
    assert settings.runtime_directory.parts[-2:] == ("maa-planner", "runtimes")
    assert state_directory().parts[-2:] == ("maa-planner", "daemon")
    assert default_endpoint().parent == state_directory().parent
    if os.name == "nt":
        root = Path(os.environ["LOCALAPPDATA"]) / "maa-planner"
        assert settings.runtime_directory == root / "runtimes"
        assert settings.cache_directory == root / "Cache"
        assert default_endpoint() == root / "control.json"

from platformdirs import windows
from platformdirs.windows import Windows

from maa_planner import endpoint as protocol
from maa_planner.daemon import config
from maa_planner.daemon.tools import locks


def test_windows_install_cache_and_control_paths_have_one_app_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(windows, "get_win_folder", lambda _: str(tmp_path))
    for kind in ("data", "cache", "state"):
        attribute = f"user_{kind}_path"

        def path(name, *, _attribute=attribute, **kwargs):
            return getattr(Windows(name, **kwargs), _attribute)

        if kind in {"data", "cache"}:
            monkeypatch.setattr(config, attribute, path)
        else:
            monkeypatch.setattr(locks, attribute, path)
            monkeypatch.setattr("platformdirs.user_state_path", path)

    root = tmp_path / "maa-planner"
    direct = config.Settings(tmp_path / "config.toml", ())
    parsed = config.settings_from_bytes(b'[[profiles]]\nname="test"\n', direct.config_file)
    for settings in (direct, parsed):
        assert settings.runtime_directory == root / "runtimes"
        assert settings.cache_directory == root / "Cache"
    assert locks.state_directory() == root / "daemon"
    assert protocol.default_endpoint() == root / "control.json"
    overridden = config.settings_from_bytes(
        b'[maa_core]\ndirectory="custom-core"\n[[profiles]]\nname="test"\n', direct.config_file
    )
    assert overridden.runtime_directory == tmp_path / "custom-core"


def test_state_override_keeps_daemon_and_clients_in_the_same_root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert locks.state_directory() == tmp_path / "maa-planner/daemon"
    assert protocol.default_endpoint() == tmp_path / "maa-planner/control.json"

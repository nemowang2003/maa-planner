import json
import subprocess
import sys

import pytest
from platformdirs import windows
from platformdirs.unix import Unix
from platformdirs.windows import Windows

from maa_planner.interfaces.wecom import config as module
from maa_planner.interfaces.wecom.config import WeComConfig
from tests.helpers.wecom import config_document, config_file


@pytest.mark.parametrize(
    "platform,env,expected",
    [
        (Unix, "XDG_CONFIG_HOME", "maa-planner-wecom-client/wecom.json"),
        (Windows, "APPDATA", "maa-planner-wecom-client/wecom.json"),
    ],
)
def test_config_path_uses_native_user_config_convention(
    tmp_path, monkeypatch, platform, env, expected
):
    monkeypatch.setenv(env, str(tmp_path))
    monkeypatch.setattr(windows, "get_win_folder", lambda _: str(tmp_path))
    monkeypatch.setattr(
        module, "user_config_path", lambda name, **kwargs: platform(name, **kwargs).user_config_path
    )
    assert module.default_config_file().as_posix().endswith(expected)
    assert str(tmp_path) in str(module.default_config_file())


@pytest.mark.parametrize(
    "key,value",
    [
        ("secret", ""),
        ("admins", ["admin", "admin"]),
        ("routes", {"nemo": ["alice", "alice"]}),
        ("routes", []),
        ("routes", {"admin": ["alice"]}),
        ("admins", "admin"),
    ],
)
def test_bad_configuration_never_echoes_secret(tmp_path, key, value):
    document = config_document()
    document[key] = value
    path = tmp_path / "wecom.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError) as error:
        WeComConfig.load(path)
    assert "test-secret" not in str(error.value)


def test_config_repr_and_offline_cli_do_not_expose_credentials(tmp_path):
    path = config_file(tmp_path)
    assert "test-secret" not in repr(WeComConfig.load(path))
    assert "test-bot" not in repr(WeComConfig.load(path))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from maa_planner.interfaces.wecom import main; main()",
            "--config",
            str(path),
            "--check",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test-secret" not in result.stdout + result.stderr
    assert "未建立任何连接" in result.stdout


def test_allowed_users_are_derived_without_adding_admins_to_account_routes(tmp_path):
    document = config_document()
    document["routes"] = {"nemo": ["alice"], "wyf": ["bob", "alice"], "disabled": []}
    path = tmp_path / "wecom.json"
    path.write_text(json.dumps(document))
    config = WeComConfig.load(path)
    assert config.allowed_users == frozenset({"admin", "alice", "bob"})
    assert config.routes["nemo"] == frozenset({"alice"})
    assert config.routes["wyf"] == frozenset({"alice", "bob"})
    assert config.profiles("admin") == frozenset()
    assert config.profiles("alice") == frozenset({"nemo", "wyf"})


def test_empty_admins_and_routes_mean_no_authorized_users(tmp_path):
    document = config_document()
    document.update(admins=[], routes={})
    path = tmp_path / "wecom.json"
    path.write_text(json.dumps(document))
    assert not WeComConfig.load(path).allowed_users

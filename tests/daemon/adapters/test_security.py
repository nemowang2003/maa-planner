from pathlib import Path

import pytest

from maa_planner.daemon.adapters.security import write_private


@pytest.mark.parametrize("location", ["directory", "ancestor"])
def test_endpoint_credentials_reject_reparse_directories_before_any_write(
    tmp_path, monkeypatch, location
):
    directory = tmp_path / "parent" / "private"
    directory.mkdir(parents=True, mode=0o700)
    reparse = directory if location == "directory" else directory.parent
    monkeypatch.setattr(Path, "is_junction", lambda path: path == reparse)
    endpoint = directory / "control.json"
    with pytest.raises(PermissionError, match="重解析|链接|junction"):
        write_private(endpoint, b"credential")
    assert not endpoint.exists() and not list(directory.glob("control.json.*"))

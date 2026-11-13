"""Business tests never operate on the developer's real cache or runtime installs."""

import pytest

from maa_planner.daemon import config


@pytest.fixture(autouse=True)
def isolated_managed_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "user_cache_path", lambda *args, **kwargs: tmp_path / "user-cache")
    monkeypatch.setattr(config, "user_data_path", lambda *args, **kwargs: tmp_path / "user-data")

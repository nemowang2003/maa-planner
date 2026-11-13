from __future__ import annotations

import pytest

from maa_planner.daemon.config import Profile
from maa_planner.daemon.planning.skland import _load_auth_info, _select_character


def test_missing_auth_has_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="skland auth add main"):
        _load_auth_info(tmp_path / "main.json")


def test_character_selection_matches_name_and_reports_available_characters():
    characters = [{"uid": "1", "nickName": "Doctor"}, {"uid": "2", "nickName": "Other"}]
    assert (
        _select_character(Profile("main", auth_name="main", character_name="doctor"), characters)
        == characters[0]
    )
    with pytest.raises(ValueError, match="Doctor, Other"):
        _select_character(Profile("main", auth_name="main", character_name="missing"), characters)


def test_character_collection_requests_only_arknights_bindings(monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from maa_planner.daemon.config import Settings
    from maa_planner.daemon.planning import skland

    binding = {"uid": "1", "nickName": "Doctor"}
    api = Mock(binding_list=AsyncMock(return_value=[binding]))
    auth = Mock(full_auth=AsyncMock(return_value=api))
    info = object()
    loader = Mock(return_value=Mock(full_load=AsyncMock(return_value=info)))
    monkeypatch.setattr(skland, "_load_auth_info", lambda path: auth)
    monkeypatch.setattr(skland, "CharacterInfoLoader", loader)
    result = asyncio.run(
        skland.collect_character_info(Settings(tmp_path / "config.toml", ()), Profile("main"))
    )
    assert result == (info, "Doctor")
    api.binding_list.assert_awaited_once_with(app_code="arknights")
    loader.assert_called_once_with("main", api, binding)

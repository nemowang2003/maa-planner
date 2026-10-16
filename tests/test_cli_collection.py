from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maa_cli_planner import cli
from maa_cli_planner.config import PlannerUser, build_example_config
from maa_cli_planner.skland_client import PlayerContext, _load_auth_info, _select_character


@pytest.mark.parametrize("dry_run", [True, False])
def test_empty_planner_preserves_existing_outputs(tmp_path, capsys, dry_run):
    config = tmp_path / "config.toml"
    config.write_text(build_example_config(), encoding="utf-8")
    context = MagicMock(spec=PlayerContext)
    context.user_name = "main"
    context.character_name = "Doctor"
    with (
        patch.object(cli, "collect_player_context", AsyncMock(return_value=context)) as collect,
        patch.object(cli, "summarize_context_text", return_value='{"sanity": 120}'),
        patch.object(cli, "write_plan") as write_plan,
        patch.object(cli, "write_profile") as write_profile,
    ):
        asyncio.run(cli.run_plan(config, dry_run=dry_run))
    collect.assert_awaited_once()
    write_plan.assert_not_called()
    write_profile.assert_not_called()
    output = capsys.readouterr().out
    assert "尚未实现" in output
    assert ('"sanity": 120' in output) if dry_run else ("未写入任何任务文件" in output)


def test_missing_auth_has_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="skland auth add main"):
        _load_auth_info(tmp_path / "main.json")


def test_character_selection_matches_name_and_reports_available_characters():
    characters = [{"uid": "1", "nickName": "Doctor"}, {"uid": "2", "nickName": "Other"}]
    assert _select_character(PlannerUser("main", "main", "doctor"), characters) == characters[0]
    with pytest.raises(ValueError, match="Doctor, Other"):
        _select_character(PlannerUser("main", "main", "missing"), characters)

import asyncio
from dataclasses import replace

import pytest

from maa_planner.daemon import preparation
from maa_planner.daemon.config import Settings
from maa_planner.daemon.tools.resources import ResourceUpdate
from tests.helpers.activity import empty_activity


@pytest.mark.parametrize("update", [False, True])
def test_preparation_observes_checked_versions_updates_and_hot_snapshot(
    tmp_path, monkeypatch, update
):
    settings = Settings(tmp_path / "config.toml", (), runtime_directory=tmp_path / "runtime")
    runtime = replace(settings.runtime_template(), expected_core_version="6.21.4")
    activity = empty_activity()
    current = activity.fetched_at
    status = ResourceUpdate(current, current, update)
    from types import SimpleNamespace

    monkeypatch.setattr(preparation, "ensure_latest_runtime", lambda settings: runtime)
    monkeypatch.setattr(preparation, "check_resource", lambda *a, **kw: status)
    calls = []
    monkeypatch.setattr(preparation, "update_resource", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(
        preparation,
        "prepare_hot_update",
        lambda cache: SimpleNamespace(directory=tmp_path / "hash", activity=activity),
    )
    monkeypatch.setattr(type(runtime), "validate", lambda *a, **kw: None)
    result = preparation.prepare_environment(settings)
    assert result.observations["core"]["version"] == "6.21.4"
    assert result.observations["resources"]["updated"] == update
    assert len(calls) == int(update)
    assert result.observations["hot_update"]["fingerprint"] == "hash"
    assert result.runtime.navigation == tmp_path / "hash"


def test_failure_reports_stage_without_losing_original_exception(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "config.toml", ())

    def fail(settings):
        raise OSError("offline")

    monkeypatch.setattr(preparation, "ensure_latest_runtime", fail)
    with pytest.raises(OSError, match="offline") as error:
        preparation.prepare_environment(settings)
    assert getattr(error.value, "preparation_stage") == "core"


def test_catalog_observation_includes_immutable_commit(tmp_path, monkeypatch):
    from maa_planner.daemon.tools import stage_catalog

    monkeypatch.setattr(
        stage_catalog,
        "load_stage_catalog",
        lambda cache: {"commit": "a" * 40, "source": "fixed/source"},
    )
    reports = []
    unit = preparation.Preparation(
        Settings(tmp_path / "config.toml", ()),
        observer=lambda stage, report: reports.append((stage, report)),
    )
    asyncio.run(unit.stage_catalog())
    assert reports == [
        ("catalog", {"status": "succeeded", "commit": "a" * 40, "source": "fixed/source"})
    ]


def test_new_batch_refreshes_public_snapshot_but_pins_active_core_and_resources(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    settings = Settings(tmp_path / "config.toml", ())
    old = preparation.PreparedEnvironment(settings.runtime_template(), empty_activity())
    fresh = empty_activity()
    monkeypatch.setattr(
        preparation,
        "ensure_latest_runtime",
        lambda settings: pytest.fail("active Core must stay pinned"),
    )
    monkeypatch.setattr(
        preparation,
        "check_resource",
        lambda *a, **kw: pytest.fail("active resource must not be updated"),
    )
    monkeypatch.setattr(
        preparation,
        "prepare_hot_update",
        lambda cache: SimpleNamespace(directory=tmp_path / "new-hot", activity=fresh),
    )
    monkeypatch.setattr(type(old.runtime), "validate", lambda *a, **kw: None)
    result = preparation.prepare_environment(settings, pinned=old)
    assert result.activity is fresh and old.activity is not fresh
    assert result.runtime.data_dir == old.runtime.data_dir
    assert result.runtime.navigation == tmp_path / "new-hot"
    assert result.observations["core"]["reused"]
    assert result.observations["resources"]["reused"]


def test_new_unit_shares_parent_runtime_and_refreshes_once_for_all_its_members(tmp_path):
    settings = Settings(tmp_path / "config.toml", ())
    calls = []

    async def load(settings, *, pinned=None):
        calls.append(pinned)
        return preparation.PreparedEnvironment(settings.runtime_template(), empty_activity())

    async def scenario():
        first = preparation.Preparation(settings)
        old = await first.get(load)
        second = preparation.Preparation(settings, shared=first)
        values = await asyncio.gather(*(second.get(load) for _ in range(4)))
        assert all(value is values[0] for value in values)
        assert values[0] is not old
        assert calls == [None, old]
        assert second.shared is None

    asyncio.run(scenario())

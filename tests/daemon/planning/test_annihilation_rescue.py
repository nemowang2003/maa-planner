import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.tasks import CoreTasks
from maa_planner.daemon.planning.annihilation_rescue import prepare_rescue, validate_context


def test_preparation_freezes_rotation_and_roster_without_search_or_script_attachments(monkeypatch):
    info = SimpleNamespace(
        cultivate={"characters": [{"id": "char_1"}]},
        player_info={"charInfoMap": {"one": {"id": "char_1", "name": "拥有"}}},
    )

    async def load():
        return "a" * 40, (
            {
                "stage_id": "camp_r_34",
                "name": "灰石剧场",
                "available_from": 1,
                "available_until": 10000,
            },
        )

    monkeypatch.setattr(
        "maa_planner.daemon.planning.annihilation_rescue.search_scripts",
        lambda *args: pytest.fail("preparation must not search"),
    )
    context, files, nodes = asyncio.run(
        prepare_rescue(None, None, info, load, threading.Event(), now=100)
    )
    assert context["owned_operators"] == ["拥有"]
    assert context["candidates"][0]["stage_id"] == "camp_r_34"
    assert not files
    validate_context(context)
    CoreTasks(
        json.dumps(
            {
                "tasks": [{"type": "Fight", "params": {"stage": "Annihilation"}}],
                "annihilation_copilot": context,
                "resource_tasks": nodes,
            }
        ),
        json.dumps(files),
    )


def test_bad_catalog_and_ambiguous_map_names_rejected():
    with pytest.raises(ValueError):
        validate_context({"candidates": [], "catalog_commit": "unknown", "prepared_at": 100})


def test_public_search_uses_real_query_filters_and_validates_full_script_identity(
    tmp_path, monkeypatch
):
    from maa_planner.daemon.planning.annihilation_rescue import current_candidates, search_scripts

    calls = []

    class Source:
        def __init__(self, cancel, cache):
            pass

        def get(self, url):
            calls.append(url)
            if "/query?" in url:
                assert "levelKeyword=camp_r_34" in url and "uploaderId=7661" in url
                return {
                    "data": [
                        {
                            "id": 7,
                            "uploader_id": "7661",
                            "available": True,
                            "content": json.dumps({"stage_name": "camp_r_34", "opers": []}),
                        }
                    ],
                    "has_next": False,
                }
            return {"content": json.dumps({"stage_name": "camp_r_34", "actions": [], "opers": []})}

    monkeypatch.setattr("maa_planner.daemon.planning.annihilation_rescue._Source", Source)
    catalog = {"stage_table": {"stages": {"camp_r_34": {"name": "剿灭"}}}}
    campaign = {
        "campaignGroups": {"group": {"startTs": 1, "endTs": 200, "activeCamps": ["camp_01"]}},
        "campaignRotateStageOpenTimes": [{"stageId": "camp_r_34", "startTs": 1, "endTs": 200}],
    }
    entry = current_candidates(catalog, campaign, now=100)[0]
    assert not calls
    result = search_scripts(tmp_path, entry, threading.Event())
    assert result[0][0] == 7
    assert len(calls) == 2
    assert entry["available_until"] == 201


def test_batch_public_candidates_shared_and_one_account_cancel_does_not_cancel_others(
    tmp_path, monkeypatch
):
    from maa_planner.daemon.config import Settings
    from maa_planner.daemon.preparation import Preparation

    entered, release = threading.Event(), threading.Event()
    calls = []

    async def catalog(self):
        return {"commit": "a" * 40}

    def public(catalog, campaign, *, now):
        calls.append(now)
        entered.set()
        assert release.wait(2)
        return ({"stage_id": "camp_01"},)

    monkeypatch.setattr(Preparation, "stage_catalog", catalog)
    monkeypatch.setattr(
        "maa_planner.daemon.tools.stage_catalog.load_campaign_table", lambda *args: {}
    )
    monkeypatch.setattr(
        "maa_planner.daemon.planning.annihilation_rescue.current_candidates", public
    )

    async def scenario():
        preparation = Preparation(Settings(tmp_path / "config.toml", ()))
        first_cancel = threading.Event()
        a = asyncio.create_task(preparation.annihilation_candidates(first_cancel, now=100))
        assert await asyncio.to_thread(entered.wait, 2)
        b = asyncio.create_task(preparation.annihilation_candidates(threading.Event(), now=100))
        first_cancel.set()
        release.set()
        outcomes = await asyncio.gather(a, b, return_exceptions=True)
        assert isinstance(outcomes[0], InterruptedError)
        assert outcomes[1] == ("a" * 40, ({"stage_id": "camp_01"},))

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("identifier", ["camp_01", "camp_02", "camp_03"])
def test_permanent_maps_cannot_be_rescue_candidates(identifier):
    with pytest.raises(ValueError, match="轮换"):
        validate_context(
            {
                "catalog_commit": "a" * 40,
                "prepared_at": 100,
                "owned_operators": [],
                "candidates": [
                    {
                        "stage_id": identifier,
                        "name": "常驻",
                        "available_from": 1,
                        "available_until": 1000,
                    }
                ],
            }
        )


def test_ambiguous_or_missing_current_rotation_fails_without_falling_back_to_permanent():
    from maa_planner.daemon.planning.annihilation_rescue import current_candidates

    campaign = {
        "campaignGroups": {"all": {"startTs": 1, "endTs": 200, "activeCamps": ["camp_01"]}},
        "campaignRotateStageOpenTimes": [],
    }
    with pytest.raises(ValueError, match="唯一"):
        current_candidates({}, campaign, now=100)
    campaign["campaignRotateStageOpenTimes"] = [
        {"stageId": f"camp_r_{n}", "startTs": 1, "endTs": 200} for n in (1, 2)
    ]
    with pytest.raises(ValueError, match="唯一"):
        current_candidates({}, campaign, now=100)

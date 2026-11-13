import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from maa_planner.daemon.execution.copilot import _Source
from maa_planner.daemon.planning.auto_copilot import (
    current_activity,
    observe_operators,
    query_sets,
    resolve_formation,
    select_sets,
    shared_formation,
)
from maa_planner.daemon.planning.schedule import parse_schedules
from tests.helpers.clearance import NOW, activity, catalog


def script(stage, *names, modes=3):
    return {
        "stage_name": stage,
        "difficulty": modes,
        "actions": [],
        "opers": [{"name": name, "skill": 1} for name in names],
        "groups": [],
    }


def test_roster_uses_owned_ids_not_global_names_and_distinguishes_amiya_forms():
    player = {
        "charInfoMap": {
            "a": {"id": "a", "name": "甲"},
            "b": {"id": "b", "name": "乙"},
            "c": {"id": "char_1001_amiya2", "name": "阿米娅"},
        }
    }
    assert observe_operators({"characters": [{"id": "a"}, {"id": "char_1001_amiya2"}]}, player) == {
        "甲",
        "阿米娅(近卫)",
    }
    assert observe_operators({"characters": []}, player) == set()


@pytest.mark.parametrize(
    "cultivate,player",
    [
        ({}, {}),
        ({"characters": []}, {}),
        ({"characters": [{"id": "missing"}]}, {"charInfoMap": {}}),
        ({"characters": [None]}, {"charInfoMap": {}}),
    ],
)
def test_missing_or_invalid_roster_never_becomes_zero_owned(cultivate, player):
    with pytest.raises(ValueError, match="Skland"):
        observe_operators(cultivate, player)


def test_group_matching_uses_alternatives_without_reusing_one_operator():
    source = {
        "opers": [],
        "groups": [
            {"name": "group1", "opers": [{"name": "甲"}, {"name": "乙"}]},
            {"name": "group2", "opers": [{"name": "甲"}]},
        ],
        "actions": [{"name": "group1"}],
    }
    fixed, missing = resolve_formation(source, frozenset({"甲", "乙"}), 0)
    assert missing == []
    assert fixed["groups"][0]["opers"] == [{"name": "乙"}]
    assert fixed["groups"][1]["opers"] == [{"name": "甲"}]
    assert len(source["groups"][0]["opers"]) == 2
    assert fixed["actions"] == source["actions"]
    borrowed, missing = resolve_formation(source, frozenset({"甲"}), 1)
    assert missing == ["乙"]
    assert borrowed["groups"][0]["opers"] == [{"name": "乙"}]
    impossible = {"opers": [{"name": "甲"}, {"name": "甲"}]}
    with pytest.raises(ValueError):
        resolve_formation(impossible, frozenset({"甲"}), 1)  # Cannot borrow a second copy of 甲.


@pytest.mark.parametrize(
    "names,allowance,valid",
    [
        (("甲",), 0, True),
        (("甲", "乙"), 0, False),
        (("甲", "乙"), 1, True),
        (("甲", "乙", "丙"), 1, False),
    ],
)
def test_ownership_allowances(names, allowance, valid):
    source = script("act_01", *names)
    if valid:
        _, missing = resolve_formation(source, frozenset({"甲"}), allowance)
        assert missing == list(names[1:])
    else:
        with pytest.raises(ValueError):
            resolve_formation(source, frozenset({"甲"}), allowance)


@pytest.fixture
def search_source(tmp_path, monkeypatch):
    _, data = catalog()
    stages = data["stage_table"]["stages"]
    # One regular and one EX stage, each with a normal/raid pair.
    for identifier in ("act_01", "act_01#f#"):
        stages[identifier]["code"] = "EV-1"
    rows = [
        {"id": 30, "name": "EV EX一摆到底", "creator_id": "7661"},
        {"id": 20, "name": "EV 普通一摆到底", "creator_id": "7661"},
        {"id": 40, "name": "EV 普通全关卡", "creator_id": "7661"},
    ]
    jobs = {
        30: ((3, script("act_02", "甲", "乙")),),
        20: ((2, script("act_01", "乙")),),
        40: ((4, script("act_01", "甲")),),
    }
    reads = []

    def get(self, url):
        reads.append(url)
        if "/set/get" in url:
            return {"copilot_ids": [i for i, _ in jobs[int(url.rsplit("=", 1)[1])]]}
        key = int(url.rsplit("/", 1)[1])
        return {
            "content": json.dumps(
                next(s for entries in jobs.values() for i, s in entries if i == key)
            )
        }

    monkeypatch.setattr(_Source, "get", get)
    settings = SimpleNamespace(cache_directory=tmp_path)
    return settings, data, rows, jobs, reads


def select(source, *, owned=("甲",), completed=(), now=NOW):
    settings, data, rows, _, _ = source
    return select_sets(
        settings,
        activity(),
        data,
        frozenset(owned),
        frozenset(completed),
        rows,
        threading.Event(),
        now=now,
    )


def test_title_ev_cannot_make_ex_set_qualify_for_regular_region_and_fallback_is_whole_set(
    search_source,
):
    plan, files, _ = select(search_source)
    assert [(v["region"], v["set_id"]) for v in plan["selection"]] == [("normal", 40), ("EX", 30)]
    assert {t["script_id"] for t in plan["targets"] if t["code"] == "EV-1"} == {4}
    assert all(
        t["support_unit_usage"] == (0 if t["code"] == "EV-1" else 1) for t in plan["targets"]
    )
    assert set(files) == {"clearance-4.json", "clearance-3.json"}
    assert plan["catalog_complete"] is True


def test_usable_one_placement_set_beats_newer_generic_set(search_source):
    plan, _, _ = select(search_source, owned=("甲", "乙"))
    assert plan["selection"][0]["set_id"] == 20


def test_one_placement_unions_all_collection_stages_even_when_a_stage_is_completed(search_source):
    _, data, rows, jobs, _ = search_source
    for value in data["stage_table"]["stages"].values():
        value["code"] = "EV-EX-1" if value["stageId"].startswith("act_01") else "EV-EX-2"
    rows[:] = rows[:1]
    jobs[30] = ((2, script("act_01", "甲")), (3, script("act_02", "乙")))
    plan, files, _ = select(
        search_source,
        owned=("甲", "乙"),
        completed=[("act_01", False), ("act_01", True)],
    )
    assert {oper["name"] for oper in files["clearance-3.json"]["opers"]} == {"甲", "乙"}
    assert all(target["formation_group"] == "set-30" for target in plan["targets"])
    assert all(target["support_unit_usage"] == 0 for target in plan["targets"])
    assert plan["selection"][0]["formation_reuse"] is True
    assert jobs[30][1][1]["opers"] == [{"name": "乙", "skill": 1}]


@pytest.mark.parametrize("name", ["EV EX一摆到底", "EV EX全关卡"])
def test_only_entirely_owned_one_placement_collections_enable_reuse(search_source, name):
    search_source[2][0]["name"] = name
    plan, _, _ = select(search_source, owned=("甲", "乙"))
    ex = next(target for target in plan["targets"] if "EX" in target["code"])
    assert ("formation_group" in ex) == ("一摆到底" in name)
    borrowed_plan, _, _ = select(search_source)
    assert not any("formation_group" in t for t in borrowed_plan["targets"])


def test_union_preserves_group_aliases_actions_and_stage_specific_skill_usage():
    a = script("act_01", "甲")
    a["opers"][0].update(skill_usage=1, requirements={"module": -1, "elite": 1, "level": 80})
    a["actions"] = [{"name": "甲", "type": "Deploy"}]
    b = script("act_02", "乙")
    b["groups"] = [
        {
            "name": "group",
            "opers": [
                {
                    "name": "甲",
                    "skill": 1,
                    "skill_usage": 2,
                    "requirements": {"module": 1, "elite": 2, "level": 60},
                }
            ],
        }
    ]
    b["actions"] = [{"name": "group", "type": "Deploy"}]
    original = json.dumps([a, b], ensure_ascii=False)
    fixed, reason = shared_formation(((1, a), (2, b)), frozenset({"甲", "乙"}))
    assert fixed is not None and reason is None
    assert fixed[1]["opers"][0]["skill_usage"] == 1
    assert fixed[2]["groups"][0]["opers"][0]["skill_usage"] == 2
    assert fixed[1]["opers"][0]["requirements"] == {"module": 1, "elite": 2, "level": 60}
    assert [o["name"] for o in fixed[1]["opers"]] == ["甲", "乙"]
    assert [o["name"] for o in fixed[2]["opers"]] == ["乙"]  # No duplicate of the group member.
    assert fixed[1]["actions"] == a["actions"] and fixed[2]["actions"] == b["actions"]
    assert json.dumps([a, b], ensure_ascii=False) == original


@pytest.mark.parametrize("conflict", ["skill", "module", "size", "unowned", "group_alias"])
def test_union_falls_back_when_one_unchanging_formation_cannot_satisfy_collection(conflict):
    a, b = script("act_01", "甲"), script("act_02", "甲")
    owned = frozenset({"甲"})
    if conflict == "skill":
        b["opers"][0]["skill"] = 2
    elif conflict == "module":
        a["opers"][0]["requirements"] = {"module": 1}
        b["opers"][0]["requirements"] = {"module": 2}
    elif conflict == "size":
        a = script("act_01", *(str(i) for i in range(7)))
        b = script("act_02", *(str(i) for i in range(7, 13)))
        owned = frozenset(str(i) for i in range(13))
    elif conflict == "group_alias":
        a["opers"] = []
        a["groups"] = [{"name": "乙", "opers": [{"name": "甲"}]}]
        b = script("act_02", "乙")
        owned = frozenset({"甲", "乙"})
    else:
        b = script("act_02", "乙")
    fixed, reason = shared_formation(((1, a), (2, b)), owned)
    assert fixed is None and reason


def test_same_set_can_use_a_suitable_alternative_for_the_same_stage(search_source):
    search_source[3][20] = ((2, script("act_01", "乙")), (5, script("act_01", "甲")))
    plan, files, _ = select(search_source)
    assert plan["selection"][0]["set_id"] == 20
    assert "clearance-5.json" in files and "clearance-2.json" not in files


def test_ex_allowance_is_per_battle_not_union_across_collection(search_source):
    _, data, rows, jobs, _ = search_source
    stages = data["stage_table"]["stages"]
    for identifier in ("act_01", "act_01#f#"):
        stages[identifier]["code"] = "EV-EX-1"
    jobs[30] = ((2, script("act_01", "甲", "乙")), (3, script("act_02", "甲", "丙")))
    rows[:] = rows[:1]
    plan, _, _ = select(search_source)
    assert plan["selection"][0]["set_id"] == 30
    assert plan["selection"][0]["missing_operators"] == {"2": ["乙"], "3": ["丙"]}


def test_no_candidate_does_not_drop_full_scope_or_invent_a_script(search_source):
    search_source[2].clear()
    plan, files, _ = select(search_source)
    assert plan["targets"] == [] and files == {}
    assert len(plan["required"]) == 4
    assert {v["status"] for v in plan["selection"]} == {"no_usable_set"}


def test_completed_battles_do_not_require_operators_or_downloads(search_source):
    plan, _, _ = select(
        search_source,
        owned=(),
        completed=[
            ("act_01", False),
            ("act_01", True),
            ("act_02", False),
            ("act_02", True),
        ],
    )
    assert not search_source[-1] and plan["targets"] == []


def test_closed_ex_region_is_not_searched_or_frozen_as_a_target(search_source):
    _, data, _, _, _ = search_source
    data["stage_table"]["stageValidInfo"] = {"act_02": {"startTs": NOW + 100}}
    plan, _, _ = select(search_source)
    assert [v["region"] for v in plan["selection"]] == ["normal"]
    assert {t["code"] for t in plan["targets"]} == {"EV-1"}


@pytest.mark.parametrize("window_kind", ["stage", "zone"])
def test_unopened_region_does_not_block_farming_but_blocks_shop_full_clear(
    search_source, window_kind
):
    from maa_planner.daemon.execution.activity_copilot import farming_gate
    from maa_planner.daemon.planning.activity_scope import activity_scope

    _, data, _, _, _ = search_source
    if window_kind == "stage":
        data["stage_table"]["stageValidInfo"] = {"act_02": {"startTs": NOW + 100}}
    else:
        for identifier in ("act_02", "act_02#f#"):
            data["stage_table"]["stages"][identifier]["zoneId"] = "later"
        data["activity_table"]["zoneToActivity"]["later"] = "event"
        data["zone_table"]["zoneValidInfo"]["later"] = {"startTs": NOW + 100, "endTs": 2999}
    plan, _, _ = select(search_source)
    done = frozenset({("act_01", False), ("act_01", True)})
    assert farming_gate(plan, done) == {"allowed": True, "reason": "ready", "missing": []}
    assert ["act_02", True] in activity_scope(data, "event")["required"]
    opened, _, _ = select(search_source, now=NOW + 100)
    assert farming_gate(opened, done)["allowed"] is False


def test_s_region_uses_its_actual_stage_codes_instead_of_title_tokens(search_source):
    _, data, _, _, _ = search_source
    for identifier in ("act_02", "act_02#f#"):
        data["stage_table"]["stages"][identifier]["code"] = "EV-S-2"
    plan, _, _ = select(search_source)
    assert [v["region"] for v in plan["selection"]] == ["normal", "S"]
    assert plan["selection"][1]["set_id"] == 30


def test_search_moves_to_next_activity_after_complete_account_certification(search_source):
    from datetime import timedelta

    _, data, _, _, _ = search_source
    snapshot = activity()
    story = snapshot.data.side_stories[0]
    later = replace(story, key="LATER", name="后续活动", ends_at=story.ends_at + timedelta(days=1))
    snapshot = replace(snapshot, data=replace(snapshot.data, side_stories=(later, story)))
    data["activity_table"]["basicInfo"]["later"] = {
        "id": "later",
        "name": later.name,
        "startTime": 1000,
        "endTime": 3000,
    }
    data["activity_table"]["zoneToActivity"]["later_zone"] = "later"
    done = frozenset((stage, raid) for stage in ("act_01", "act_02") for raid in (False, True))
    first, identifier = current_activity(
        data, snapshot, NOW, lambda key: done if key == "event" else frozenset()
    )
    assert first.key == "LATER" and identifier == "later"
    first, identifier = current_activity(data, snapshot, NOW, lambda key: frozenset())
    assert first.key == "SS" and identifier == "event"


def test_query_follows_has_next_not_api_page_count(tmp_path, monkeypatch):
    pages = []

    def post(self, url, payload):
        pages.append(payload)
        return {
            "page": 65,
            "has_next": len(pages) == 1,
            "data": [{"id": len(pages), "name": "EV", "creator_id": "7661"}],
        }

    monkeypatch.setattr(_Source, "post", post)
    rows = query_sets(SimpleNamespace(cache_directory=tmp_path), threading.Event())
    assert [p["page"] for p in pages] == [1, 2]
    assert all(p["creator_id"] == "7661" and p["only_following"] is False for p in pages)
    assert len(rows) == 2


@pytest.mark.parametrize("kind", ["creator", "repeat", "empty"])
def test_untrusted_or_incomplete_pagination_is_an_error(tmp_path, monkeypatch, kind):
    def post(self, url, payload):
        return {
            "has_next": True,
            "data": []
            if kind == "empty"
            else [{"id": 1, "name": "EV", "creator_id": "other" if kind == "creator" else "7661"}],
        }

    monkeypatch.setattr(_Source, "post", post)
    with pytest.raises(ValueError):
        query_sets(SimpleNamespace(cache_directory=tmp_path), threading.Event())


@pytest.mark.parametrize(
    "fields",
    [
        {"auto_copilot": True},
        {"auto_copilot": False},
        {"copilot_url": "prts://s1"},
        {"client_type": "YoStarEN"},
    ],
)
def test_automatic_selection_config_rejects_ambiguous_or_unsupported_modes(tmp_path, fields):
    row = {
        "id": "daily",
        "profiles": ["a"],
        "times": ["12:00"],
        "tasks": [{"type": "Fight"}],
        **fields,
    }
    with pytest.raises(ValueError):
        parse_schedules([row], {"a"}, tmp_path / "config.toml")


@pytest.mark.parametrize("kind", ["Fight", "Award", "Infrast"])
def test_schedule_has_no_automatic_search_toggle(tmp_path, kind):
    from dataclasses import asdict

    row = {"id": "daily", "profiles": ["a"], "times": ["12:00"], "tasks": [{"type": kind}]}
    schedule = parse_schedules([row], {"a"}, tmp_path / "config.toml")[0]
    assert "auto_copilot" not in asdict(schedule)


def test_tutorial_script_is_selected_in_normal_region_instead_of_ignored(search_source):
    _, data, _, jobs, _ = search_source
    stages = data["stage_table"]["stages"]
    for identifier in ("act_01", "act_01#f#"):
        stages[identifier]["code"] = "EV-TR-1"
    jobs[40] = ((4, script("act_01", "甲")),)
    plan, _, _ = select(search_source)
    assert plan["selection"][0]["region"] == "normal"
    assert {t["script_id"] for t in plan["targets"] if t["code"] == "EV-TR-1"} == {4}


def test_absent_tutorial_has_explicit_barrier_and_does_not_discard_usable_battles(
    search_source,
):
    _, data, _, _, _ = search_source
    data["stage_table"]["stages"]["tutorial"] = {
        **data["stage_table"]["stages"]["act_01"],
        "stageId": "tutorial",
        "code": "EV-TR-1",
        "hardStagedId": None,
    }
    plan, _, _ = select(search_source)
    assert plan["selection"][0]["status"] == "selected"
    assert ["tutorial", False] in plan["required"]
    assert any(t["code"] == "EV-1" and t["script_id"] > 0 for t in plan["targets"])
    barrier = next(t for t in plan["targets"] if t["code"] == "EV-TR-1")
    assert barrier["kind"] == "missing_tutorial" and barrier["script_id"] == 0

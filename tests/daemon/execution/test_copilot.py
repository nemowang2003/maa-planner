import json
import threading

import httpx
import pytest

from maa_planner.daemon.adapters.mumu import Instance
from maa_planner.daemon.config import Settings
from maa_planner.daemon.copilot_command import CopilotCommand
from maa_planner.daemon.execution import copilot
from maa_planner.daemon.execution.context import EmulatorRuntime
from maa_planner.daemon.execution.copilot import prepare_copilot
from maa_planner.daemon.execution.runtime import CoreRuntime
from tests.helpers.execution import request


@pytest.mark.parametrize(
    "url", ["https://example.com/job", "maa://123", "prts://s0", "prts://../1"]
)
def test_only_current_prts_urls_are_accepted(url):
    with pytest.raises(ValueError, match="URL"):
        CopilotCommand("test", "nemo", url)


def test_parameters_are_not_truthy_strings_or_boolean_indexes():
    with pytest.raises(ValueError, match="布尔"):
        CopilotCommand.from_dict(
            {"request_id": "test", "profile": "nemo", "url": "prts://1", "formation": "false"}
        )
    with pytest.raises(ValueError, match="正整数"):
        CopilotCommand("test", "nemo", "prts://1", start_at=True)


def test_public_query_post_does_not_reuse_get_etag_or_cache(tmp_path, monkeypatch):
    requests = []

    def handle(transport, request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"ETag": "public-tag"},
            json={"status_code": 200, "data": {"method": request.method}},
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    source = copilot._Source(threading.Event(), tmp_path)
    url = "https://public.test/query"
    assert source.get(url) == {"method": "GET"}
    assert source.post(url, {"creator_id": "7661"}) == {"method": "POST"}
    assert json.loads(requests[-1].content) == {"creator_id": "7661"}
    assert "if-none-match" not in requests[-1].headers
    assert source.get(url) == {"method": "GET"}
    assert requests[-1].headers["if-none-match"] == "public-tag"


@pytest.mark.parametrize("limit", ["bytes", "requests"])
def test_exhausted_source_budget_never_makes_another_request(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *a, **k: pytest.fail("预算耗尽后不应联网")
    )
    source = copilot._Source(threading.Event(), tmp_path)
    if limit == "bytes":
        source.total = copilot.MAX_TOTAL
    else:
        source.requests = copilot.MAX_REQUESTS
    with pytest.raises(ValueError, match="限制"):
        source.get("https://public.test/query")


@pytest.fixture
def source(tmp_path, monkeypatch):
    binding = request(tmp_path).binding
    root = tmp_path / "runtime"
    tiles = root / "data/resource/Arknights-Tile-Pos"
    tiles.mkdir(parents=True)
    runtime = CoreRuntime(root / "maa", root / "data", root / "navigation", tmp_path / "runs")
    settings = Settings(
        tmp_path / "config.toml",
        (binding.profile,),
        core_runtime=runtime,
    )
    scripts = {}
    for identifier in (31, 7, 21):
        stage = f"act_test_{identifier}"
        scripts[identifier] = {
            "stage_name": stage,
            "actions": [],
            "opers": [{"name": str(identifier), "skill": 2}],
        }
        (tiles / (stage + "-map.json")).write_text(json.dumps({"code": f"TL-{identifier}"}))
    reads = []

    def get(self, url):
        reads.append(url)
        if "/set/get" in url:
            return {"copilot_ids": [31, 7, 21]}
        return {"content": json.dumps(scripts[int(url.rsplit("/", 1)[1])])}

    monkeypatch.setattr(copilot._Source, "get", get)
    return settings, binding, scripts, reads


def test_set_order_start_index_and_false_formation_survive_snapshot(source):
    settings, binding, scripts, reads = source
    command = CopilotCommand("test", "nemo", "prts://s56648", ignore_requirements=True, start_at=2)
    req = prepare_copilot(settings, command, binding, "revision", "attempt", threading.Event())
    restored = type(req).from_dict(req.to_dict())
    assert restored.core is not None
    params = json.loads(restored.core.tasks.document)["tasks"][0]["params"]
    assert params["formation"] is False
    assert params["ignore_requirements"] is True
    assert params["use_sanity_potion"] is False
    assert [item["nav_name_override"] for item in params["copilot_list"]] == ["TL-7", "TL-21"]
    assert len(reads) == 3
    scripts[7]["opers"] = []
    assert json.loads(restored.core.tasks.attachments)["copilot-7.json"]["opers"]


def test_single_job_keeps_single_stage_entry_semantics(source):
    settings, binding, scripts, reads = source
    command = CopilotCommand("test", "nemo", "prts://31", formation=True)
    req = prepare_copilot(settings, command, binding, "rev", "single", threading.Event())
    assert req.core is not None
    params = json.loads(req.core.tasks.document)["tasks"][0]["params"]
    assert params["filename"] == "@file:copilot-31.json"
    assert "copilot_list" not in params
    assert params["formation"] is True


@pytest.mark.parametrize(
    "url,select,expected",
    [
        ("prts://31", None, [31]),
        ("prts://s56648", None, [31, 7, 21]),
        ("prts://s56648", (3, 1), [31, 21]),
    ],
)
def test_raid_uses_navigation_list_and_survives_worker_snapshot(source, url, select, expected):
    settings, binding, _, _ = source
    command = CopilotCommand("raid", "nemo", url, is_raid=True, formation=True, select=select)
    assert CopilotCommand.from_dict(command.to_dict()) == command
    req = prepare_copilot(settings, command, binding, "rev", "raid", threading.Event())
    restored = type(req).from_dict(req.to_dict())
    assert restored.core is not None
    params = json.loads(restored.core.tasks.document)["tasks"][0]["params"]
    assert "filename" not in params and "loop_times" not in params
    assert params["formation"] is True and params["use_sanity_potion"] is False
    queue = params["copilot_list"]
    assert [item["nav_name_override"] for item in queue] == [f"TL-{i}" for i in expected]
    assert all(item["is_raid"] is True for item in queue)
    attachments = json.loads(restored.core.tasks.attachments)
    assert all(item["filename"].removeprefix("@file:") in attachments for item in queue)


@pytest.mark.parametrize("flag", ["is_raid", "use_sanity_potion"])
@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
def test_copilot_flags_require_a_boolean(flag, value):
    with pytest.raises(ValueError, match=flag):
        CopilotCommand.from_dict(
            {"request_id": "bad", "profile": "nemo", "url": "prts://31", flag: value}
        )


@pytest.mark.parametrize(
    "url,raid", [("prts://31", False), ("prts://31", True), ("prts://s56648", False)]
)
@pytest.mark.parametrize("allow_medicine", [False, True])
def test_explicit_medicine_option_survives_worker_snapshot(source, url, raid, allow_medicine):
    settings, binding, _, _ = source
    command = CopilotCommand(
        "medicine", "nemo", url, is_raid=raid, use_sanity_potion=allow_medicine
    )
    assert CopilotCommand.from_dict(command.to_dict()) == command
    req = prepare_copilot(settings, command, binding, "rev", "medicine", threading.Event())
    restored = type(req).from_dict(req.to_dict())
    assert restored.core is not None
    params = json.loads(restored.core.tasks.document)["tasks"][0]["params"]
    assert params["use_sanity_potion"] is allow_medicine
    assert not any("stone" in key for key in params)


@pytest.mark.parametrize("indices,codes", [((3, 1), ["TL-31", "TL-21"]), ((2,), ["TL-7"])])
def test_subset_only_downloads_selected_scripts_in_original_order(source, indices, codes):
    settings, binding, _, reads = source
    command = CopilotCommand("subset", "nemo", "prts://s56648", select=indices)
    assert CopilotCommand.from_dict(command.to_dict()) == command
    req = prepare_copilot(settings, command, binding, "rev", "subset", threading.Event())
    assert req.core is not None
    params = json.loads(req.core.tasks.document)["tasks"][0]["params"]
    assert [item["nav_name_override"] for item in params["copilot_list"]] == codes
    assert params["formation"] is False
    assert len(reads) == 1 + len(indices)
    assert len(json.loads(req.core.tasks.attachments)) == len(indices)


@pytest.mark.parametrize("indices", [[], [0], [True], [1, 1], "1,2"])
def test_invalid_subsets_are_rejected(indices):
    with pytest.raises(ValueError, match="select"):
        CopilotCommand.from_dict(
            {"request_id": "bad", "profile": "nemo", "url": "prts://s1", "select": indices}
        )


def test_subset_conflicts_with_start_index_and_out_of_bounds_fails(source):
    with pytest.raises(ValueError, match="不能同时"):
        CopilotCommand("bad", "nemo", "prts://s1", start_at=2, select=(1,))
    settings, binding, _, reads = source
    with pytest.raises(ValueError, match="超出"):
        prepare_copilot(
            settings,
            CopilotCommand("bad", "nemo", "prts://s1", select=(4,)),
            binding,
            "rev",
            "bad",
            threading.Event(),
        )
    assert len(reads) == 1


def test_out_of_range_or_missing_stage_fails_before_worker(source):
    settings, binding, scripts, _ = source
    with pytest.raises(ValueError, match="超出"):
        prepare_copilot(
            settings,
            CopilotCommand("test", "nemo", "prts://s1", start_at=4),
            binding,
            "rev",
            "attempt",
            threading.Event(),
        )
    scripts[31]["stage_name"] = "unknown_stage"
    with pytest.raises(ValueError, match="更新资源"):
        prepare_copilot(
            settings,
            CopilotCommand("test", "nemo", "prts://31"),
            binding,
            "rev",
            "attempt",
            threading.Event(),
        )


@pytest.mark.parametrize("running,pids", [(False, "123"), (True, ""), (True, "123 456")])
def test_game_probe_never_boots_or_starts_any_application(tmp_path, monkeypatch, running, pids):
    binding = request(tmp_path).binding
    instance = Instance(binding.instance_index, binding.instance_name, running, "127.0.0.1:12345")
    calls = []

    class MuMu:
        def __init__(self, root):
            pass

        def instances(self):
            return [instance]

        def connect(self, address):
            calls.append("connect")

        def adb(self, address, *args, **kwargs):
            assert args == ("shell", "pidof", "com.hypergryph.arknights")
            calls.append("pidof")
            return pids

    monkeypatch.setattr("maa_planner.daemon.execution.context.MuMu", MuMu)
    runtime = EmulatorRuntime(allow_foreground_game=True, package_name="com.hypergryph.arknights")
    if not running or not pids:
        with pytest.raises(RuntimeError, match="运行"):
            runtime.require_game_running(binding)
    else:
        assert runtime.require_game_running(binding) == instance
    assert calls == ([] if not running else ["connect", "pidof"])

from maa_planner.daemon.planning.activity_rewards import reward_task
from tests.helpers.activity_shop import navigation_assets, shop_plan


def plan():
    value = shop_plan()
    value["mission_entry"] = navigation_assets(missions=True)["entries"]["missions"]
    return value


def test_uncalibrated_reward_button_scans_whole_verified_page():
    _, nodes = reward_task(plan(), 0)
    assert nodes["Planner@Rewards0@MissionControls"]["roi"] == [0, 0, 1280, 720]
    assert nodes["Planner@Rewards0@Claim"]["roi"] == [0, 0, 1280, 720]


def test_cached_roi_has_full_frame_fallback_before_no_claim():
    _, nodes = reward_task({**plan(), "claim_roi": [900, 100, 300, 100]}, 0)
    prefix = "Planner@Rewards0@"
    choices = nodes[prefix + "MissionReady"]["next"]
    assert choices.index(prefix + "CachedControls") < choices.index(prefix + "MissionControls")
    assert choices.index(prefix + "MissionControls") < choices.index(prefix + "NoClaim")
    assert nodes[prefix + "CachedControls"]["roi"] == [900, 100, 300, 100]
    assert nodes[prefix + "CachedClaim"]["roi"] == [900, 100, 300, 100]
    # A button that moves between recognition and clicking can be rediscovered.
    assert prefix + "MissionControls" in nodes[prefix + "CachedControls"]["next"]


def test_callback_calibration_is_reused_by_preparation_without_claim_history(tmp_path, monkeypatch):
    import asyncio
    import json
    import threading
    from types import SimpleNamespace

    from maa_planner.daemon.execution.core import run
    from maa_planner.daemon.planning.activity_shop import prepare_shop_context
    from maa_planner.daemon.state import StateStore
    from maa_planner.daemon.tools.reward_roi import load_claim_roi, save_claim_roi
    from tests.helpers.activity_shop import NOW, activity, configured, shop_catalog
    from tests.helpers.core import FakeCore, spec

    task, _ = reward_task(plan(), 0)
    marker = task["params"]["activity_reward"]

    class RoiCore(FakeCore):
        def task_outcome(self, identifier, kind, params):
            for node in ("MissionReady", "MissionControls", "Claim", "Received"):
                self.emit(
                    20002,
                    {
                        "taskid": identifier,
                        "taskchain": kind,
                        "subtask": "ProcessTask",
                        "details": {
                            "task": node,
                            "result": {"text": "键领取", "rect": [1050, 140, 100, 30]},
                        },
                    },
                )
            return 10002

    value = spec(tmp_path)
    value["tasks"] = [task]
    monkeypatch.setattr("maa_planner.daemon.execution.core.time.time", lambda: NOW)
    run(value, stop=threading.Event(), library_factory=lambda _: SimpleNamespace(lib=RoiCore()))
    report = json.loads((tmp_path / "core-result.json").read_text())["activity_reward_results"][0]
    assert report["outcome"] == "claimed"
    assert report["claim_roi"] == [1025, 125, 150, 60]
    assert report["claim_roi_key"] == marker["claim_roi_key"]
    from maa_planner.daemon.config import Settings
    from maa_planner.daemon.coordinator import Daemon
    from maa_planner.daemon.execution.context import RunResult
    from maa_planner.daemon.planning.schedule import ScheduledOccurrence
    from maa_planner.daemon.policy import ConfigSnapshot

    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control")
    daemon.store = StateStore(tmp_path / "state.sqlite3")
    occurrence = ScheduledOccurrence("roi-test", "a", NOW, NOW + 86400)
    daemon.store.claim_schedule(occurrence, "revision")
    daemon.daily.attempts["attempt"] = occurrence, ConfigSnapshot("revision", b"")
    monkeypatch.setattr(
        "maa_planner.daemon.scheduling.settings_from_bytes",
        lambda *args: Settings(tmp_path / "config.toml", (), cache_directory=tmp_path),
    )
    daemon.daily.finished(RunResult("attempt", "succeeded", activity_reward_results=(report,)))
    artifact = json.loads(
        (tmp_path / "activity-roi" / (marker["claim_roi_key"] + ".json")).read_text()
    )
    assert "outcome" not in artifact and "evidence" not in artifact
    daemon.store.close()

    # Absence later is normal; never replace a useful ROI with an empty result.
    assert not save_claim_roi(
        tmp_path, {**report, "outcome": "nothing_to_claim", "claim_roi": None}
    )
    assert load_claim_roi(tmp_path, marker["claim_roi_key"]) == [1025, 125, 150, 60]

    async def catalog():
        return shop_catalog()

    async def navigation(catalog, activity_id):
        return navigation_assets(missions=True)

    store = StateStore(tmp_path / "state.sqlite3")
    shops = asyncio.run(
        prepare_shop_context(
            configured(tmp_path),
            activity(),
            now=NOW,
            store=store,
            profile="a",
            load_catalog=catalog,
            load_navigation=navigation,
            roi_cache_directory=tmp_path,
        )
    )
    store.close()
    _, nodes = reward_task(shops[0], 0)
    assert nodes["Planner@Rewards0@CachedControls"]["roi"] == [1025, 125, 150, 60]


def test_unverified_or_invalid_match_cannot_prepare_roi_and_new_resources_invalidate_cache(
    tmp_path,
):
    from maa_planner.daemon.execution.core import CallbackResults
    from maa_planner.daemon.tools.reward_roi import load_claim_roi, reward_roi_key, save_claim_roi

    result = CallbackResults()
    result.rewards[1] = {"outcome": "unknown", "page_verified": False, "evidence": []}

    def emit(node, rect):
        result.consume(
            20002,
            {
                "taskid": 1,
                "taskchain": "Custom",
                "subtask": "ProcessTask",
                "details": {"task": node, "result": {"text": "一键领取", "rect": rect}},
            },
            "callback:1",
        )

    emit("MissionControls", [1000, 100, 200, 50])
    assert "claim_roi" not in result.rewards[1]
    emit("MissionReady", [0, 0, 10, 10])
    emit("MissionControls", [-1, 100, 200, 50])
    assert "claim_roi" not in result.rewards[1]
    emit("CachedControls", [0, 0, 40, 20])
    assert result.rewards[1]["claim_roi"] == [0, 0, 50, 30]
    first = plan()
    key = reward_roi_key(first)
    assert save_claim_roi(
        tmp_path, {**result.rewards[1], "claim_roi_key": key, "activity_id": "event"}
    )
    changed = {**first, "navigation_source": {"revision": "new-sprites"}}
    assert reward_roi_key(changed) != key
    assert load_claim_roi(tmp_path, reward_roi_key(changed)) is None


def test_invalid_cache_is_optional_and_does_not_authorize_unverified_calibration(tmp_path):
    import json

    from maa_planner.daemon.tools.reward_roi import load_claim_roi, reward_roi_key, save_claim_roi

    key = reward_roi_key(plan())
    report = {
        "claim_roi_key": key,
        "claim_roi": [100, 100, 100, 50],
        "page_verified": False,
        "activity_id": "event",
    }
    assert not save_claim_roi(tmp_path, report)
    assert load_claim_roi(tmp_path, key) is None
    path = tmp_path / "activity-roi" / (key + ".json")
    path.parent.mkdir()
    path.write_text("broken")
    assert load_claim_roi(tmp_path, key) is None
    path.write_text(
        json.dumps({"key": key, "revision": 1, "viewport": [1280, 720], "roi": [1270, 0, 40, 20]})
    )
    assert load_claim_roi(tmp_path, key) is None

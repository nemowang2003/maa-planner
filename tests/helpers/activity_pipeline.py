"""Offline daily pipeline; only HTTP payloads and MaaCore are substituted."""

import json
import threading
import time
from types import SimpleNamespace

from maa_planner.daemon.execution.activity_copilot import ClearanceReport
from maa_planner.daemon.execution.core import run
from maa_planner.daemon.planning.activity_shop import prepare_shops
from maa_planner.daemon.planning.auto_copilot import prepare_automatic
from maa_planner.daemon.planning.daily import build_daily_plan
from maa_planner.daemon.planning.schedule import parse_schedules
from maa_planner.schema import StageConfirmation
from tests.helpers.activity_shop import activity, navigation_assets
from tests.helpers.clearance import NOW, catalog
from tests.helpers.core import spec


class ActivityPipeline:
    def __init__(self, root, store):
        self.root, self.store = root, store
        _, self.catalog = catalog()
        stages = self.catalog["stage_table"]["stages"]
        # Normal and EX are open, S opens later. Each has a normal/raid pair.
        for key in ("act_01", "act_01#f#"):
            stages[key]["code"] = "EV-1"
        for key, original in list(stages.items()):
            if not key.startswith("act_02"):
                continue
            new = key.replace("act_02", "act_s")
            stages[new] = {
                **original,
                "stageId": new,
                "levelId": original["levelId"].replace("act_02", "act_s"),
                "zoneId": "s_zone",
                "code": "EV-S-1",
                "hardStagedId": "act_s#f#" if original["hardStagedId"] else None,
                "unlockCondition": [{"stageId": "act_02", "completeState": "PASS"}]
                if not new.endswith("#f#")
                else [{"stageId": "act_s", "completeState": "COMPLETE"}],
            }
        self.catalog["zone_table"]["zones"]["s_zone"] = {"zoneNameSecond": "S区域"}
        self.catalog["zone_table"]["zoneValidInfo"]["s_zone"] = {"startTs": 2500, "endTs": 2999}
        self.catalog["activity_table"]["zoneToActivity"]["s_zone"] = "event"
        self.stage_ids = {v["code"]: k for k, v in stages.items() if v["difficulty"] == "NORMAL"}
        self.scripts = {
            i: {"stage_name": key, "difficulty": 3, "actions": [], "opers": []}
            for i, key in enumerate(("act_01", "act_02", "act_s"), 1)
        }
        self.collections = tuple(
            {"id": i, "name": title, "creator_id": "7661"}
            for i, title in enumerate(("EV 普通全关卡", "EV EX全关卡", "EV S全关卡"), 1)
        )
        self.reads = []
        self.schedule = parse_schedules(
            [
                {
                    "id": "daily",
                    "profiles": ["nemo"],
                    "times": ["12:00"],
                    "session": {"startup": {}, "closedown": {}},
                    "tasks": [
                        {"type": "Recruit"},
                        {"type": "Fight", "strategy": "expert", "params": {"times": 2}},
                        {"type": "Award"},
                    ],
                }
            ],
            {"nemo"},
            root / "config.toml",
        )[0]

    def response(self, url):
        self.reads.append(url)
        if "/set/get" in url:
            return {"copilot_ids": [int(url.rsplit("=", 1)[1])]}
        return {"content": json.dumps(self.scripts[int(url.rsplit("/", 1)[1])])}

    def confirm_open(self):
        for stage in ("act_01", "act_02"):
            for raid in (False, True):
                self.store.confirm_stage(StageConfirmation("nemo", "event", stage, raid), now=NOW)

    async def prepare(self, now, *, pending=None, available=True):
        async def load_catalog():
            return self.catalog

        async def load_sets():
            return self.collections if available else ()

        info = SimpleNamespace(cultivate={"characters": []}, player_info={"charInfoMap": {}})
        snapshot = activity()
        clearance = await prepare_automatic(
            SimpleNamespace(cache_directory=self.root),
            SimpleNamespace(name="nemo"),
            info,
            snapshot,
            self.store,
            load_catalog,
            load_sets,
            threading.Event(),
            now=now,
        )
        shops = prepare_shops(
            self.catalog, snapshot, now=now, navigation={"event": navigation_assets()}, blacklist=()
        )
        # A user's disabled shop remains disabled even when clearance succeeds.
        shops = tuple({**p, "navigation_status": "disabled"} for p in shops)
        return build_daily_plan(
            self.schedule,
            now=now,
            activity=snapshot,
            depot={},
            clearance=clearance,
            shops=shops,
            pending=pending,
        )

    def execute(self, plan, core, name, *, certified=None):
        assert plan.tasks is not None
        folder = self.root / name
        folder.mkdir()
        document = {**spec(folder), **plan.tasks.materialize(folder)}

        def record(value):
            self.store.record_clearance(
                "nemo", "event", ClearanceReport.from_dict(value), now=time.time()
            )
            if certified:
                certified()

        code = run(
            document, library_factory=lambda _: SimpleNamespace(lib=core), on_clearance=record
        )
        return code, json.loads((folder / "core-result.json").read_text())

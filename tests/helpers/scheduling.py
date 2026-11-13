import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from maa_planner.daemon import coordinator, scheduling
from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.execution.context import RunResult, UnitBinding
from maa_planner.daemon.preparation import PreparedEnvironment
from tests.helpers.activity import empty_activity
from tests.helpers.async_tools import eventually


def timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=BUSINESS_TIMEZONE).timestamp()


class Clock:
    def __init__(self, value="2026-11-02T12:00:00", *, realtime=False):
        self.value = timestamp(value)
        self.realtime = realtime
        self.began = time.monotonic()

    def now(self):
        return self.value + (time.monotonic() - self.began if self.realtime else 0)

    def set(self, value):
        self.value = timestamp(value)


def document(names=("a", "b", "c", "d"), *, times='["12:10", "15:40", "19:40"]', enabled=True):
    return (
        "\n".join(f'[[profiles]]\nname="{name}"' for name in names)
        + f"""
[[schedules]]
id="daily"
profiles={json.dumps(names)}
times={times}
enabled={str(enabled).lower()}
session={{startup={{start_game_enabled=true}},closedown={{}}}}
[[schedules.tasks]]
type="Fight"
params={{stage="1-7", medicine=0, stone=0, expiring_medicine=999, medicine_expire_days=2}}
"""
    )


@asynccontextmanager
async def running(
    tmp_path, monkeypatch, clock, *, content=None, loader=None, alias=False, admit_gate=None
):
    monkeypatch.setattr(coordinator, "state_directory", lambda: tmp_path / "user/daemon")
    monkeypatch.setattr(time, "time", clock.now)
    config = tmp_path / "config.toml"
    if content is not None:
        config.write_text(content, newline="\n")
    workers, calls = [], []

    def resolve(settings, name, pure):
        assert not pure
        profile = next(p for p in settings.profiles if p.name == name)
        index = 0 if alias else list(settings.profiles).index(profile)
        return UnitBinding(profile, tmp_path / "mumu", index, name)

    async def prepare(self, settings, *, pinned=None):
        if pinned is None:
            calls.append(settings)
        if loader is not None:
            await loader()
        return PreparedEnvironment(settings.runtime_template(), empty_activity())

    async def no_skland(*args):
        pytest.fail("本隔离场景未提供 Skland 模拟，不应采集真实账号")

    class Worker:
        def __init__(self, request, root):
            self.request = request
            self.allowed = None
            self.finish = asyncio.Event()
            self.cancelled = False
            workers.append(self)

        async def run(self, admit, phase):
            if admit_gate is not None:
                await admit_gate.wait()
            self.allowed = await admit()
            if self.allowed:
                await phase("running")
                await self.finish.wait()
            return getattr(
                self,
                "result",
                RunResult(
                    self.request.attempt_id,
                    "succeeded" if self.allowed and not self.cancelled else "cancelled",
                ),
            )

        async def cancel(self):
            self.cancelled = True
            self.finish.set()
            if admit_gate is not None:
                admit_gate.set()

    async def no_rescue(*args, **kwargs):
        return None

    monkeypatch.setattr("maa_planner.daemon.planning.annihilation_rescue.prepare_rescue", no_rescue)
    monkeypatch.setattr(scheduling, "resolve_binding", resolve)
    monkeypatch.setattr(Daemon, "prepare_dependencies", prepare)
    monkeypatch.setattr(scheduling, "collect_character_info", no_skland)
    from tests.helpers.activity_shop import shop_catalog

    def catalog_snapshot(cache):
        value = shop_catalog()
        # The default public snapshot can be replaced by a scenario. Keep the
        # isolated catalog on that same name/window instead of contacting GitHub
        # or pairing 1970 fixture metadata with a 2026 activity.
        stories = empty_activity().data.side_stories
        if stories:
            value["activity_table"]["basicInfo"] = {
                "event" if index == 0 else f"event-{index}": {
                    "id": "event" if index == 0 else f"event-{index}",
                    "name": story.name,
                    "startTime": story.starts_at.timestamp(),
                    "endTime": story.ends_at.timestamp(),
                }
                for index, story in enumerate(stories)
            }
        return value

    from tests.helpers.activity_shop import navigation_assets

    monkeypatch.setattr(
        "maa_planner.daemon.tools.activity_sprite_navigation.prepare_navigation_assets",
        lambda *args: navigation_assets(),
    )
    monkeypatch.setattr(
        "maa_planner.daemon.tools.stage_catalog.load_stage_catalog", catalog_snapshot
    )
    monkeypatch.setattr(
        "maa_planner.daemon.planning.auto_copilot.collect_character_info", no_skland
    )
    daemon = Daemon(
        config, tmp_path / "runtime/control.json", root=tmp_path / "state", worker_factory=Worker
    )
    # These fake-clock scenarios isolate daily dispatch; retention has its own integration tests.
    monkeypatch.setattr(daemon.cache_maintenance, "tick", lambda: None)
    task = asyncio.create_task(daemon.run())
    await eventually(
        lambda: (daemon.configuration is not None or daemon.config_error is not None)
        and daemon.server.is_serving()
    )
    try:
        yield SimpleNamespace(daemon=daemon, config=config, workers=workers, calls=calls, task=task)
    finally:
        daemon.request_stop()
        await asyncio.wait_for(task, 6)


def wake(state, clock, value):
    clock.set(value)
    state.daemon.wakeup.set()

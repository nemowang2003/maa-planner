import asyncio
from contextlib import asynccontextmanager

from maa_planner.daemon import coordinator as daemon_module
from maa_planner.daemon.coordinator import Daemon
from tests.helpers.async_tools import eventually
from tests.helpers.control import ApiPeer


@asynccontextmanager
async def running(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(daemon_module, "state_directory", lambda: tmp_path / "user/daemon")
    config = tmp_path / "config.toml"
    if not config.exists():
        config.write_text('[[profiles]]\nname="nemo"\n', newline="\n")

    async def latest_runtime(self, request):
        assert request.core is not None
        return request.core.runtime

    from maa_planner.daemon.preparation import PreparedEnvironment
    from tests.helpers.activity import empty_activity

    async def prepare_dependencies(self, settings, *, pinned=None):
        return PreparedEnvironment(settings.runtime_template(), empty_activity())

    monkeypatch.setattr(Daemon, "prepare_dependencies", prepare_dependencies)
    monkeypatch.setattr(Daemon, "latest_runtime", latest_runtime)
    daemon = Daemon(
        config,
        tmp_path / "runtime/control.json",
        root=tmp_path / "state",
        **kwargs,
    )
    task = asyncio.create_task(daemon.run())
    await eventually(lambda: daemon.configuration is not None and daemon.server.is_serving())
    try:
        yield daemon, ApiPeer(daemon.server.path), task
    finally:
        daemon.request_stop()
        await asyncio.wait_for(task, 8)

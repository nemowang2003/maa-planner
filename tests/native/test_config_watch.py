import asyncio

from maa_planner.daemon.config_watch import ConfigWatch


def test_native_directory_watch_observes_atomic_replace_and_later_edits(tmp_path):
    async def scenario():
        path = tmp_path / "config.toml"
        path.write_text("initial")
        changed = asyncio.Event()
        watcher = ConfigWatch(path, changed.set)
        watcher.start()
        try:
            replacement = tmp_path / "replacement.toml"
            replacement.write_text("replaced")
            replacement.replace(path)
            await asyncio.wait_for(changed.wait(), 5)
            assert path.read_text() == "replaced"
            changed.clear()
            path.write_text("edited")
            await asyncio.wait_for(changed.wait(), 5)
            assert path.read_text() == "edited"
        finally:
            await watcher.close()

    asyncio.run(scenario())

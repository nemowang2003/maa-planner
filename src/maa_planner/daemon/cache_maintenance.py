"""Run cache retention under the coordinator's existing idle maintenance barrier."""

import asyncio
import logging
import time

from maa_planner.daemon.tools.cache_cleanup import DAY, clean_cache, recovery_pending

log = logging.getLogger(__name__)


class CacheMaintenance:
    def __init__(self, daemon):
        self.daemon = daemon
        self.next_at = time.time() + 60
        self.task: asyncio.Task | None = None

    def tick(self):
        daemon = self.daemon
        settings = daemon._settings
        if settings is None or not settings.cleanup.enabled:
            return None
        if self.task is not None and not self.task.done():
            return None
        now = time.time()
        if now < self.next_at:
            return self.next_at
        store = daemon.control_store()
        if (
            daemon.resources_busy
            or daemon.maintenance_pending
            or daemon.stopping.is_set()
            or daemon.draining
            or daemon.running
            or daemon.operations.tasks
            or daemon.operations.submissions
            or daemon._runtime_checks
            or daemon._apk_checks
            or daemon._active_preparations
            or store.cache_cleanup_blocked()
        ):
            self.next_at = now + 60
            return self.next_at
        owner = "cache-cleanup"
        daemon.reserve_resources(owner, exclusive=True)
        self.next_at = now + DAY

        async def run():
            try:

                def collect():
                    if recovery_pending(daemon.root / "recovery"):
                        return {"skipped": "recovery_pending"}
                    return clean_cache(settings, protected=(daemon.root,))

                report = await asyncio.to_thread(collect)
                store.event("cache_cleanup", report)
            except Exception:
                # Retention failures must never fail a daily task or a later admission.
                log.exception("缓存清理失败，保留文件等待下次维护")
            finally:
                daemon.release_resources(owner)

        self.task = asyncio.create_task(run())
        return None

    async def close(self):
        if self.task is not None:
            await asyncio.shield(self.task)

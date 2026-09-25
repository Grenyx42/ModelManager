from __future__ import annotations

import asyncio
import logging

from .manager import Manager

logger = logging.getLogger("modelmanager.scheduler")


class Scheduler:
    """Background idle sweep: stops non-pinned models past their idle_timeout_seconds.

    This frees VRAM proactively even with no new request competing for it; the
    reactive swap-on-conflict path in Manager.ensure() handles the "swap to the
    other model, but only once the current one goes idle" case immediately.
    """

    def __init__(self, manager: Manager, interval_seconds: float = 30.0):
        self.manager = manager
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            try:
                self.sweep_once()
            except Exception:
                logger.exception("idle sweep failed")
            await asyncio.sleep(self.interval_seconds)

    def sweep_once(self) -> None:
        self.manager.state.purge_all_expired_leases()
        for name, definition in self.manager.registry.models.items():
            if definition.pinned or definition.idle_timeout_seconds is None:
                continue
            state = self.manager.state.get(name)
            if not state.is_active or state.is_busy():
                continue
            idle = state.idle_seconds()
            if idle is not None and idle >= definition.idle_timeout_seconds:
                logger.info("evicting idle model %s (idle %.0fs)", name, idle)
                self.manager.stop(name)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

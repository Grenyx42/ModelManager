from __future__ import annotations

import asyncio
import logging

from .. import lifecycle_log
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
                # Last line of defence before we pull the rug: ActivityTracker
                # samples every few seconds, but a request that arrives in the
                # gap between its last tick and this sweep would otherwise be
                # killed mid-generation. Ask the engine directly.
                if self.manager.probe_busy(name):
                    logger.info("model %s looked idle (%.0fs) but is mid-request - not evicting", name, idle)
                    self.manager.state.touch(name)
                    continue
                logger.info("evicting idle model %s (idle %.0fs)", name, idle)
                self.manager.stop(name)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


class ActivityTracker:
    """Keeps the idle clock honest for traffic that never touches us.

    ensure()/acquire()/release() are the only things that set last_used, but
    they're just the handshake: callers get a host:port and then talk to the
    engine directly, so the coordinator sees nothing of the actual work. A
    model can serve a saturated six-slot workload for ten minutes and still be
    evicted on the dot of idle_timeout_seconds measured from the ensure() that
    started it - which is exactly what used to happen.

    So every tick, for each running model that could be evicted, ask its
    engine whether it's doing anything and refresh last_used if it is. That
    turns idle_timeout_seconds into "time since the model last did work",
    which is what it reads like and what anyone setting it expects.

    Engines without an activity signal return None from busy() and are left
    alone, falling back to the old call-based clock. Pinned models and models
    with no timeout can't be evicted anyway, so there's nothing to protect and
    we skip the probe rather than spend a request on them.
    """

    def __init__(self, manager: Manager, interval_seconds: float = 5.0):
        self.manager = manager
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    def check_once(self) -> None:
        for name, definition in self.manager.registry.models.items():
            if definition.pinned or definition.idle_timeout_seconds is None:
                continue
            if not self.manager.state.get(name).is_active:
                continue
            if self.manager.probe_busy(name):
                self.manager.state.touch(name)

    async def _run(self) -> None:
        while True:
            try:
                self.check_once()
            except Exception:
                logger.exception("activity tracking failed")
            await asyncio.sleep(self.interval_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()


class HealthChecker:
    """Every few seconds, checks each active model's adapter status directly
    (not just trusting our own state) and restarts it via manager.ensure() if
    the underlying process/server died on its own - a crash, as opposed to a
    deliberate stop() or an idle-timeout eviction, both of which already
    clear active state themselves and so are invisible to this check.

    manager.ensure() already knows how to recover from "state says active but
    the adapter reports it's not running" (see Manager._ensure_loaded) - that
    path previously only ran lazily, the next time something happened to call
    ensure() on that model. This just calls it proactively so a crashed model
    comes back within one interval instead of whenever it's next requested.

    Note: there's a narrow race with a concurrent stop() request - between
    the adapter actually stopping and set_active(None) landing, a health
    check tick could see "active but not running" and restart a model that
    was being deliberately stopped. Not guarded against (no per-model lock
    exists anywhere else in Manager either); acceptable for a single-user
    home-lab coordinator, worth revisiting if this ever gets concurrent
    callers that fight over the same model.
    """

    def __init__(self, manager: Manager, interval_seconds: float = 5.0):
        self.manager = manager
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    def check_once(self) -> None:
        for name, definition in self.manager.registry.models.items():
            state = self.manager.state.get(name)
            if not state.is_active:
                continue
            candidate = definition.candidates[state.active_candidate_index]
            adapter = self.manager.adapters.get(candidate.engine)
            try:
                status = adapter.status(name, candidate)
            except Exception as exc:
                # A status check that raises (e.g. HostUnreachableError from
                # an offline SSH box) must not stop the rest of this tick's
                # models from being checked - isolate it per-model, same as
                # reconcile_running_state() already does at startup.
                logger.warning("health check for %s failed: %s", name, exc)
                continue
            if status.running:
                continue
            logger.warning("model %s is active but not running - assuming it crashed, restarting", name)
            lifecycle_log.record(f"{name}: crash detected (process no longer running) - restarting")
            try:
                self.manager.ensure(name)
            except Exception as exc:
                logger.exception("failed to restart %s after crash", name)
                lifecycle_log.record(f"{name}: restart failed: {exc}")
            else:
                lifecycle_log.record(f"{name}: restarted successfully")

    async def _run(self) -> None:
        while True:
            try:
                self.check_once()
            except Exception:
                logger.exception("health check tick failed")
            await asyncio.sleep(self.interval_seconds)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


class RegistryWatcher:
    """Polls registry.json's mtime and calls Manager.reload_from_disk() the
    moment it changes, so a hand edit (or a delete) takes effect on its own
    instead of waiting for someone to hit POST /v1/config/reload.
    """

    def __init__(self, manager: Manager, interval_seconds: float = 5.0):
        self.manager = manager
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    def check_once(self) -> None:
        if not self.manager.registry_changed_on_disk():
            return
        try:
            result = self.manager.reload_from_disk()
            logger.info("registry.json changed on disk; reloaded (%s)", result)
        except Exception:
            logger.exception("failed to auto-reload registry.json after on-disk change")

    async def _run(self) -> None:
        while True:
            try:
                self.check_once()
            except Exception:
                logger.exception("registry watcher tick failed")
            await asyncio.sleep(self.interval_seconds)

    def start(self) -> None:
        if self.manager.registry_path is not None and self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

from __future__ import annotations

import json
import logging
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError

from ..adapters.factory import AdapterRegistry, build_runner
from ..config import Candidate, EngineConfig, GpuGroupConfig, ModelDefinition, Registry, SystemConfig, default_backup_path
from ..errors import (
    AmbiguousNameError,
    ConflictError,
    GpuGroupBusyError,
    LeaseConflictError,
    ModelManagerError,
    NotFoundError,
    RegistryValidationError,
    ReloadUnavailableError,
)
from .state import StateStore

logger = logging.getLogger("modelmanager.manager")

DEFAULT_LEASE_TTL_SECONDS = 300


@dataclass
class EnsureResult:
    host: str
    port: int
    candidate_index: int


@dataclass
class AcquireResult:
    host: str
    port: int
    candidate_index: int
    lease_id: str
    expires_in: float


class Manager:
    def __init__(self, registry: Registry, state: StateStore | None = None, registry_path: Path | None = None):
        self.registry = registry
        self.state = state or StateStore()
        self.adapters = AdapterRegistry(registry)
        self.registry_path = registry_path
        self._write_lock = threading.Lock()
        # Serializes every start/evict/swap decision across the whole
        # coordinator (not per-model/per-gpu_group - see _ensure_loaded's
        # docstring) so two concurrent ensure() calls can never double-start a
        # process, double-evict an occupant, or race a port choice.
        self._ensure_lock = threading.Lock()
        self._known_stat = self._stat_key()
        # Bumped by every successful reload_from_disk() (manual or via
        # RegistryWatcher) so a caller like the bar-widget plugin can tell a
        # reload actually happened - e.g. to flash a "definitions refreshed"
        # toast - without having to diff the registry contents itself.
        self.reload_count = 0
        self.last_reload_at: datetime | None = None
        # Set whenever an automatic (RegistryWatcher) or manual reload fails
        # validation, cleared on the next successful one - so a poller can
        # show/blink a persistent error instead of just the one-shot
        # exception a manual POST /v1/config/reload would have surfaced.
        # Also set directly by app.py's lifespan() when registry.json was
        # already broken at process startup (see load_registry_or_empty) -
        # same field, since it's the same "still broken" state either way.
        self.last_reload_error: str | None = None

    def _stat_key(self) -> tuple[float, int] | None:
        # (mtime, size) rather than mtime alone, so a same-second rewrite (two
        # quick hand edits, or a script regenerating the file) isn't missed
        # just because the filesystem's mtime resolution is coarser than that.
        if self.registry_path is None:
            return None
        try:
            st = self.registry_path.stat()
            return (st.st_mtime, st.st_size)
        except FileNotFoundError:
            return None

    def registry_changed_on_disk(self) -> bool:
        """True if registry.json differs on disk from what this Manager last
        read or wrote itself - i.e. someone hand-edited it since."""
        return self._stat_key() != self._known_stat

    def resolve_name(self, name: str) -> str:
        """An exact model name always wins outright. Otherwise, a caller may
        give any case-insensitive substring of exactly one model's name (e.g.
        'agent' for 'agentflow-planner-3b') and get that one back - so CLI/API
        callers don't have to type a full, often-long model name. Raises
        NotFoundError for zero matches, AmbiguousNameError (->409) for more
        than one."""
        if name in self.registry.models:
            return name
        needle = name.lower()
        matches = sorted(n for n in self.registry.models if needle in n.lower())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousNameError(f"{name!r} matches multiple models: {', '.join(matches)}")
        raise NotFoundError(f"unknown model {name!r}")

    def ensure(self, name: str) -> EnsureResult:
        name = self.resolve_name(name)
        candidate, idx = self._ensure_loaded(name)
        self.state.touch(name)
        return self._result(name, candidate, idx)

    def acquire(self, name: str, ttl_seconds: float | None = None) -> AcquireResult:
        """Like ensure(), but also takes a time-boxed lease marking the model
        as actively in use: while any lease is held, it can never be
        auto-evicted or swapped out for another model, regardless of
        idle_timeout_seconds. Call release() when done, or let the lease
        expire (a safety net for a caller that crashes without releasing)."""
        name = self.resolve_name(name)
        candidate, idx = self._ensure_loaded(name)
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_LEASE_TTL_SECONDS
        lease_id = uuid.uuid4().hex
        self.state.get(name).add_lease(lease_id, ttl)
        self.state.touch(name)
        result = self._result(name, candidate, idx)
        return AcquireResult(host=result.host, port=result.port, candidate_index=idx, lease_id=lease_id, expires_in=ttl)

    def release(self, name: str, lease_id: str | None = None) -> dict:
        name = self.resolve_name(name)
        model_state = self.state.get(name)
        if lease_id is None:
            released = model_state.clear_leases()
        else:
            released = 1 if model_state.remove_lease(lease_id) else 0
        if released:
            self.state.touch(name)  # idle countdown starts from the moment of release, not the original acquire
        return {"released": released, "remaining_leases": len(model_state.leases)}

    def _ensure_loaded(self, name: str) -> tuple[Candidate, int]:
        definition = self.registry.models.get(name)
        if definition is None:
            raise NotFoundError(f"unknown model {name!r}")

        # Ensured outside _ensure_lock: each dependency's own ensure() takes
        # the lock itself (threading.Lock isn't reentrant), and a dependency
        # must be fully settled before this model's own decision is made, not
        # merely queued behind it.
        for dep in definition.depends_on:
            self.ensure(dep)

        with self._ensure_lock:
            model_state = self.state.get(name)
            if model_state.is_active:
                candidate = definition.candidates[model_state.active_candidate_index]
                adapter = self.adapters.get(candidate.engine)
                status = adapter.status(name, candidate)
                if status.running:
                    return candidate, model_state.active_candidate_index
                self.state.set_active(name, None)

            last_error: str | None = None
            for idx, candidate in enumerate(definition.candidates):
                occupant = self.state.find_active_in_gpu_group(candidate.gpu_group, self.registry)
                if occupant is not None and occupant != name:
                    occupant_def = self.registry.models[occupant]
                    occupant_state = self.state.get(occupant)
                    if occupant_def.pinned:
                        last_error = f"gpu_group {candidate.gpu_group!r} occupied by pinned model {occupant!r}"
                        continue
                    if occupant_state.is_busy():
                        last_error = (
                            f"gpu_group {candidate.gpu_group!r} is in active use by {occupant!r} "
                            f"({len(occupant_state.leases)} lease(s) held) - release it first"
                        )
                        continue
                    idle_timeout = occupant_def.idle_timeout_seconds
                    idle_secs = occupant_state.idle_seconds()
                    # No configured timeout means "never evicted by the proactive
                    # idle sweep" (see idle_timeout_seconds' docstring), not "never
                    # evictable at all" - with nothing to time against, this
                    # occupant yields the instant something else actually needs
                    # its gpu_group, same as it would after idle_timeout_seconds
                    # elapsed if one were set.
                    free_to_evict = (
                        idle_timeout is None or (idle_secs is not None and idle_secs >= idle_timeout)
                    )
                    if not free_to_evict:
                        last_error = (
                            f"gpu_group {candidate.gpu_group!r} busy with {occupant!r} "
                            f"(idle {idle_secs or 0:.0f}s, needs {idle_timeout}s to free)"
                        )
                        continue
                    self.stop(occupant)

                adapter = self.adapters.get(candidate.engine)
                actual_port = adapter.start(name, candidate)
                if actual_port != candidate.port:
                    logger.info(
                        "model %r started on port %d (declared port %d was already taken by a "
                        "concurrently-active model)", name, actual_port, candidate.port,
                    )
                self.state.set_active(name, idx, port=actual_port)
                return candidate, idx

            raise GpuGroupBusyError(last_error or f"no available candidate for {name!r}")

    def dry_run(self, name: str) -> dict:
        """Read-only preview of what ensure(name) would do right now: which
        candidate it would pick (or keep), whether that requires evicting
        another model, or why it's currently blocked - without starting,
        stopping, or persisting anything. Deliberately a separate read path
        from _ensure_loaded() (not a shared helper) so this can never
        accidentally take a real action; it mirrors that method's selection
        logic by hand. Recurses into depends_on first (safe - no cycles are
        possible, see Registry._cross_references)."""
        name = self.resolve_name(name)
        definition = self.registry.models[name]
        deps = [self.dry_run(dep) for dep in definition.depends_on]

        model_state = self.state.get(name)
        if model_state.is_active:
            candidate = definition.candidates[model_state.active_candidate_index]
            adapter = self.adapters.get(candidate.engine)
            status = adapter.status(name, candidate)
            if status.running:
                return self._dry_run_result(name, "keep", candidate, model_state.active_candidate_index, None, None, deps)

        last_error: str | None = None
        for idx, candidate in enumerate(definition.candidates):
            occupant = self.state.find_active_in_gpu_group(candidate.gpu_group, self.registry)
            if occupant is not None and occupant != name:
                occupant_def = self.registry.models[occupant]
                occupant_state = self.state.get(occupant)
                if occupant_def.pinned:
                    last_error = f"gpu_group {candidate.gpu_group!r} occupied by pinned model {occupant!r}"
                    continue
                if occupant_state.is_busy():
                    last_error = (
                        f"gpu_group {candidate.gpu_group!r} is in active use by {occupant!r} "
                        f"({len(occupant_state.leases)} lease(s) held) - release it first"
                    )
                    continue
                idle_timeout = occupant_def.idle_timeout_seconds
                idle_secs = occupant_state.idle_seconds()
                free_to_evict = idle_timeout is None or (idle_secs is not None and idle_secs >= idle_timeout)
                if not free_to_evict:
                    last_error = (
                        f"gpu_group {candidate.gpu_group!r} busy with {occupant!r} "
                        f"(idle {idle_secs or 0:.0f}s, needs {idle_timeout}s to free)"
                    )
                    continue
                return self._dry_run_result(name, "swap", candidate, idx, occupant, None, deps)
            return self._dry_run_result(name, "start", candidate, idx, None, None, deps)

        return self._dry_run_result(name, "blocked", None, None, None, last_error or f"no available candidate for {name!r}", deps)

    def _dry_run_result(
        self,
        name: str,
        action: str,
        candidate: Candidate | None,
        idx: int | None,
        evicts: str | None,
        blocked_reason: str | None,
        deps: list[dict],
    ) -> dict:
        result = {
            "model": name,
            "action": action,
            "candidate_index": idx,
            "engine": None,
            "gpu_group": None,
            "host": None,
            "port": None,
            "evicts": evicts,
            "blocked_reason": blocked_reason,
            "files_ok": True,
            "missing_paths": [],
            "depends_on": deps,
        }
        if candidate is None:
            return result
        engine = self.registry.engines[candidate.engine]
        system = self.registry.systems[engine.system]
        result.update(
            {
                "engine": candidate.engine,
                "gpu_group": candidate.gpu_group,
                "host": system.resolved_host(),
                "port": candidate.port,
            }
        )
        missing = []
        if engine.binary and not self._path_exists(system, engine.binary):
            missing.append(engine.binary)
        if candidate.model_path and not self._path_exists(system, candidate.model_path):
            missing.append(candidate.model_path)
        result["files_ok"] = not missing
        result["missing_paths"] = missing
        return result

    def _path_exists(self, system: SystemConfig, path: str) -> bool:
        """Whether a file path exists on the given system - local filesystem
        check, or a one-off remote check reusing the same Runner adapters
        already build from a SystemConfig (adapters.factory.build_runner).
        Used by dry_run()/doctor() to confirm a candidate's binary/model file
        is actually there, not just that the registry JSON is well-formed."""
        if system.control == "local":
            return Path(path).exists()
        runner = build_runner(system)
        try:
            if system.os == "windows":
                escaped = path.replace("'", "''")
                result = runner.run_shell(
                    f"powershell -NoProfile -Command \"if (Test-Path -LiteralPath '{escaped}') "
                    f"{{exit 0}} else {{exit 1}}\"",
                    timeout=15,
                )
            else:
                result = runner.run_shell(f"test -e {shlex.quote(path)}", timeout=15)
            return result.ok
        except ModelManagerError:
            return False

    def dry_run_group(self, name: str) -> dict:
        results = []
        for model_name in self._group_names(name):
            try:
                results.append(self.dry_run(model_name))
            except ModelManagerError as exc:
                results.append({"model": model_name, "action": "error", "detail": str(exc)})
        return {"group": name, "results": results}

    def check_system_reachable(self, system_name: str) -> dict:
        system = self.registry.systems[system_name]
        if system.control == "local":
            return {"system": system_name, "ok": True, "detail": None}
        try:
            result = build_runner(system).run_shell("echo ok", timeout=10)
            return {"system": system_name, "ok": result.ok, "detail": None if result.ok else result.stderr}
        except ModelManagerError as exc:
            return {"system": system_name, "ok": False, "detail": str(exc)}

    def doctor(self) -> dict:
        """High-level health check: every system's reachability, plus a
        dry_run() of every model to confirm its selected candidate's
        binary/model files actually exist and report what ensure() would
        currently do. Nothing here starts, stops, or changes anything."""
        systems = [self.check_system_reachable(name) for name in self.registry.systems]
        models = []
        for name in self.registry.models:
            try:
                models.append(self.dry_run(name))
            except ModelManagerError as exc:
                models.append({"model": name, "action": "error", "detail": str(exc)})
        ok = all(s["ok"] for s in systems) and all(
            m.get("action") != "error" and m.get("files_ok", True) for m in models
        )
        return {"ok": ok, "systems": systems, "models": models}

    @staticmethod
    def validate_registry(data: dict) -> dict:
        """Validates a full registry document (same shape as GET /v1/config or
        registry.json itself) without applying it to live state - a peer to
        the validation that already runs implicitly inside every upsert/
        reload, exposed standalone so a candidate registry.json can be
        checked before it's saved or reloaded. pydantic's ValidationError
        already aggregates every field error in one pass (exc.errors()), so
        every problem is reported together rather than just the first."""
        try:
            Registry.model_validate(data)
        except ValidationError as exc:
            errors = [{"loc": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]
            return {"ok": False, "errors": errors}
        return {"ok": True, "errors": []}

    def stop(self, name: str, force: bool = False) -> None:
        name = self.resolve_name(name)
        definition = self.registry.models[name]
        model_state = self.state.get(name)
        if not model_state.is_active:
            return
        if not force and model_state.is_busy():
            raise LeaseConflictError(
                f"{name!r} has {len(model_state.leases)} active lease(s); pass force=true to stop anyway"
            )
        candidate = definition.candidates[model_state.active_candidate_index]
        adapter = self.adapters.get(candidate.engine)
        adapter.stop(name, candidate)
        self.state.set_active(name, None)

    def status(self, name: str) -> dict:
        name = self.resolve_name(name)
        definition = self.registry.models[name]
        model_state = self.state.get(name)
        entry = {
            "name": name,
            "pinned": definition.pinned,
            "idle_timeout_seconds": definition.idle_timeout_seconds,
            "active": model_state.is_active,
            "last_used": model_state.last_used.isoformat() if model_state.last_used else None,
            "idle_seconds": model_state.idle_seconds(),
            "in_use": model_state.is_busy(),
            "lease_count": len(model_state.leases),
            # Every gpu_group any candidate could land in, always populated (not
            # just while active) so callers can see which models compete for the
            # same slot before anything is actually running.
            "gpu_groups": list(dict.fromkeys(c.gpu_group for c in definition.candidates)),
            "candidate": None,
        }
        if model_state.is_active:
            candidate = definition.candidates[model_state.active_candidate_index]
            entry["candidate"] = {
                "engine": candidate.engine,
                "gpu_group": candidate.gpu_group,
                "host": self.registry.systems[self.registry.engines[candidate.engine].system].resolved_host(),
                "port": model_state.active_port if model_state.active_port is not None else candidate.port,
            }
        return entry

    def list_models(self) -> list[dict]:
        return [self.status(name) for name in self.registry.models]

    def verify(self, name: str, timeout: float = 30.0) -> dict:
        """Push a tiny real inference request straight at the engine's own
        endpoint (ensuring it's loaded first). status()/list_models() only
        prove the process is running and listening on its port - not that the
        model actually loaded correctly or the engine's internals aren't
        wedged - so this is the only check that proves end-to-end liveness."""
        name = self.resolve_name(name)
        definition = self.registry.models[name]
        result = self.ensure(name)
        candidate = definition.candidates[result.candidate_index]
        engine = self.registry.engines[candidate.engine]
        base = f"http://{result.host}:{result.port}"
        info = {
            "name": name,
            "engine": candidate.engine,
            "engine_type": engine.type,
            "host": result.host,
            "port": result.port,
        }

        if candidate.health_check is not None:
            check = candidate.health_check
            path, body = check.path, check.body
            method = check.method
        elif engine.type == "ollama":
            method = "POST"
            if candidate.ollama_task == "embed":
                path, body = "/api/embed", {"model": candidate.model_ref, "input": "modelmanager-verify"}
            else:
                path, body = "/api/generate", {
                    "model": candidate.model_ref,
                    "prompt": "Reply with OK.",
                    "stream": False,
                    "options": {"num_predict": 8},
                }
        elif engine.type == "llama.cpp":
            method = "POST"
            path, body = "/v1/chat/completions", {
                "model": "local",
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
            }
        else:
            return {
                **info,
                "ok": None,
                "latency_ms": None,
                "detail": f"no standard inference API for engine type {engine.type!r} - only confirmed the process is running (set this candidate's health_check to make it verifiable)",
            }

        started = time.monotonic()
        try:
            if method == "GET":
                resp = httpx.get(f"{base}{path}", timeout=timeout)
            else:
                resp = httpx.post(f"{base}{path}", json=body, timeout=timeout)
            latency_ms = round((time.monotonic() - started) * 1000, 1)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return {**info, "ok": False, "latency_ms": round((time.monotonic() - started) * 1000, 1), "detail": str(exc)}
        return {**info, "ok": True, "latency_ms": latency_ms, "detail": None}

    def _group_names(self, name: str) -> list[str]:
        if name not in self.registry.groups:
            raise NotFoundError(f"unknown group {name!r}")
        return self.registry.groups[name]

    def ensure_group(self, name: str) -> dict:
        """Best-effort: ensure() every member model, independently. One
        member's failure (offline box, gpu_group conflict, etc.) never stops
        the rest from being attempted - the point of a group is "start my
        usual set of models", not an all-or-nothing transaction."""
        results = []
        for model_name in self._group_names(name):
            try:
                result = self.ensure(model_name)
                results.append({"name": model_name, "ok": True, "host": result.host, "port": result.port, "detail": None})
            except (ModelManagerError, RuntimeError) as exc:
                results.append({"name": model_name, "ok": False, "host": None, "port": None, "detail": str(exc)})
        return {"group": name, "results": results}

    def verify_group(self, name: str, timeout: float = 30.0) -> dict:
        results = []
        for model_name in self._group_names(name):
            try:
                results.append(self.verify(model_name, timeout=timeout))
            except (ModelManagerError, RuntimeError) as exc:
                results.append({"name": model_name, "ok": False, "latency_ms": None, "detail": str(exc)})
        return {"group": name, "results": results}

    def group_system_models(self, name: str) -> dict[str, list[str]]:
        """Which system each group member is running on (or would run on, if
        not currently active) - same candidate-selection rule as logs()/
        log_location(): the active candidate if running, else the first
        one. Returns {system_name: [model_names]}, since more than one
        member can land on the same box."""
        result: dict[str, list[str]] = {}
        for model_name in self._group_names(name):
            definition = self.registry.models[model_name]
            model_state = self.state.get(model_name)
            idx = model_state.active_candidate_index if model_state.is_active else 0
            candidate = definition.candidates[idx]
            system_name = self.registry.engines[candidate.engine].system
            result.setdefault(system_name, []).append(model_name)
        return result

    def stop_group(self, name: str, force: bool = False) -> dict:
        results = []
        for model_name in self._group_names(name):
            try:
                self.stop(model_name, force=force)
                results.append({"name": model_name, "ok": True, "detail": None})
            except ModelManagerError as exc:
                results.append({"name": model_name, "ok": False, "detail": str(exc)})
        return {"group": name, "results": results}

    def logs(self, name: str, lines: int = 200) -> str:
        name = self.resolve_name(name)
        definition = self.registry.models[name]
        model_state = self.state.get(name)
        idx = model_state.active_candidate_index if model_state.is_active else 0
        candidate = definition.candidates[idx]
        adapter = self.adapters.get(candidate.engine)
        return adapter.logs(name, candidate, lines)

    def log_location(self, name: str) -> dict:
        """Where a model's logs actually live, for a caller that wants to
        tail them directly (e.g. `ssh <host> tail -f <paths...>`) instead of
        polling GET .../logs. `paths` is empty when there's no discrete file
        to point at (an externally-managed ollama service, logged via
        journalctl only); it can have more than one entry - a remote Windows
        launch keeps stdout/stderr as separate files, and the caller needs
        both to see everything."""
        name = self.resolve_name(name)
        definition = self.registry.models[name]
        model_state = self.state.get(name)
        idx = model_state.active_candidate_index if model_state.is_active else 0
        candidate = definition.candidates[idx]
        engine = self.registry.engines[candidate.engine]
        system = self.registry.systems[engine.system]
        adapter = self.adapters.get(candidate.engine)
        return {
            "system": engine.system,
            "control": system.control,
            "os": system.os,
            "ssh_host": system.ssh_host,
            "paths": adapter.log_paths(name, candidate),
        }

    def reconcile_running_state(self) -> dict[str, int]:
        """Called once at coordinator startup, before any pinned model is
        auto-started: this Manager's state is always empty on a fresh
        process, but a model's actual process/server can easily have
        survived the restart - a non-pinned llama.cpp process just keeps
        running unmanaged, and an Ollama model stays loaded server-side
        either way. Without this, the first ensure() on such a model
        wouldn't recognize it's already running and would try to launch a
        *second* instance on the same port - an instant bind-conflict crash
        for a local llama.cpp process, and it leaves the original orphaned:
        untracked by /v1/models, the idle sweep, or the health-check
        restarter, since as far as this Manager knows nothing is active.

        Checks every candidate of every model (not just the first) since the
        one that was actually running before restart isn't known yet; stops
        at the first one whose adapter reports it running. A status check
        that errors (e.g. an offline SSH box) is treated as "not this one"
        and skipped rather than aborting the whole reconciliation."""
        reattached: dict[str, int] = {}
        for name, definition in self.registry.models.items():
            if self.state.get(name).is_active:
                continue
            for idx, candidate in enumerate(definition.candidates):
                adapter = self.adapters.get(candidate.engine)
                try:
                    status = adapter.status(name, candidate)
                except Exception:
                    continue
                if status.running:
                    self.state.set_active(name, idx, port=status.port)
                    reattached[name] = idx
                    break
        return reattached

    def probe_busy(self, name: str) -> bool | None:
        """Ask the engine whether this model has work in flight right now.

        Resolves the same host/port we hand out from ensure() and lets the
        adapter answer; None when the model isn't running, the engine has no
        activity signal, or the probe failed. Never raises - a probe is an
        optimisation over the call-based idle clock, so a broken one must
        degrade to "no opinion" rather than break the sweep.
        """
        state = self.state.get(name)
        if state.active_candidate_index is None:
            return None
        definition = self.registry.models.get(name)
        if definition is None:
            return None
        try:
            candidate = definition.candidates[state.active_candidate_index]
            system = self.registry.systems[self.registry.engines[candidate.engine].system]
            port = state.active_port if state.active_port is not None else candidate.port
            return self.adapters.get(candidate.engine).busy(name, candidate, system.resolved_host(), port)
        except Exception as exc:
            logger.warning("activity probe for %s failed: %s", name, exc)
            return None

    def _result(self, name: str, candidate, idx: int) -> EnsureResult:
        system = self.registry.systems[self.registry.engines[candidate.engine].system]
        active_port = self.state.get(name).active_port
        port = active_port if active_port is not None else candidate.port
        return EnsureResult(host=system.resolved_host(), port=port, candidate_index=idx)

    def upsert_system(self, name: str, data: dict) -> SystemConfig:
        return self._upsert("systems", name, data, on_commit=lambda: self.adapters.clear())

    def upsert_engine(self, name: str, data: dict) -> EngineConfig:
        return self._upsert("engines", name, data, on_commit=lambda: self.adapters.invalidate(name))

    def upsert_gpu_group(self, name: str, data: dict) -> GpuGroupConfig:
        return self._upsert("gpu_groups", name, data)

    def upsert_model(self, name: str, data: dict) -> ModelDefinition:
        return self._upsert("models", name, data)

    def upsert_group(self, name: str, data: list[str]) -> list[str]:
        return self._upsert("groups", name, data)

    def _upsert(self, section: str, name: str, data: dict, on_commit=None):
        with self._write_lock:
            proposed = self.registry.model_dump(mode="json")
            proposed[section][name] = data
            try:
                new_registry = Registry.model_validate(proposed)
            except ValidationError as exc:
                raise RegistryValidationError(str(exc)) from exc
            getattr(self.registry, section)[name] = getattr(new_registry, section)[name]
            if on_commit is not None:
                on_commit()
            self._persist(new_registry)
            return getattr(self.registry, section)[name]

    def reload_from_disk(self) -> dict:
        """Re-read registry.json (e.g. after a hand edit, including a delete)
        and apply it to the live coordinator without a restart: stops any
        currently-active model whose definition was actually removed, swaps
        in the new systems/engines/gpu_groups/models, drops all cached
        adapters so changed engine/system config takes effect immediately,
        then auto-starts any newly-pinned model that isn't already running.

        A model that's active and was renamed (not removed) is a special
        case: its old key disappearing looks identical to a deletion, but
        force-stopping it and leaving the new key with no memory of it would
        kill a perfectly healthy process just because the JSON changed its
        name. Detected by an exact candidates match against a new, unknown
        name; when found, the running state (active candidate, last_used,
        leases) is carried over to the new name instead of being stopped."""
        if self.registry_path is None:
            raise ReloadUnavailableError("no registry_path configured for this coordinator instance")

        with self._write_lock:
            if not self.registry_path.exists():
                self.last_reload_error = f"registry file not found at {self.registry_path}"
                raise RegistryValidationError(self.last_reload_error)
            try:
                data = json.loads(self.registry_path.read_text())
                new_registry = Registry.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                self.last_reload_error = str(exc)
                raise RegistryValidationError(str(exc)) from exc
            self._known_stat = self._stat_key()

            old_only = set(self.registry.models) - set(new_registry.models)
            new_only = set(new_registry.models) - set(self.registry.models)

            renamed: dict[str, str] = {}
            for old_name in list(old_only):
                if not self.state.get(old_name).is_active:
                    continue
                old_candidates = self.registry.models[old_name].candidates
                matches = [n for n in new_only if new_registry.models[n].candidates == old_candidates]
                if len(matches) == 1:
                    new_name = matches[0]
                    renamed[old_name] = new_name
                    old_only.discard(old_name)
                    new_only.discard(new_name)

            stopped = []
            for name in self.registry.models:  # preserve deterministic order over the (unordered) old_only set
                if name in old_only and self.state.get(name).is_active:
                    self.stop(name, force=True)  # its definition is actually gone; no lease can hold it open
                    stopped.append(name)

            for section in ("systems", "engines", "gpu_groups", "models"):
                target = getattr(self.registry, section)
                target.clear()
                target.update(getattr(new_registry, section))
            self.adapters.clear()

            for old_name, new_name in renamed.items():
                self.state.rename(old_name, new_name)

            started = []
            for name, definition in self.registry.models.items():
                if definition.pinned and not self.state.get(name).is_active:
                    try:
                        self.ensure(name)
                        started.append(name)
                    except Exception:
                        pass

            self.reload_count += 1
            self.last_reload_at = datetime.now(timezone.utc)
            self.last_reload_error = None
            self.save_backup(new_registry)

        return {
            "stopped": stopped,
            "renamed": renamed,
            "started_pinned": started,
            "systems": list(self.registry.systems),
            "engines": list(self.registry.engines),
            "gpu_groups": list(self.registry.gpu_groups),
            "models": list(self.registry.models),
        }

    def reload_status(self) -> dict:
        return {
            "reload_count": self.reload_count,
            "last_reload_at": self.last_reload_at.isoformat() if self.last_reload_at else None,
            "last_reload_error": self.last_reload_error,
        }

    def _persist(self, new_registry: Registry) -> None:
        if self.registry_path is None:
            return
        data = new_registry.model_dump(mode="json")
        tmp_path = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2) + "\n")
        tmp_path.replace(self.registry_path)
        self._known_stat = self._stat_key()
        self.save_backup(new_registry)

    def save_backup(self, registry: Registry | None = None) -> None:
        """Snapshots a known-good registry to default_backup_path(), so a
        later cold start that finds registry.json broken can fall back to
        this instead of an empty registry (see load_registry_or_empty() in
        app.py). Called after every successful upsert/reload, and once at
        startup if the live file happened to load cleanly on its own.
        Best-effort - a failure here must never fail the upsert/reload that
        triggered it."""
        if self.registry_path is None:
            return
        try:
            data = (registry or self.registry).model_dump(mode="json")
            backup_path = default_backup_path(self.registry_path)
            tmp_path = backup_path.with_suffix(backup_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(data, indent=2) + "\n")
            tmp_path.replace(backup_path)
        except OSError:
            logger.warning("failed to write last-known-good registry backup", exc_info=True)

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ..adapters.factory import AdapterRegistry
from ..config import Candidate, EngineConfig, GpuGroupConfig, ModelDefinition, Registry, SystemConfig
from .state import StateStore

DEFAULT_LEASE_TTL_SECONDS = 300


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class RegistryValidationError(Exception):
    """A proposed registry write failed validation; nothing was changed."""


class ReloadUnavailableError(Exception):
    """This Manager has no registry_path, so there is nothing to reload from."""


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

    def ensure(self, name: str) -> EnsureResult:
        candidate, idx = self._ensure_loaded(name)
        self.state.touch(name)
        return self._result(candidate, idx)

    def acquire(self, name: str, ttl_seconds: float | None = None) -> AcquireResult:
        """Like ensure(), but also takes a time-boxed lease marking the model
        as actively in use: while any lease is held, it can never be
        auto-evicted or swapped out for another model, regardless of
        idle_timeout_seconds. Call release() when done, or let the lease
        expire (a safety net for a caller that crashes without releasing)."""
        candidate, idx = self._ensure_loaded(name)
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_LEASE_TTL_SECONDS
        lease_id = uuid.uuid4().hex
        self.state.get(name).add_lease(lease_id, ttl)
        self.state.touch(name)
        result = self._result(candidate, idx)
        return AcquireResult(host=result.host, port=result.port, candidate_index=idx, lease_id=lease_id, expires_in=ttl)

    def release(self, name: str, lease_id: str | None = None) -> dict:
        if name not in self.registry.models:
            raise NotFoundError(f"unknown model {name!r}")
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
                free_to_evict = (
                    idle_timeout is not None and idle_secs is not None and idle_secs >= idle_timeout
                )
                if not free_to_evict:
                    last_error = (
                        f"gpu_group {candidate.gpu_group!r} busy with {occupant!r} "
                        f"(idle {idle_secs or 0:.0f}s, needs {idle_timeout}s to free)"
                    )
                    continue
                self.stop(occupant)

            adapter = self.adapters.get(candidate.engine)
            adapter.start(name, candidate)
            self.state.set_active(name, idx)
            return candidate, idx

        raise ConflictError(last_error or f"no available candidate for {name!r}")

    def stop(self, name: str, force: bool = False) -> None:
        definition = self.registry.models.get(name)
        if definition is None:
            raise NotFoundError(f"unknown model {name!r}")
        model_state = self.state.get(name)
        if not model_state.is_active:
            return
        if not force and model_state.is_busy():
            raise ConflictError(
                f"{name!r} has {len(model_state.leases)} active lease(s); pass force=true to stop anyway"
            )
        candidate = definition.candidates[model_state.active_candidate_index]
        adapter = self.adapters.get(candidate.engine)
        adapter.stop(name, candidate)
        self.state.set_active(name, None)

    def status(self, name: str) -> dict:
        definition = self.registry.models.get(name)
        if definition is None:
            raise NotFoundError(f"unknown model {name!r}")
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
                "port": candidate.port,
            }
        return entry

    def list_models(self) -> list[dict]:
        return [self.status(name) for name in self.registry.models]

    def logs(self, name: str, lines: int = 200) -> str:
        definition = self.registry.models.get(name)
        if definition is None:
            raise NotFoundError(f"unknown model {name!r}")
        model_state = self.state.get(name)
        idx = model_state.active_candidate_index if model_state.is_active else 0
        candidate = definition.candidates[idx]
        adapter = self.adapters.get(candidate.engine)
        return adapter.logs(name, candidate, lines)

    def _result(self, candidate, idx: int) -> EnsureResult:
        system = self.registry.systems[self.registry.engines[candidate.engine].system]
        return EnsureResult(host=system.resolved_host(), port=candidate.port, candidate_index=idx)

    def upsert_system(self, name: str, data: dict) -> SystemConfig:
        return self._upsert("systems", name, data, on_commit=lambda: self.adapters.clear())

    def upsert_engine(self, name: str, data: dict) -> EngineConfig:
        return self._upsert("engines", name, data, on_commit=lambda: self.adapters.invalidate(name))

    def upsert_gpu_group(self, name: str, data: dict) -> GpuGroupConfig:
        return self._upsert("gpu_groups", name, data)

    def upsert_model(self, name: str, data: dict) -> ModelDefinition:
        return self._upsert("models", name, data)

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
                raise RegistryValidationError(f"registry file not found at {self.registry_path}")
            try:
                data = json.loads(self.registry_path.read_text())
                new_registry = Registry.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise RegistryValidationError(str(exc)) from exc

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

        return {
            "stopped": stopped,
            "renamed": renamed,
            "started_pinned": started,
            "systems": list(self.registry.systems),
            "engines": list(self.registry.engines),
            "gpu_groups": list(self.registry.gpu_groups),
            "models": list(self.registry.models),
        }

    def _persist(self, new_registry: Registry) -> None:
        if self.registry_path is None:
            return
        data = new_registry.model_dump(mode="json")
        tmp_path = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2) + "\n")
        tmp_path.replace(self.registry_path)

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.factory import AdapterRegistry
from ..config import Registry
from .state import StateStore


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


@dataclass
class EnsureResult:
    host: str
    port: int
    candidate_index: int


class Manager:
    def __init__(self, registry: Registry, state: StateStore | None = None):
        self.registry = registry
        self.state = state or StateStore()
        self.adapters = AdapterRegistry(registry)

    def ensure(self, name: str) -> EnsureResult:
        definition = self.registry.models.get(name)
        if definition is None:
            raise NotFoundError(f"unknown model {name!r}")

        model_state = self.state.get(name)
        if model_state.is_active:
            candidate = definition.candidates[model_state.active_candidate_index]
            adapter = self.adapters.get(candidate.engine)
            status = adapter.status(name, candidate)
            if status.running:
                self.state.touch(name)
                return self._result(candidate, model_state.active_candidate_index)
            self.state.set_active(name, None)

        last_error: str | None = None
        for idx, candidate in enumerate(definition.candidates):
            occupant = self.state.find_active_in_gpu_group(candidate.gpu_group, self.registry)
            if occupant is not None and occupant != name:
                occupant_def = self.registry.models[occupant]
                if occupant_def.pinned:
                    last_error = f"gpu_group {candidate.gpu_group!r} occupied by pinned model {occupant!r}"
                    continue
                occupant_state = self.state.get(occupant)
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
            return self._result(candidate, idx)

        raise ConflictError(last_error or f"no available candidate for {name!r}")

    def stop(self, name: str) -> None:
        definition = self.registry.models.get(name)
        if definition is None:
            raise NotFoundError(f"unknown model {name!r}")
        model_state = self.state.get(name)
        if not model_state.is_active:
            return
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

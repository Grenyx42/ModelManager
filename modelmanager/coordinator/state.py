from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ModelState:
    active_candidate_index: int | None = None
    # The port actually in use for the active candidate - usually equal to
    # that candidate's declared Candidate.port, but a llama.cpp/process
    # candidate can be live-resolved to a different one if the declared port
    # was already taken by another concurrently-active model (see
    # port_check.resolve_port). None means "trust candidate.port".
    active_port: int | None = None
    last_used: datetime | None = None
    # lease_id -> monotonic expiry time. A lease is a caller's claim that it is
    # actively using this model right now; while any lease is unexpired the
    # model is "busy" and must never be auto-evicted or swapped out, no matter
    # what idle_timeout_seconds says.
    leases: dict[str, float] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.active_candidate_index is not None

    def idle_seconds(self) -> float | None:
        if self.last_used is None:
            return None
        return (datetime.now(timezone.utc) - self.last_used).total_seconds()

    def purge_expired_leases(self) -> None:
        now = time.monotonic()
        expired = [lease_id for lease_id, expires_at in self.leases.items() if expires_at <= now]
        for lease_id in expired:
            del self.leases[lease_id]

    def is_busy(self) -> bool:
        self.purge_expired_leases()
        return bool(self.leases)

    def add_lease(self, lease_id: str, ttl_seconds: float) -> None:
        self.leases[lease_id] = time.monotonic() + ttl_seconds

    def remove_lease(self, lease_id: str) -> bool:
        return self.leases.pop(lease_id, None) is not None

    def clear_leases(self) -> int:
        count = len(self.leases)
        self.leases.clear()
        return count


class StateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._models: dict[str, ModelState] = {}

    def get(self, name: str) -> ModelState:
        with self._lock:
            return self._models.setdefault(name, ModelState())

    def set_active(self, name: str, candidate_index: int | None, port: int | None = None) -> None:
        with self._lock:
            state = self._models.setdefault(name, ModelState())
            state.active_candidate_index = candidate_index
            state.active_port = port
            if candidate_index is not None:
                state.last_used = datetime.now(timezone.utc)
            else:
                # No longer running - any outstanding lease against it is stale.
                state.clear_leases()

    def touch(self, name: str) -> None:
        with self._lock:
            state = self._models.setdefault(name, ModelState())
            state.last_used = datetime.now(timezone.utc)

    def find_active_in_gpu_group(self, gpu_group: str, registry) -> str | None:
        with self._lock:
            for name, state in self._models.items():
                if state.active_candidate_index is None:
                    continue
                definition = registry.models.get(name)
                if definition is None:
                    continue
                candidate = definition.candidates[state.active_candidate_index]
                if candidate.gpu_group == gpu_group:
                    return name
        return None

    def rename(self, old_name: str, new_name: str) -> None:
        with self._lock:
            state = self._models.pop(old_name, None)
            if state is not None:
                self._models[new_name] = state

    def all(self) -> dict[str, ModelState]:
        with self._lock:
            return dict(self._models)

    def purge_all_expired_leases(self) -> None:
        with self._lock:
            for state in self._models.values():
                state.purge_expired_leases()

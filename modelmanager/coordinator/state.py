from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ModelState:
    active_candidate_index: int | None = None
    last_used: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.active_candidate_index is not None

    def idle_seconds(self) -> float | None:
        if self.last_used is None:
            return None
        return (datetime.now(timezone.utc) - self.last_used).total_seconds()


class StateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._models: dict[str, ModelState] = {}

    def get(self, name: str) -> ModelState:
        with self._lock:
            return self._models.setdefault(name, ModelState())

    def set_active(self, name: str, candidate_index: int | None) -> None:
        with self._lock:
            state = self._models.setdefault(name, ModelState())
            state.active_candidate_index = candidate_index
            if candidate_index is not None:
                state.last_used = datetime.now(timezone.utc)

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

    def all(self) -> dict[str, ModelState]:
        with self._lock:
            return dict(self._models)

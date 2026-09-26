from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config import Candidate


@dataclass
class AdapterStatus:
    running: bool
    pid: int | None = None
    detail: str | None = None


class EngineAdapter(ABC):
    """Starts/stops/inspects one candidate backend for a model definition."""

    @abstractmethod
    def start(self, definition_name: str, candidate: Candidate) -> None:
        ...

    @abstractmethod
    def stop(self, definition_name: str, candidate: Candidate) -> None:
        ...

    @abstractmethod
    def status(self, definition_name: str, candidate: Candidate) -> AdapterStatus:
        ...

    @abstractmethod
    def logs(self, definition_name: str, candidate: Candidate, lines: int = 200) -> str:
        ...

    @abstractmethod
    def log_path(self, definition_name: str, candidate: Candidate) -> str | None:
        """Absolute path to this candidate's log file on its own system (native
        path style - Windows backslashes included), or None if there isn't a
        discrete file to point at (e.g. an externally-managed ollama service
        whose logs only exist in the system journal)."""
        ...

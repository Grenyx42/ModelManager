from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config import Candidate


@dataclass
class AdapterStatus:
    running: bool
    pid: int | None = None
    port: int | None = None
    detail: str | None = None


class EngineAdapter(ABC):
    """Starts/stops/inspects one candidate backend for a model definition."""

    @abstractmethod
    def start(self, definition_name: str, candidate: Candidate) -> int:
        """Starts the candidate and returns the port it's actually reachable
        on - usually candidate.port unchanged, but llama.cpp/process adapters
        may resolve a different one live if candidate.port was already taken
        by another concurrently-active model (see port_check.resolve_port)."""
        ...

    @abstractmethod
    def stop(self, definition_name: str, candidate: Candidate) -> None:
        ...

    @abstractmethod
    def status(self, definition_name: str, candidate: Candidate) -> AdapterStatus:
        ...

    def busy(self, definition_name: str, candidate: Candidate, host: str, port: int) -> bool | None:
        """Whether the engine has work in flight for this model *right now*.

        This is what lets idle_timeout_seconds mean "time since the model last
        did anything" rather than "time since someone last called us". We hand
        callers a host:port and then leave the data path entirely, so traffic
        sent straight to the engine is otherwise invisible to the idle sweep -
        a model can be saturated for ten minutes and still get evicted for
        being "idle" (see Scheduler/ActivityTracker).

        True/False is a real answer; None means "this engine can't tell us",
        which leaves the old call-based behaviour in place rather than
        pretending a silent engine is idle. Default is None so an engine only
        opts in when it has a signal worth trusting.
        """
        return None

    @abstractmethod
    def logs(self, definition_name: str, candidate: Candidate, lines: int = 200) -> str:
        ...

    @abstractmethod
    def log_paths(self, definition_name: str, candidate: Candidate) -> list[str]:
        """Absolute path(s) to this candidate's log file(s) on its own system
        (native path style - Windows backslashes included), for a caller that
        wants to tail them directly. Usually one file (stdout+stderr merged
        for a local process), but a remote Windows launch keeps stdout and
        stderr as two separate files - and since the process being launched
        almost always logs to stderr, a caller only tailing the first path
        would see nothing. Empty when there's no discrete file to point at
        (e.g. an externally-managed ollama service whose logs only exist in
        the system journal)."""
        ...

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(ABC):
    """Executes a command somewhere: locally, or on a remote box over SSH."""

    @abstractmethod
    def run(self, cmd: list[str], timeout: float | None = 30) -> CommandResult:
        ...

    @abstractmethod
    def run_shell(self, cmd: str, timeout: float | None = 30) -> CommandResult:
        ...

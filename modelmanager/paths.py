from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    d = Path(base) / "modelmanager"
    (d / "pids").mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d


def pid_file(definition_name: str) -> Path:
    return state_dir() / "pids" / f"{definition_name}.pid"


def log_file(definition_name: str) -> Path:
    return state_dir() / "logs" / f"{definition_name}.log"


def access_log_file() -> Path:
    return state_dir() / "access.log"

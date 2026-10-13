from __future__ import annotations

import logging
import logging.handlers
import subprocess

from . import paths

LOGGER_NAME = "modelmanager.lifecycle"


def configure() -> logging.Logger:
    """Sets up the lifecycle-log logger once: its own rotated file under
    XDG_STATE_HOME/modelmanager/lifecycle.log, recording model start/crash/
    restart/stop events across every model in one place.

    A model's own per-engine log (paths.log_file / adapter.log_paths) already
    captures that process's raw stdout/stderr, which is where the actual
    crash reason (OOM, a bad CUDA kernel, etc) shows up - but that file gets
    overwritten in spirit every restart with no obvious marker of "here is
    where it died". This log is the other half: a compact, cross-model
    timeline of *when* something crashed and whether the restart worked,
    still readable after the coordinator itself has been restarted, so an
    intermittent crash (e.g. the bonsai models dying under load) leaves a
    trace independent of any single run's own log.

    Checks for our own marked handler specifically, not just "any handler at
    all" - see access_log.configure() for why.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if any(getattr(h, "_modelmanager_lifecycle", False) for h in logger.handlers):
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.handlers.RotatingFileHandler(
        paths.lifecycle_log_file(), maxBytes=5_000_000, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler._modelmanager_lifecycle = True
    logger.addHandler(handler)
    return logger


def record(message: str) -> None:
    logging.getLogger(LOGGER_NAME).info(message)


def tail(lines: int = 200) -> str:
    log_path = paths.lifecycle_log_file()
    if not log_path.exists():
        return ""
    result = subprocess.run(["tail", "-n", str(lines), str(log_path)], capture_output=True, text=True)
    return result.stdout

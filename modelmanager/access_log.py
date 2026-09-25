from __future__ import annotations

import logging
import logging.handlers
import subprocess

from . import paths

LOGGER_NAME = "modelmanager.access"


def configure() -> logging.Logger:
    """Sets up the access-log logger once: its own rotated file under
    XDG_STATE_HOME/modelmanager/access.log, separate from app logs so it's
    easy to tail/ship on its own, and reachable remotely via GET
    /v1/access-log for a caller with no shell on this box.

    Checks for our own marked handler specifically, not just "any handler at
    all" - pytest (and other test/log-capture setups) attach their own
    handler to every logger, which would otherwise look like we'd already
    configured this one.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if any(getattr(h, "_modelmanager_access", False) for h in logger.handlers):
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.handlers.RotatingFileHandler(
        paths.access_log_file(), maxBytes=5_000_000, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler._modelmanager_access = True
    logger.addHandler(handler)
    return logger


def record(client: str, method: str, path: str, status_code: int, duration_ms: float) -> None:
    logging.getLogger(LOGGER_NAME).info(f"{client} {method} {path} -> {status_code} ({duration_ms:.1f}ms)")


def tail(lines: int = 200) -> str:
    log_path = paths.access_log_file()
    if not log_path.exists():
        return ""
    result = subprocess.run(["tail", "-n", str(lines), str(log_path)], capture_output=True, text=True)
    return result.stdout

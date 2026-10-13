from __future__ import annotations

import errno
import socket
from typing import Callable

from .errors import PortInUseError

MAX_PORT_SCAN_ATTEMPTS = 32


def check_local_port_free(port: int, system_name: str) -> None:
    """Pre-flight check right before launching a process that will bind
    `port` locally - turns a real external conflict into an instant, clean
    error instead of discovering it only after the launched process crashes
    (see STARTUP_GRACE_SECONDS in llamacpp.py/process.py). Used where the
    port is fixed/structural (Ollama's own base_url) and can't be remapped -
    see resolve_port() for the llama.cpp/process case, where it can."""
    if not is_local_port_free(port):
        raise PortInUseError(f"port {port} is already in use on {system_name!r}")


def is_local_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            # e.g. PermissionError binding a port <1024 without privilege -
            # not evidence the port is taken, just that this best-effort
            # check couldn't test it.
            return exc.errno != errno.EADDRINUSE
        return True


def resolve_port(is_free: Callable[[int], bool], preferred_port: int, system_name: str) -> int:
    """Try preferred_port first - the common, no-contention case returns
    immediately with no change in behavior - then scan upward; the first
    free port wins. Only raises PortInUseError if nothing in the whole scan
    range is free."""
    for port in range(preferred_port, preferred_port + MAX_PORT_SCAN_ATTEMPTS):
        if is_free(port):
            return port
    raise PortInUseError(
        f"no free port found near {preferred_port} on {system_name!r} after checking {MAX_PORT_SCAN_ATTEMPTS} ports"
    )

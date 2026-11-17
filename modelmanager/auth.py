from __future__ import annotations

import os
import secrets
from pathlib import Path

from .config import default_token_path
from .secret_files import write_secret_file

TOKEN_BYTES = 32


def generate_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def write_token(token: str, path: Path | None = None) -> Path:
    """Writes the REST API bearer token, 0600 the whole time it's visible -
    scripts/setup.sh calls this on first install, `modelmanager auth rotate`
    calls it again any time after. Nothing needs restarting afterward:
    read_token() is read fresh on every request (see
    coordinator.app.install_auth_middleware), not cached at startup.

    This is the *owner* token - full access to this coordinator, and it must
    never be sent to anyone else's coordinator. See known_hosts.py for the
    separate, per-peer token that's safe to hand to someone else's
    coordinator via --peer."""
    path = path or default_token_path()
    write_secret_file(path, token + "\n")
    return path


def read_token(path: Path | None = None) -> str | None:
    """The token both the coordinator (to check incoming requests) and the
    CLI (to send one) should use. MODELMANAGER_TOKEN wins outright (a
    systemd EnvironmentFile or container secret, no file on disk needed);
    otherwise the on-disk token file written by write_token(). None means
    "not configured" - the coordinator treats that as a hard misconfiguration
    (see install_auth_middleware), not "auth disabled"."""
    env_token = os.environ.get("MODELMANAGER_TOKEN")
    if env_token:
        return env_token.strip()
    path = path or default_token_path()
    if not path.exists():
        return None
    text = path.read_text().strip()
    return text or None

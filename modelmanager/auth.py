from __future__ import annotations

import os
import secrets
from pathlib import Path

from .config import default_shared_token_path, default_token_path

TOKEN_BYTES = 32


def generate_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def write_token(token: str, path: Path | None = None) -> Path:
    """Writes the REST API bearer token, 0600 so only this user can read it -
    scripts/setup.sh calls this on first install, `modelmanager auth rotate`
    calls it again any time after. Nothing needs restarting afterward:
    read_token() is read fresh on every request (see
    coordinator.app.install_auth_middleware), not cached at startup."""
    path = path or default_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return path


def read_token(path: Path | None = None) -> str | None:
    """The token both the coordinator (to check incoming requests) and the
    CLI (to send one) should use. MODELMANAGER_TOKEN wins outright (a
    systemd EnvironmentFile or container secret, no file on disk needed);
    otherwise the on-disk token file written by write_token(). None means
    "not configured" - the coordinator treats that as a hard misconfiguration
    (see install_auth_middleware), not "auth disabled".

    This is the *owner* token - full access to this coordinator, and it must
    never be sent anywhere else. See read_shared_token() below for the
    separate value that's safe to hand to someone else's coordinator."""
    env_token = os.environ.get("MODELMANAGER_TOKEN")
    if env_token:
        return env_token.strip()
    path = path or default_token_path()
    if not path.exists():
        return None
    text = path.read_text().strip()
    return text or None


def write_shared_token(token: str, path: Path | None = None) -> Path:
    """Writes the token this machine presents to OTHER people's
    coordinators via `--peer` (see `modelmanager grant add` on their end) -
    0600, same as write_token(). Deliberately a distinct value from the
    owner token: a malicious or compromised peer that captures this one
    (it necessarily sees whatever's in the Authorization header) gains
    nothing by replaying it back at *this* coordinator's own API - it
    matches neither this machine's owner token (a different random value)
    nor, normally, any entry in this machine's own grants.json - unlike the
    owner token, which would grant full access if ever leaked to a peer."""
    path = path or default_shared_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return path


def read_shared_token(path: Path | None = None) -> str | None:
    """MODELMANAGER_SHARED_TOKEN wins outright, otherwise the on-disk file
    written by write_shared_token() - same resolution order as read_token().
    None means "not generated yet" - cli.py's _client() treats that as a
    hard stop for any --peer request rather than silently falling back to
    the owner token, which would defeat the whole point of having a
    separate value."""
    env_token = os.environ.get("MODELMANAGER_SHARED_TOKEN")
    if env_token:
        return env_token.strip()
    path = path or default_shared_token_path()
    if not path.exists():
        return None
    text = path.read_text().strip()
    return text or None

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import default_grants_path


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_grants(path: Path | None = None) -> list[dict]:
    path = path or default_grants_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data.get("grants", [])


def save_grants(grants: list[dict], path: Path | None = None) -> Path:
    """Writes grants.json 0600 (token hashes aren't reversible, but there's no
    reason to let other local users enumerate who's been granted access) -
    atomic replace, same pattern as Manager._persist()'s registry writes.
    Read fresh on every auth check (see coordinator.app.install_auth_middleware)
    so grant add/remove needs no coordinator restart, matching auth.read_token()."""
    path = path or default_grants_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps({"grants": grants}, indent=2) + "\n")
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)
    return path


def add_grant(name: str, token: str, allowed_systems: list[str], path: Path | None = None) -> dict:
    """Overwrites any existing grant with the same name (re-running `grant add`
    for the same person replaces their token/systems rather than duplicating)."""
    grants = [g for g in load_grants(path) if g["name"] != name]
    entry = {
        "name": name,
        "token_hash": hash_token(token),
        "allowed_systems": list(allowed_systems),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    grants.append(entry)
    save_grants(grants, path)
    return entry


def remove_grant(name: str, path: Path | None = None) -> bool:
    grants = load_grants(path)
    remaining = [g for g in grants if g["name"] != name]
    if len(remaining) == len(grants):
        return False
    save_grants(remaining, path)
    return True


def find_grant_by_token(token: str, path: Path | None = None) -> dict | None:
    """Only ever called with a token that already failed the owner-token
    comparison in install_auth_middleware - hashes `token` once and scans,
    fine for the handful of grants a home-lab coordinator will ever have."""
    provided_hash = hash_token(token)
    for grant in load_grants(path):
        if hmac.compare_digest(grant["token_hash"], provided_hash):
            return grant
    return None

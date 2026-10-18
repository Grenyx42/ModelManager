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


def add_grant(name: str, token: str, path: Path | None = None) -> dict:
    """Registers (or re-registers) a person's token under `name`, with no
    system access yet - `allow_system`/`revoke_system` below are how access
    gets granted or taken away afterward, one system at a time, without
    ever having to retype the token. Re-running this for an existing name
    (e.g. they rotated their token) replaces token_hash but preserves
    whatever allowed_systems/created_at were already there - rotating a
    token shouldn't silently wipe someone's existing access."""
    grants = load_grants(path)
    existing = next((g for g in grants if g["name"] == name), None)
    grants = [g for g in grants if g["name"] != name]
    entry = {
        "name": name,
        "token_hash": hash_token(token),
        "allowed_systems": list(existing["allowed_systems"]) if existing else [],
        "created_at": existing["created_at"] if existing else datetime.now(timezone.utc).isoformat(),
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


def allow_system(name: str, system: str, path: Path | None = None) -> dict | None:
    """Adds one system to an existing grant's allowed_systems (idempotent -
    already-allowed is a no-op, not an error). None if `name` isn't a known
    grant (register it with `grant add` first)."""
    grants = load_grants(path)
    for entry in grants:
        if entry["name"] == name:
            if system not in entry["allowed_systems"]:
                entry["allowed_systems"].append(system)
            save_grants(grants, path)
            return entry
    return None


def revoke_system(name: str, system: str, path: Path | None = None) -> dict | None:
    """Removes one system from an existing grant's allowed_systems
    (idempotent - already-absent is a no-op, not an error). None if `name`
    isn't a known grant."""
    grants = load_grants(path)
    for entry in grants:
        if entry["name"] == name:
            if system in entry["allowed_systems"]:
                entry["allowed_systems"].remove(system)
            save_grants(grants, path)
            return entry
    return None


def find_grant_by_token(token: str, path: Path | None = None) -> dict | None:
    """Only ever called with a token that already failed the owner-token
    comparison in install_auth_middleware - hashes `token` once and scans,
    fine for the handful of grants a home-lab coordinator will ever have."""
    provided_hash = hash_token(token)
    for grant in load_grants(path):
        if hmac.compare_digest(grant["token_hash"], provided_hash):
            return grant
    return None

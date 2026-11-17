from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import auth
from .config import default_known_hosts_dir, default_known_hosts_path
from .secret_files import write_secret_file


def load_known_hosts(path: Path | None = None) -> dict:
    path = path or default_known_hosts_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_known_hosts(hosts: dict, path: Path | None = None) -> Path:
    # 0600, atomic, symlink-safe (see secret_files.write_secret_file) - each
    # entry now carries a per-peer bearer token (see add_known_host below),
    # not just public URLs/fingerprints, so this needs the same protection
    # as token/grants.json.
    path = path or default_known_hosts_path()
    write_secret_file(path, json.dumps(hosts, indent=2) + "\n")
    return path


def add_known_host(
    name: str,
    url: str,
    cert_pem: str,
    fingerprint: str,
    path: Path | None = None,
    cert_dir: Path | None = None,
) -> dict:
    """Saves the peer's cert PEM under <cert_dir>/<name>.pem and records the
    entry - called only after the caller has already shown `fingerprint` to
    a human (or they passed --yes) and chosen to trust it. Re-adding an
    existing name always overwrites url/cert_file/fingerprint, but only
    preserves the existing `token` when `fingerprint` matches what was
    already stored under this name - the fingerprint, not the name, is the
    actual cryptographic identity a per-peer token is bound to. A routine
    re-confirmation of the *same* party's cert (fingerprint unchanged, even
    if its url moved) shouldn't invalidate the credential already handed to
    them for `grant add`; but a *different* fingerprint means this local
    name now points at a different entity - possibly the same person's new
    hardware, possibly a mistake, possibly a swapped-in attacker the human
    just approved without noticing - and carrying the old token forward
    would hand that new, different entity a credential already registered
    (and still valid) in the *previous* entity's grants.json, letting it
    replay that back for the previous entity's own granted access. See
    cli.py's cmd_hosts, which surfaces `token_regenerated` so the caller
    knows to re-share it when this happens."""
    cert_dir = cert_dir or default_known_hosts_dir()
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / f"{name}.pem"
    cert_path.write_text(cert_pem)
    hosts = load_known_hosts(path)
    existing = hosts.get(name)
    same_identity = bool(existing) and existing.get("fingerprint") == fingerprint
    token = existing["token"] if same_identity and existing.get("token") else auth.generate_token()
    token_regenerated = bool(existing) and not same_identity
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "url": url,
        "cert_file": str(cert_path),
        "fingerprint": fingerprint,
        "token": token,
        "added_at": now,
        # Same as added_at on first trust - a brand-new host is, by
        # definition, the one you just connected to. touch_known_host()
        # bumps this on every later switch so history ordering (most
        # recently used first) reflects actual use, not just when it was
        # first trusted.
        "last_connected": now,
    }
    hosts[name] = entry
    save_known_hosts(hosts, path)
    # token_regenerated is caller-facing only, not part of the persisted
    # entry - a fresh dict, not a mutation of what was just saved.
    return {**entry, "token_regenerated": token_regenerated}


def rotate_host_token(name: str, path: Path | None = None) -> dict | None:
    """Explicitly replaces just this peer's token, leaving url/cert_file/
    fingerprint untouched - for when you suspect it leaked, separate from
    add_known_host's own re-trust flow (which deliberately preserves it).
    None if `name` isn't known. The new value still needs to be re-shared
    with that peer's owner for them to `grant add` again - rotating it here
    doesn't reach their grants.json."""
    hosts = load_known_hosts(path)
    entry = hosts.get(name)
    if entry is None:
        return None
    entry["token"] = auth.generate_token()
    save_known_hosts(hosts, path)
    return entry


def remove_known_host(name: str, path: Path | None = None) -> bool:
    hosts = load_known_hosts(path)
    entry = hosts.pop(name, None)
    if entry is None:
        return False
    save_known_hosts(hosts, path)
    cert_file = entry.get("cert_file")
    if cert_file:
        Path(cert_file).unlink(missing_ok=True)
    return True


def get_known_host(name: str, path: Path | None = None) -> dict | None:
    return load_known_hosts(path).get(name)


def touch_known_host(name: str, path: Path | None = None) -> dict | None:
    """Bumps last_connected to now - called every time the panel/CLI
    actually switches to this host (not on every REST call made while
    already on it), so history ordering tracks "which coordinators have you
    used recently" rather than every single request. Returns the updated
    entry, or None if `name` isn't known."""
    hosts = load_known_hosts(path)
    entry = hosts.get(name)
    if entry is None:
        return None
    entry["last_connected"] = datetime.now(timezone.utc).isoformat()
    save_known_hosts(hosts, path)
    return entry

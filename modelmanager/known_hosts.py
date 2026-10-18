from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import default_known_hosts_dir, default_known_hosts_path


def load_known_hosts(path: Path | None = None) -> dict:
    path = path or default_known_hosts_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_known_hosts(hosts: dict, path: Path | None = None) -> Path:
    path = path or default_known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(hosts, indent=2) + "\n")
    os.replace(tmp_path, path)
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
    existing name overwrites it (re-trusting a rotated cert for someone you
    already know, same `grant add`-style overwrite-by-name convention)."""
    cert_dir = cert_dir or default_known_hosts_dir()
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / f"{name}.pem"
    cert_path.write_text(cert_pem)
    hosts = load_known_hosts(path)
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "url": url,
        "cert_file": str(cert_path),
        "fingerprint": fingerprint,
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

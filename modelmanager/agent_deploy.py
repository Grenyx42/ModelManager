from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from . import remote_paths
from .config import Registry, SystemConfig
from .errors import CommandFailedError
from .runners.ssh import SSHRunner

logger = logging.getLogger("modelmanager.agent_deploy")

LOCAL_AGENT_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "windows" / "agent.ps1"


def local_agent_hash() -> str:
    return hashlib.sha256(LOCAL_AGENT_SCRIPT_PATH.read_bytes()).hexdigest().lower()


def _remote_agent_hash(runner: SSHRunner, script_path: str) -> str | None:
    cmd = (
        "powershell -NoProfile -Command "
        f"\"if (Test-Path '{script_path}') "
        f"{{ (Get-FileHash -Path '{script_path}' -Algorithm SHA256).Hash }}\""
    )
    result = runner.run_shell(cmd, timeout=15)
    text = result.stdout.strip()
    return text.lower() if result.ok and text else None


def is_deployable(system: SystemConfig) -> bool:
    return system.os == "windows" and system.control == "ssh"


def deploy_if_needed(system_name: str, system: SystemConfig, force: bool = False) -> dict:
    """Uploads agent.ps1 to `system` over SSH iff its content differs from what's
    already there (or `force`). Safe to call often - a no-op costs one SSH round trip.

    Deploys to ~/.ModelManager/agent.ps1 (the SSH user's own home directory on
    that box), resolved fresh per system rather than assumed, since the exact
    Windows profile path (C:\\Users\\<name>) varies per machine/account.
    """
    if not is_deployable(system):
        return {"system": system_name, "action": "skipped", "reason": "not a windows/ssh system"}

    runner = SSHRunner(system.ssh_host)
    agent_dir = remote_paths.windows_agent_dir(runner)
    script_path = remote_paths.windows_agent_script_path(runner)
    local_hash = local_agent_hash()

    if not force:
        remote_hash = _remote_agent_hash(runner, script_path)
        if remote_hash == local_hash:
            return {"system": system_name, "action": "unchanged", "hash": local_hash}

    mkdir_result = runner.run_shell(
        f"powershell -NoProfile -Command \"New-Item -ItemType Directory -Force -Path '{agent_dir}\\pids','{agent_dir}\\logs' | Out-Null\"",
        timeout=15,
    )
    if not mkdir_result.ok:
        raise CommandFailedError(f"failed to prepare {agent_dir!r} on {system_name!r}: {mkdir_result.stderr.strip()}")

    upload_result = runner.upload_file(LOCAL_AGENT_SCRIPT_PATH, script_path.replace("\\", "/"))
    if not upload_result.ok:
        raise CommandFailedError(f"failed to upload agent.ps1 to {system_name!r}: {upload_result.stderr.strip()}")

    return {"system": system_name, "action": "deployed", "hash": local_hash, "path": script_path}


def deploy_all(registry: Registry, force: bool = False) -> list[dict]:
    results = []
    for name, system in registry.systems.items():
        if not is_deployable(system):
            continue
        try:
            results.append(deploy_if_needed(name, system, force=force))
        except Exception as exc:
            logger.warning("agent.ps1 deploy to %s failed: %s", name, exc)
            results.append({"system": name, "action": "failed", "error": str(exc)})
    return results

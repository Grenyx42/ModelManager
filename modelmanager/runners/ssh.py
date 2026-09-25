from __future__ import annotations

import subprocess
from pathlib import Path

from .base import CommandResult, Runner

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


class SSHRunner(Runner):
    """Runs commands on a remote host via the system `ssh` binary, reusing ~/.ssh/config."""

    def __init__(self, host_alias: str):
        self.host_alias = host_alias

    def run(self, cmd: list[str], timeout: float | None = 30) -> CommandResult:
        # Join into a single remote command string; SSH always execs via the remote shell.
        remote_cmd = subprocess.list2cmdline(cmd)
        return self.run_shell(remote_cmd, timeout=timeout)

    def run_shell(self, cmd: str, timeout: float | None = 30) -> CommandResult:
        full = ["ssh", *SSH_OPTS, self.host_alias, cmd]
        proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def upload_file(self, local_path: Path, remote_path: str, timeout: float | None = 30) -> CommandResult:
        """Copies a local file to this host via `scp`, reusing the same ~/.ssh/config alias."""
        full = ["scp", *SSH_OPTS, str(local_path), f"{self.host_alias}:{remote_path}"]
        proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

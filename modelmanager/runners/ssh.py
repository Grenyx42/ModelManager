from __future__ import annotations

import subprocess
from pathlib import Path

from ..errors import CommandTimeoutError, HostUnreachableError
from .base import CommandResult, Runner

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

# ssh's own exit code for a connection-level failure (unreachable host, DNS
# failure, auth failure) - never the remote command's own exit code, so this
# is a safe signal to raise rather than hand back as a soft CommandResult.
SSH_CONNECT_FAILURE_CODE = 255


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
        proc = self._run(full, timeout)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def upload_file(self, local_path: Path, remote_path: str, timeout: float | None = 30) -> CommandResult:
        """Copies a local file to this host via `scp`, reusing the same ~/.ssh/config alias."""
        full = ["scp", *SSH_OPTS, str(local_path), f"{self.host_alias}:{remote_path}"]
        proc = self._run(full, timeout)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def _run(self, full: list[str], timeout: float | None) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(
                f"command to {self.host_alias!r} timed out after {timeout}s: {full!r}"
            ) from exc
        if proc.returncode == SSH_CONNECT_FAILURE_CODE:
            raise HostUnreachableError(
                f"cannot reach {self.host_alias!r} over ssh: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

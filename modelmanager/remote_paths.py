from __future__ import annotations

from .errors import CommandFailedError
from .runners.ssh import SSHRunner

AGENT_DIR_NAME = ".ModelManager"
AGENT_SCRIPT_NAME = "agent.ps1"

# Resolved home directories are cached per SSH host alias for the life of the
# process - they don't change, and re-querying on every call would add an SSH
# round trip to every start/stop/status/logs action. We deliberately never
# hardcode a Windows path like C:\Users\<name> in Python: the outer shell SSH
# lands in (cmd.exe or PowerShell, depending on the box's sshd config) isn't
# guaranteed, so we resolve $env:USERPROFILE once *inside* a PowerShell
# process (which always understands it, regardless of the outer shell) and
# reuse that concrete, unambiguous string everywhere after.
_home_cache: dict[str, str] = {}


def windows_home_dir(runner: SSHRunner) -> str:
    if runner.host_alias not in _home_cache:
        # Single-quoted: some Windows sshd setups use PowerShell itself as the
        # default shell for exec'd commands, which double-quote-interpolates
        # $env:USERPROFILE in *this* string before the inner `powershell`
        # process ever sees it, turning it into an already-expanded path
        # that gets passed as a literal (and fails to run as a command).
        # Single quotes are never interpolated, so the literal text survives
        # to the inner PowerShell process, which then evaluates it itself.
        result = runner.run_shell("powershell -NoProfile -Command '$env:USERPROFILE'", timeout=15)
        home = result.stdout.strip()
        if not result.ok or not home:
            raise CommandFailedError(
                f"could not resolve home directory on {runner.host_alias!r}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        _home_cache[runner.host_alias] = home
    return _home_cache[runner.host_alias]


def windows_agent_dir(runner: SSHRunner) -> str:
    return f"{windows_home_dir(runner)}\\{AGENT_DIR_NAME}"


def windows_agent_script_path(runner: SSHRunner) -> str:
    return f"{windows_agent_dir(runner)}\\{AGENT_SCRIPT_NAME}"


def windows_agent_log_path(runner: SSHRunner, name: str) -> str:
    """Matches Get-LogPath in scripts/windows/agent.ps1. This is stdout only -
    llama-server (and most everything else we launch this way) logs almost
    entirely to stderr, so a caller wanting the actual output nearly always
    also wants windows_agent_err_log_path()."""
    return f"{windows_agent_dir(runner)}\\logs\\{name}.log"


def windows_agent_err_log_path(runner: SSHRunner, name: str) -> str:
    """Matches Get-ErrLogPath in scripts/windows/agent.ps1."""
    return f"{windows_agent_dir(runner)}\\logs\\{name}.err.log"

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from ..errors import CommandTimeoutError
from .base import CommandResult, Runner


class LocalRunner(Runner):
    def __init__(self):
        # Tracks Popen objects for processes started by *this* runner instance
        # so we can reap them via poll()/wait() instead of leaving zombies
        # behind (os.kill(pid, 0) alone succeeds for an unreaped zombie, since
        # the PID stays allocated until something waits on it). Pid-file-only
        # lookups (e.g. after a coordinator restart) fall back to os.kill,
        # which is correct there since a dead former parent's children are
        # already reaped by init by the time we'd check.
        self._children: dict[int, subprocess.Popen] = {}

    def run(self, cmd: list[str], timeout: float | None = 30) -> CommandResult:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(f"command timed out after {timeout}s: {cmd!r}") from exc
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def run_shell(self, cmd: str, timeout: float | None = 30) -> CommandResult:
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(f"command timed out after {timeout}s: {cmd!r}") from exc
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def start_background(
        self,
        cmd: list[str],
        log_path: Path,
        pid_path: Path,
        extra_env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> int:
        log_fh = open(log_path, "ab")
        # Written into the same log a caller tails/views for this model - so
        # "why won't this load" starts with "what did we actually run", not a
        # guess from the registry definition, and each restart's line stays
        # in the log's history alongside its own output (append mode, never
        # overwritten by the next launch).
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"[{timestamp}] launching: {shlex.join(cmd)}"
        if cwd:
            header += f" (cwd={cwd})"
        log_fh.write((header + "\n").encode())
        log_fh.flush()
        env = {**os.environ, **extra_env} if extra_env else None
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            cwd=cwd,
        )
        pid_path.write_text(str(proc.pid))
        self._children[proc.pid] = proc
        return proc.pid

    def is_running(self, pid: int) -> bool:
        proc = self._children.get(pid)
        if proc is not None:
            if proc.poll() is None:
                return True
            del self._children[pid]
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False
        return True

    def stop(self, pid: int, timeout: float = 10) -> None:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return

        proc = self._children.get(pid)
        if proc is not None:
            try:
                proc.wait(timeout=timeout)
                del self._children[pid]
                return
            except subprocess.TimeoutExpired:
                pass

        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running(pid):
                return
            time.sleep(0.2)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc is not None:
            proc.wait(timeout=5)
            self._children.pop(pid, None)

    def tail(self, path: Path, lines: int = 200) -> str:
        if not path.exists():
            return ""
        result = subprocess.run(
            ["tail", "-n", str(lines), str(path)], capture_output=True, text=True
        )
        return result.stdout

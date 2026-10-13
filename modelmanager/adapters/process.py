from __future__ import annotations

import json
import time

from .. import paths, remote_paths
from ..config import Candidate, EngineConfig, SystemConfig
from ..errors import EngineControlError, PortInUseError
from ..port_check import check_local_port_free
from ..runners.local import LocalRunner
from ..runners.ssh import SSHRunner
from . import windows_agent
from .base import AdapterStatus, EngineAdapter

# Unlike LlamaCppAdapter, this adapter never constructs --port itself -
# candidate.args is the engine's *entire* command line verbatim, so there's
# no way to tell the spawned process to bind a different port than whatever
# is already hardcoded in args. That means candidate.port can't be
# dynamically remapped here the way it can for llama.cpp - this only checks
# it's actually free before launching and raises cleanly if not (same as
# Ollama's self-managed-server path, for the same reason: a fixed port
# neither side can move).

# See llamacpp.py's STARTUP_GRACE_SECONDS - same reasoning: a bad launch
# (wrong path, port already in use, a missing dependency) crashes almost
# immediately, so a short grace period catches it without delaying a normal
# start.
STARTUP_GRACE_SECONDS = 1.5


class ProcessAdapter(EngineAdapter):
    """Manages an arbitrary long-running process (a custom proxy/server, not
    llama.cpp or Ollama), locally or on a remote Windows box.

    Identical PID/log-file control to LlamaCppAdapter - the only difference is
    start() doesn't assume llama-server's --model/--port CLI convention: a
    candidate's 'args' is the engine binary's entire command line as-is.
    """

    def __init__(self, engine: EngineConfig, system: SystemConfig, runner):
        self.engine = engine
        self.system = system
        self.runner = runner

    def start(self, definition_name: str, candidate: Candidate) -> int:
        args = list(candidate.args)
        if isinstance(self.runner, LocalRunner):
            check_local_port_free(candidate.port, self.engine.system)
            cmd = [self.engine.binary, *args]
            pid = self.runner.start_background(
                cmd,
                paths.log_file(definition_name),
                paths.pid_file(definition_name),
                extra_env=self.engine.env or None,
                cwd=self.engine.cwd or None,
            )
            time.sleep(STARTUP_GRACE_SECONDS)
            if not self.runner.is_running(pid):
                paths.pid_file(definition_name).unlink(missing_ok=True)
                tail = self.runner.tail(paths.log_file(definition_name), 20)
                raise EngineControlError(f"{definition_name} exited immediately after launching (crashed on startup): {tail}")
            return candidate.port
        port_result = self._invoke("check-port", {"port": candidate.port})
        if not port_result.ok:
            raise PortInUseError(f"port {candidate.port} is already in use on {self.engine.system!r}: {port_result.stderr.strip() or port_result.stdout.strip()}")
        payload = {
            "name": definition_name,
            "binary": self.engine.binary,
            "args": args,
            "env": self.engine.env,
            "cwd": self.engine.cwd,
        }
        result = self._invoke("start", payload)
        if not result.ok:
            raise EngineControlError(f"failed to start {definition_name} on {self.system}: {result.stderr}")
        return candidate.port

    def stop(self, definition_name: str, candidate: Candidate) -> None:
        if isinstance(self.runner, LocalRunner):
            pid_path = paths.pid_file(definition_name)
            if pid_path.exists():
                pid = int(pid_path.read_text().strip())
                self.runner.stop(pid)
                pid_path.unlink(missing_ok=True)
            return
        result = self._invoke("stop", {"name": definition_name})
        if not result.ok:
            raise EngineControlError(f"failed to stop {definition_name}: {result.stderr}")

    def status(self, definition_name: str, candidate: Candidate) -> AdapterStatus:
        if isinstance(self.runner, LocalRunner):
            pid_path = paths.pid_file(definition_name)
            if not pid_path.exists():
                return AdapterStatus(running=False)
            pid = int(pid_path.read_text().strip())
            return AdapterStatus(running=self.runner.is_running(pid), pid=pid)
        result = self._invoke("status", {"name": definition_name})
        if not result.ok:
            return AdapterStatus(running=False, detail=result.stderr)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return AdapterStatus(running=False, detail=result.stdout)
        return AdapterStatus(running=data.get("running", False), pid=data.get("pid"))

    def logs(self, definition_name: str, candidate: Candidate, lines: int = 200) -> str:
        if isinstance(self.runner, LocalRunner):
            return self.runner.tail(paths.log_file(definition_name), lines)
        result = self._invoke("logs", {"name": definition_name, "lines": lines})
        return result.stdout if result.ok else result.stderr

    def log_paths(self, definition_name: str, candidate: Candidate) -> list[str]:
        if isinstance(self.runner, LocalRunner):
            return [str(paths.log_file(definition_name))]
        assert isinstance(self.runner, SSHRunner)
        return [
            remote_paths.windows_agent_log_path(self.runner, definition_name),
            remote_paths.windows_agent_err_log_path(self.runner, definition_name),
        ]

    def _invoke(self, action: str, payload: dict):
        assert isinstance(self.runner, SSHRunner)
        return windows_agent.invoke(self.runner, action, payload)

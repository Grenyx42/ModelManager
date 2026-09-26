from __future__ import annotations

import json

from .. import paths
from ..config import Candidate, EngineConfig, SystemConfig
from ..runners.local import LocalRunner
from ..runners.ssh import SSHRunner
from . import windows_agent
from .base import AdapterStatus, EngineAdapter


class LlamaCppAdapter(EngineAdapter):
    """Manages a raw llama-server process, locally or on a remote Windows box.

    Local (sleek): plain subprocess managed by LocalRunner, with a PID file and log file.
    Remote (monolith/coder, Windows): delegates to scripts/windows/agent.ps1 over SSH,
    since native Windows process lifecycle differs enough from Linux to need its own script.
    """

    def __init__(self, engine: EngineConfig, system: SystemConfig, runner):
        self.engine = engine
        self.system = system
        self.runner = runner

    def start(self, definition_name: str, candidate: Candidate) -> None:
        args = ["--model", candidate.model_path, "--port", str(candidate.port), *candidate.args]
        if isinstance(self.runner, LocalRunner):
            cmd = [self.engine.binary, *args]
            self.runner.start_background(
                cmd,
                paths.log_file(definition_name),
                paths.pid_file(definition_name),
                extra_env=self.engine.env or None,
                cwd=self.engine.cwd or None,
            )
            return
        payload = {
            "name": definition_name,
            "binary": self.engine.binary,
            "args": args,
            "env": self.engine.env,
            "cwd": self.engine.cwd,
        }
        result = self._invoke("start", payload)
        if not result.ok:
            raise RuntimeError(f"failed to start {definition_name} on {self.system}: {result.stderr}")

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
            raise RuntimeError(f"failed to stop {definition_name}: {result.stderr}")

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

    def _invoke(self, action: str, payload: dict):
        assert isinstance(self.runner, SSHRunner)
        return windows_agent.invoke(self.runner, action, payload)

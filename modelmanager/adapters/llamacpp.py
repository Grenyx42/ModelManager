from __future__ import annotations

import json
import time

import httpx

from .. import paths, remote_paths
from ..config import Candidate, EngineConfig, SystemConfig
from ..errors import EngineControlError
from ..port_check import is_local_port_free, resolve_port
from ..runners.local import LocalRunner
from ..runners.ssh import SSHRunner
from . import windows_agent
from .base import AdapterStatus, EngineAdapter

# A bad launch (missing model file, port already in use, a GPU/driver error,
# a bad CLI flag) crashes llama-server almost immediately - well before a
# large model would actually finish loading - so a short grace period is
# enough to catch it without meaningfully delaying every normal start.
STARTUP_GRACE_SECONDS = 1.5

# The activity probe runs on every tick of the activity tracker, so it has to
# stay cheap and must never be the reason a tick stalls - /slots is a local,
# in-memory read on llama-server's side.
BUSY_PROBE_TIMEOUT_SECONDS = 2.0


class LlamaCppAdapter(EngineAdapter):
    """Manages a raw llama-server process, locally or on a remote Windows box.

    Local: plain subprocess managed by LocalRunner, with a PID file and log file.
    Remote (Windows): delegates to scripts/windows/agent.ps1 over SSH,
    since native Windows process lifecycle differs enough from Linux to need its own script.
    """

    def __init__(self, engine: EngineConfig, system: SystemConfig, runner):
        self.engine = engine
        self.system = system
        self.runner = runner

    def start(self, definition_name: str, candidate: Candidate) -> int:
        if isinstance(self.runner, LocalRunner):
            port = resolve_port(is_local_port_free, candidate.port, self.engine.system)
            args = ["--model", candidate.model_path, "--port", str(port), *candidate.args]
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
            paths.port_file(definition_name).write_text(str(port))
            return port
        port = resolve_port(lambda p: self._invoke("check-port", {"port": p}).ok, candidate.port, self.engine.system)
        args = ["--model", candidate.model_path, "--port", str(port), *candidate.args]
        payload = {
            "name": definition_name,
            "binary": self.engine.binary,
            "args": args,
            "env": self.engine.env,
            "cwd": self.engine.cwd,
            "port": port,
        }
        result = self._invoke("start", payload)
        if not result.ok:
            raise EngineControlError(f"failed to start {definition_name} on {self.system}: {result.stderr}")
        return port

    def stop(self, definition_name: str, candidate: Candidate) -> None:
        if isinstance(self.runner, LocalRunner):
            pid_path = paths.pid_file(definition_name)
            if pid_path.exists():
                pid = int(pid_path.read_text().strip())
                self.runner.stop(pid)
                pid_path.unlink(missing_ok=True)
            paths.port_file(definition_name).unlink(missing_ok=True)
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
            port_path = paths.port_file(definition_name)
            port = int(port_path.read_text().strip()) if port_path.exists() else None
            return AdapterStatus(running=self.runner.is_running(pid), pid=pid, port=port)
        result = self._invoke("status", {"name": definition_name})
        if not result.ok:
            return AdapterStatus(running=False, detail=result.stderr)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return AdapterStatus(running=False, detail=result.stdout)
        return AdapterStatus(running=data.get("running", False), pid=data.get("pid"), port=data.get("port"))

    def busy(self, definition_name: str, candidate: Candidate, host: str, port: int) -> bool | None:
        """True when any llama-server slot is mid-request.

        /slots reports is_processing per slot and needs no extra launch flags,
        which is why it's preferred over /metrics here - the Prometheus
        endpoint only answers when llama-server was started with --metrics,
        and returns 501 otherwise.

        The tradeoff is that is_processing is instantaneous, not cumulative:
        this samples, so a trickle of very short requests landing entirely
        between two ticks is invisible. That's a far smaller gap than the one
        it closes (no signal at all), and the sweep double-checks before it
        evicts anything, so nothing dies mid-generation. Enabling --metrics
        and diffing its counters would make detection exact if a workload
        ever needs it.
        """
        try:
            resp = httpx.get(f"http://{host}:{port}/slots", timeout=BUSY_PROBE_TIMEOUT_SECONDS)
            resp.raise_for_status()
            slots = resp.json()
        except (httpx.HTTPError, ValueError):
            # Unreachable, still loading, or /slots disabled (--no-slots): no
            # signal, so say so rather than claiming the model is idle.
            return None
        if not isinstance(slots, list):
            return None
        return any(slot.get("is_processing") for slot in slots if isinstance(slot, dict))

    def logs(self, definition_name: str, candidate: Candidate, lines: int = 200) -> str:
        if isinstance(self.runner, LocalRunner):
            return self.runner.tail(paths.log_file(definition_name), lines)
        result = self._invoke("logs", {"name": definition_name, "lines": lines})
        return result.stdout if result.ok else result.stderr

    def log_paths(self, definition_name: str, candidate: Candidate) -> list[str]:
        if isinstance(self.runner, LocalRunner):
            return [str(paths.log_file(definition_name))]
        assert isinstance(self.runner, SSHRunner)
        # llama-server logs almost entirely to stderr (see agent.ps1's start
        # action) - a caller tailing only the stdout path would see next to
        # nothing.
        return [
            remote_paths.windows_agent_log_path(self.runner, definition_name),
            remote_paths.windows_agent_err_log_path(self.runner, definition_name),
        ]

    def _invoke(self, action: str, payload: dict):
        assert isinstance(self.runner, SSHRunner)
        return windows_agent.invoke(self.runner, action, payload)
